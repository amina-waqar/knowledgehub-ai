import pytest
from fastapi.routing import APIRoute

import main
from auth import get_current_user
from conftest import SAMPLE_LOG, SAMPLE_RUNBOOK, ask, create_incident, login, upload

PUBLIC_ROUTES = {"/health", "/health/llm", "/hello", "/auth/signup", "/auth/login"}


def in_workspace(headers: dict, workspace_id: int) -> dict:
    return {**headers, "X-Workspace-Id": str(workspace_id)}


def my_workspaces(client, headers) -> list[dict]:
    response = client.get("/workspaces", headers=headers)
    assert response.status_code == 200
    return response.json()["workspaces"]


def new_workspace(client, headers, name="Payments team") -> int:
    response = client.post("/workspaces", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add_member(client, headers, workspace_id, username, role="member"):
    return client.post(
        f"/workspaces/{workspace_id}/members", headers=headers, json={"username": username, "role": role}
    )


def requires_login(route: APIRoute) -> bool:
    pending = list(route.dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is get_current_user:
            return True
        pending.extend(dependency.dependencies)
    return False


def all_api_routes(routes) -> list[APIRoute]:
    # FastAPI keeps routers added with include_router nested rather than copying their routes.
    found = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(all_api_routes(route.original_router.routes))
    return found


def test_every_non_public_route_requires_login():
    routes = [r for r in all_api_routes(main.app.routes) if r.path not in PUBLIC_ROUTES]

    unprotected = [f"{sorted(r.methods)} {r.path}" for r in routes if not requires_login(r)]

    assert any(r.path.startswith("/workspaces") for r in routes)
    assert unprotected == []


def test_signup_creates_a_personal_workspace(client):
    client.post("/auth/signup", json={"username": "dana", "email": "dana@example.com", "password": "pw-for-tests"})
    headers = login(client, "dana", "pw-for-tests")

    assert [(w["name"], w["role"]) for w in my_workspaces(client, headers)] == [("dana's workspace", "owner")]


def test_create_workspace_makes_creator_owner(client, user_headers):
    workspace_id = new_workspace(client, user_headers, "  Payments team  ")

    workspaces = {w["id"]: w for w in my_workspaces(client, user_headers)}

    assert workspaces[workspace_id] == {"id": workspace_id, "name": "Payments team", "role": "owner"}


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_workspace_name_is_validated(client, user_headers, name):
    assert client.post("/workspaces", headers=user_headers, json={"name": name}).status_code == 422


def test_header_selects_the_workspace_and_default_is_the_first(client, user_headers):
    team = new_workspace(client, user_headers)
    personal = my_workspaces(client, user_headers)[0]["id"]

    in_team = create_incident(client, in_workspace(user_headers, team), title="Team incident")
    by_default = create_incident(client, user_headers, title="Personal incident")

    assert in_team["workspace_id"] == team
    assert by_default["workspace_id"] == personal
    team_titles = [i["title"] for i in client.get("/incidents", headers=in_workspace(user_headers, team)).json()["incidents"]]
    assert team_titles == ["Team incident"]


def test_unknown_and_foreign_workspaces_look_the_same(client, user_headers, other_user_headers):
    foreign = my_workspaces(client, other_user_headers)[0]["id"]

    foreign_response = client.get("/incidents", headers=in_workspace(user_headers, foreign))
    missing_response = client.get("/incidents", headers=in_workspace(user_headers, 9999))

    assert foreign_response.status_code == missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()


def test_non_member_cannot_list_members(client, user_headers, other_user_headers):
    foreign = my_workspaces(client, other_user_headers)[0]["id"]
    assert client.get(f"/workspaces/{foreign}/members", headers=user_headers).status_code == 404


def test_added_member_can_see_the_workspace(client, user_headers, other_user_headers):
    team = new_workspace(client, user_headers)
    incident = create_incident(client, in_workspace(user_headers, team))

    assert add_member(client, user_headers, team, "bob").status_code == 201

    bob_in_team = in_workspace(other_user_headers, team)
    assert client.get(f"/incidents/{incident['id']}", headers=bob_in_team).status_code == 200
    members = client.get(f"/workspaces/{team}/members", headers=bob_in_team).json()["members"]
    assert [(m["username"], m["role"]) for m in members] == [("alice", "owner"), ("bob", "member")]


def test_viewer_can_read_but_not_write(client, user_headers, other_user_headers):
    team = new_workspace(client, user_headers)
    incident = create_incident(client, in_workspace(user_headers, team))
    add_member(client, user_headers, team, "bob", role="viewer")
    viewer = in_workspace(other_user_headers, team)

    assert client.get("/incidents", headers=viewer).status_code == 200
    assert client.post("/incidents", headers=viewer, json={"title": "x"}).status_code == 403
    assert ask(client, viewer, incident["id"], "why?").status_code == 403


def test_removed_member_loses_access(client, user_headers, other_user_headers, db):
    team = new_workspace(client, user_headers)
    add_member(client, user_headers, team, "bob")
    bob_id = client.get("/me", headers=other_user_headers).json()["id"]

    response = client.delete(f"/workspaces/{team}/members/{bob_id}", headers=user_headers)

    assert response.status_code == 204
    assert client.get("/incidents", headers=in_workspace(other_user_headers, team)).status_code == 404


def test_member_management_rules(client, make_user, user_headers, other_user_headers):
    make_user("carol")
    make_user("dave")
    carol = login(client, "carol")
    team = new_workspace(client, user_headers)
    alice_id = client.get("/me", headers=user_headers).json()["id"]
    add_member(client, user_headers, team, "bob", role="member")
    add_member(client, user_headers, team, "carol", role="admin")

    assert add_member(client, other_user_headers, team, "dave").status_code == 403, "members can't add members"
    assert add_member(client, carol, team, "dave", role="admin").status_code == 403, "only owners grant admin"
    assert add_member(client, carol, team, "dave").status_code == 201, "admins can add members"
    assert add_member(client, user_headers, team, "dave").status_code == 409
    assert add_member(client, user_headers, team, "nobody").status_code == 404
    assert add_member(client, user_headers, team, "dave", role="owner").status_code == 422
    assert client.delete(f"/workspaces/{team}/members/{alice_id}", headers=carol).status_code == 400


def test_upload_needs_global_admin_and_workspace_admin(client, make_user, admin_headers, user_headers):
    team = new_workspace(client, user_headers)
    add_member(client, user_headers, team, "admin_user", role="member")

    as_plain_member = upload(client, in_workspace(admin_headers, team), "app.log", SAMPLE_LOG)
    as_non_admin_owner = upload(client, in_workspace(user_headers, team), "app.log", SAMPLE_LOG)
    in_own_workspace = upload(client, admin_headers, "app.log", SAMPLE_LOG)

    assert as_plain_member.status_code == 403
    assert as_non_admin_owner.status_code == 403
    assert in_own_workspace.status_code == 201


def test_documents_and_summaries_are_isolated(client, admin_headers, user_headers):
    document_id = upload(client, admin_headers, "app.log", SAMPLE_LOG).json()["id"]
    upload(client, admin_headers, "database.md", SAMPLE_RUNBOOK)

    assert client.get("/documents", headers=user_headers).json()["count"] == 0
    assert client.get(f"/logs/{document_id}/summary", headers=user_headers).status_code == 404
    assert client.get("/search/runbooks", headers=user_headers, params={"query": "database"}).json()["results"] == []


def test_feedback_on_another_workspaces_answer_is_rejected(client, user_headers, other_user_headers):
    incident = create_incident(client, user_headers)
    question_log_id = ask(client, user_headers, incident["id"], "why?").json()["question_log_id"]

    response = client.post(
        "/feedback", headers=other_user_headers, json={"question_log_id": question_log_id, "is_helpful": False}
    )

    assert response.status_code == 404


def test_analytics_only_counts_the_current_workspace(client, user_headers, other_user_headers):
    incident = create_incident(client, user_headers)
    ask(client, user_headers, incident["id"], "first")
    ask(client, user_headers, incident["id"], "second")

    mine = client.get("/analytics/summary", headers=user_headers).json()
    theirs = client.get("/analytics/summary", headers=other_user_headers).json()

    assert mine["total_questions"] == 2
    assert theirs["total_questions"] == 0
    assert theirs["recent_questions"] == []
