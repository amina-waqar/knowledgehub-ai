import os

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import main
from conftest import TEST_PASSWORD, login
from migrate import alembic_config, migrate
from migration_helpers import (
    build_pre_alembic_database,
    insert_rows,
    reset_postgres,
    schema_differences,
    snapshot,
    sqlite_url,
)

ALICE, BOB, CAROL = 1, 2, 3


def seed_rows(include_orphans: bool) -> dict[str, list[dict]]:
    """
    alice (global admin) and bob own records, carol owns nothing.
    Incident 12 and document 22 have no owner. Question 51 is bob asking
    about alice's incident. Question 53 points at an incident that no
    longer exists (SQLite does not enforce foreign keys).
    """
    password_hash = main.get_password_hash(TEST_PASSWORD)
    rows = {
        "users": [
            {"id": ALICE, "username": "alice", "email": "alice@example.com", "hashed_password": password_hash, "is_admin": True},
            {"id": BOB, "username": "bob", "email": "bob@example.com", "hashed_password": password_hash, "is_admin": False},
            {"id": CAROL, "username": "carol", "email": "carol@example.com", "hashed_password": password_hash, "is_admin": False},
        ],
        "incidents": [
            {"id": 10, "title": "Alice outage", "status": "open", "creator_id": ALICE},
            {"id": 11, "title": "Bob outage", "status": "open", "creator_id": BOB},
            {"id": 12, "title": "Ownerless outage", "status": "open", "creator_id": None},
        ],
        "documents": [
            {"id": 20, "title": "Alice runbook", "file_path": "a.md", "type": "runbook", "status": "processed", "uploader_id": ALICE},
            {"id": 21, "title": "Bob log", "file_path": "b.log", "type": "log", "status": "processed", "uploader_id": BOB},
            {"id": 22, "title": "Ownerless runbook", "file_path": "c.md", "type": "runbook", "status": "processed", "uploader_id": None},
        ],
        "document_chunks": [
            {"id": 30, "document_id": 20, "chunk_text": "restart workers", "chunk_order": 0},
            {"id": 31, "document_id": 22, "chunk_text": "check the pool", "chunk_order": 0},
        ],
        "log_entries": [
            {"id": 40, "document_id": 21, "message": "Database timeout", "level": "ERROR"},
        ],
        "question_logs": [
            {"id": 50, "incident_id": 10, "user_id": ALICE, "question": "why?", "ai_answer": "pool"},
            {"id": 51, "incident_id": 10, "user_id": BOB, "question": "bob asks about alice's incident", "ai_answer": "x"},
            {"id": 52, "incident_id": 11, "user_id": BOB, "question": "why bob?", "ai_answer": "y"},
        ],
        "answer_feedback": [
            {"id": 60, "incident_id": 11, "user_id": BOB, "question": "why bob?", "answer": "y", "is_helpful": True},
            {"id": 61, "incident_id": 12, "user_id": ALICE, "question": "q", "answer": "a", "is_helpful": False},
        ],
    }
    if include_orphans:
        rows["question_logs"].append(
            {"id": 53, "incident_id": 999, "user_id": BOB, "question": "orphan", "ai_answer": "z"}
        )
    return rows


def database_urls():
    yield pytest.param("sqlite", id="sqlite")
    yield pytest.param(
        "postgres",
        id="postgres",
        marks=pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Set TEST_POSTGRES_URL to test Postgres."),
    )


@pytest.fixture(params=database_urls())
def legacy_url(request, tmp_path):
    if request.param == "sqlite":
        url = sqlite_url(tmp_path / "legacy.db")
    else:
        url = os.environ["TEST_POSTGRES_URL"]
        reset_postgres(url)
    build_pre_alembic_database(url)
    insert_rows(url, seed_rows(include_orphans=request.param == "sqlite"))
    return url


def query(url: str, sql: str, **params) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql), params)]
    finally:
        engine.dispose()


def workspace_ids(url: str) -> dict[str, int]:
    """Workspace id by name, e.g. {"alice's workspace": 1, "Legacy (unassigned)": 4}."""
    return {name: workspace_id for workspace_id, name in query(url, "SELECT id, name FROM workspaces")}


def assigned(url: str, table: str) -> dict[int, int]:
    return dict(query(url, f"SELECT id, workspace_id FROM {table}"))


