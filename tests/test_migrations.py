import os
import shutil
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import main
from config import settings
from migrate import BASELINE_SCHEMA, alembic_config, current_revision, is_up_to_date, migrate
from migration_helpers import (
    build_pre_alembic_database,
    insert_rows,
    reset_postgres,
    schema_differences,
    snapshot,
    sqlite_url,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DATABASE = PROJECT_ROOT / "policy_assistant.db"
HEAD_REVISION = "0002_workspaces"


def make_legacy_database(path: Path) -> str:
    """A pre-Alembic database with a few related rows."""
    url = sqlite_url(path)
    build_pre_alembic_database(url)
    insert_rows(url, {
        "users": [{"id": 1, "username": "alice", "email": "alice@example.com", "hashed_password": "x", "is_admin": True}],
        "incidents": [{"id": 1, "title": "Outage", "status": "open", "creator_id": 1}],
        "documents": [{"id": 1, "title": "Log", "file_path": "a.log", "type": "log", "status": "processed", "uploader_id": 1}],
        "log_entries": [{"id": 1, "document_id": 1, "message": "boom", "level": "ERROR"}],
        "question_logs": [{"id": 1, "incident_id": 1, "user_id": 1, "question": "why?", "ai_answer": "because"}],
    })
    return url


def test_migrations_build_the_same_schema_as_the_models(tmp_path):
    url = sqlite_url(tmp_path / "fresh.db")

    migrate(url)

    assert schema_differences(url) == []


def test_fresh_database_ends_at_head(tmp_path):
    url = sqlite_url(tmp_path / "fresh.db")

    migrate(url)

    engine = create_engine(url)
    assert is_up_to_date(engine)
    engine.dispose()


def test_legacy_database_is_adopted_and_upgraded_without_changing_rows(tmp_path):
    url = make_legacy_database(tmp_path / "legacy.db")
    before = snapshot(url)

    migrate(url)

    assert snapshot(url) == before
    engine = create_engine(url)
    assert current_revision(engine) == HEAD_REVISION
    engine.dispose()


def test_running_migrations_twice_is_safe(tmp_path):
    url = make_legacy_database(tmp_path / "legacy.db")
    before = snapshot(url)

    migrate(url)
    migrate(url)

    assert snapshot(url) == before
    assert schema_differences(url) == []


def test_legacy_database_missing_columns_is_not_stamped(tmp_path):
    url = sqlite_url(tmp_path / "drifted.db")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)"))
        connection.execute(text("INSERT INTO users (id, username) VALUES (1, 'alice')"))

    with pytest.raises(RuntimeError, match="users.email"):
        migrate(url)

    assert current_revision(engine) is None
    assert "incidents" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT username FROM users")).scalar_one() == "alice"
    engine.dispose()


def test_baseline_downgrade_is_refused(tmp_path):
    url = sqlite_url(tmp_path / "fresh.db")
    migrate(url)
    command.downgrade(alembic_config(url), "0001_baseline")

    with pytest.raises(NotImplementedError):
        command.downgrade(alembic_config(url), "base")

    engine = create_engine(url)
    assert "users" in inspect(engine).get_table_names()
    engine.dispose()


def test_app_refuses_to_start_on_unmigrated_database(tmp_path, monkeypatch):
    engine = create_engine(sqlite_url(tmp_path / "empty.db"))
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "wait_for_database", lambda: None)

    with pytest.raises(RuntimeError, match="python migrate.py"):
        with TestClient(main.app):
            pass
    engine.dispose()


@pytest.mark.skipif(not LOCAL_DATABASE.exists(), reason="No local policy_assistant.db to copy.")
def test_copy_of_local_database_migrates_without_data_changes(tmp_path):
    copy = tmp_path / "policy_assistant_copy.db"
    shutil.copyfile(LOCAL_DATABASE, copy)
    url = sqlite_url(copy)
    before = snapshot(url)

    migrate(url)
    migrate(url)

    assert snapshot(url) == before
    assert schema_differences(url) == []


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Set TEST_POSTGRES_URL to an empty Postgres database.")
def test_fresh_postgres_database_migrates_twice_with_pgvector(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    reset_postgres(url)
    # The baseline decides on pgvector from settings, which conftest points at SQLite.
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setenv("USE_PGVECTOR", "true")

    migrate(url)
    migrate(url)

    engine = create_engine(url)
    assert is_up_to_date(engine)
    assert set(BASELINE_SCHEMA) <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
        assert connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one() == "vector"
        assert connection.execute(text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
        )).scalar_one() == f"vector({settings.EMBEDDING_DIM})"
        index_definition = connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_document_chunks_embedding_hnsw'"
        )).scalar_one()
        assert "USING hnsw" in index_definition
        assert "vector_cosine_ops" in index_definition

        workspace_id = connection.execute(text(
            "INSERT INTO workspaces (name) VALUES ('vectors') RETURNING id"
        )).scalar_one()
        document_id = connection.execute(
            text(
                "INSERT INTO documents (title, file_path, type, status, workspace_id) "
                "VALUES ('runbook', 'r.md', 'runbook', 'processed', :workspace_id) RETURNING id"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
        near = [1.0] + [0.0] * (settings.EMBEDDING_DIM - 1)
        far = [0.0, 1.0] + [0.0] * (settings.EMBEDDING_DIM - 2)
        for order, vector in enumerate([far, near]):
            connection.execute(
                text(
                    "INSERT INTO document_chunks (document_id, chunk_text, chunk_order, embedding) "
                    "VALUES (:document_id, :chunk_text, :chunk_order, CAST(:embedding AS vector))"
                ),
                {"document_id": document_id, "chunk_text": f"chunk {order}", "chunk_order": order, "embedding": str(vector)},
            )
        closest = connection.execute(
            text(
                "SELECT chunk_text FROM document_chunks WHERE document_id = :document_id "
                "ORDER BY embedding <=> CAST(:query AS vector) LIMIT 1"
            ),
            {"document_id": document_id, "query": str(near)},
        ).scalar_one()
        assert closest == "chunk 1"
    engine.dispose()