def test_backfill_keeps_every_row_and_schema_matches_models(legacy_url):
    before = snapshot(legacy_url)

    migrate(legacy_url)

    assert snapshot(legacy_url) == before
    assert schema_differences(legacy_url) == []
    for table in ("incidents", "documents", "question_logs", "answer_feedback"):
        assert query(legacy_url, f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL") == [(0,)]


def test_every_user_owns_exactly_one_personal_workspace(legacy_url):
    migrate(legacy_url)
    workspaces = workspace_ids(legacy_url)

    owners = query(legacy_url, "SELECT user_id, workspace_id FROM workspace_members WHERE role = 'owner'")

    assert sorted(owners) == [
        (ALICE, workspaces["alice's workspace"]),
        (BOB, workspaces["bob's workspace"]),
        (CAROL, workspaces["carol's workspace"]),
    ]


def test_records_follow_their_owner(legacy_url):
    migrate(legacy_url)
    ws = workspace_ids(legacy_url)
    alice, bob, legacy = ws["alice's workspace"], ws["bob's workspace"], ws["Legacy (unassigned)"]

    assert assigned(legacy_url, "incidents") == {10: alice, 11: bob, 12: legacy}
    assert assigned(legacy_url, "documents") == {20: alice, 21: bob, 22: legacy}
    assert assigned(legacy_url, "answer_feedback") == {60: bob, 61: legacy}
    questions = assigned(legacy_url, "question_logs")
    assert questions[50] == alice
    assert questions[51] == alice, "a question stays with the incident it is about"
    assert questions[52] == bob
    if 53 in questions:
        assert questions[53] == legacy


def test_legacy_workspace_is_only_open_to_global_admins(legacy_url):
    migrate(legacy_url)
    legacy = workspace_ids(legacy_url)["Legacy (unassigned)"]

    members = query(legacy_url, "SELECT user_id, role FROM workspace_members WHERE workspace_id = :id", id=legacy)

    assert members == [(ALICE, "admin")]


def test_no_user_is_added_to_another_users_personal_workspace(legacy_url):
    migrate(legacy_url)

    memberships = query(legacy_url, "SELECT user_id, count(*) FROM workspace_members GROUP BY user_id ORDER BY user_id")

    assert memberships == [(ALICE, 2), (BOB, 1), (CAROL, 1)]


def test_running_the_workspace_migration_twice_changes_nothing(legacy_url):
    migrate(legacy_url)
    after_first = (snapshot(legacy_url), assigned(legacy_url, "incidents"), workspace_ids(legacy_url))

    migrate(legacy_url)

    assert (snapshot(legacy_url), assigned(legacy_url, "incidents"), workspace_ids(legacy_url)) == after_first


def test_downgrade_removes_workspaces_but_keeps_records(legacy_url):
    before = snapshot(legacy_url)
    migrate(legacy_url)

    command.downgrade(alembic_config(legacy_url), "0001_baseline")

    assert snapshot(legacy_url) == before
    engine = create_engine(legacy_url)
    assert "workspaces" not in inspect(engine).get_table_names()
    engine.dispose()

    migrate(legacy_url)
    assert snapshot(legacy_url) == before


@pytest.fixture
def migrated_url(tmp_path):
    url = sqlite_url(tmp_path / "migrated.db")
    build_pre_alembic_database(url)
    insert_rows(url, seed_rows(include_orphans=True))
    migrate(url)
    return url


@pytest.fixture
def session_factory(migrated_url):
    """Overrides conftest so the API runs against the migrated database."""
    engine = create_engine(migrated_url, connect_args={"check_same_thread": False})
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def incident_ids(client, headers) -> set[int]:
    response = client.get("/incidents", headers=headers)
    assert response.status_code == 200, response.text
    return {incident["id"] for incident in response.json()["incidents"]}


def test_api_after_migration_shows_each_user_only_their_records(client, migrated_url):
    alice, bob, carol = login(client, "alice"), login(client, "bob"), login(client, "carol")

    assert incident_ids(client, alice) == {10}
    assert incident_ids(client, bob) == {11}
    assert incident_ids(client, carol) == set()
    assert client.get("/incidents/10", headers=bob).status_code == 404
    assert client.get("/incidents/12", headers=bob).status_code == 404
    assert [d["id"] for d in client.get("/documents", headers=bob).json()["documents"]] == [21]
    assert client.get("/logs/21/summary", headers=bob).status_code == 200
    assert client.get("/logs/21/summary", headers=alice).status_code == 404


def test_api_after_migration_legacy_records_reachable_only_by_admin(client, migrated_url):
    legacy = str(workspace_ids(migrated_url)["Legacy (unassigned)"])
    alice, bob = login(client, "alice"), login(client, "bob")

    assert incident_ids(client, {**alice, "X-Workspace-Id": legacy}) == {12}
    assert client.get("/incidents", headers={**bob, "X-Workspace-Id": legacy}).status_code == 404


def test_api_after_migration_analytics_counts_only_the_workspace(client, migrated_url):
    alice = login(client, "alice")

    summary = client.get("/analytics/summary", headers=alice).json()

    assert summary["total_questions"] == 2
    assert summary["documents"] == {"runbooks": 1, "logs": 0}
    assert summary["total_chunks"] == 1
