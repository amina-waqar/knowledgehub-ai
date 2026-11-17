import pytest

from conftest import create_incident


def test_create_incident_requires_login(client):
    response = client.post("/incidents", json={"title": "Outage"})
    assert response.status_code == 401


def test_create_incident_records_creator_and_defaults(client, user_headers):
    me = client.get("/me", headers=user_headers).json()

    incident = create_incident(client, user_headers, severity="high")

    assert incident["title"] == "Checkout API timeouts"
    assert incident["severity"] == "high"
    assert incident["status"] == "open"
    assert incident["creator_id"] == me["id"]
    assert incident["created_at"] is not None


@pytest.mark.parametrize("title", ["", "x" * 301])
def test_create_incident_validates_title_length(client, user_headers, title):
    response = client.post("/incidents", headers=user_headers, json={"title": title})
    assert response.status_code == 422


def test_list_incidents_returns_created_incidents(client, user_headers):
    first = create_incident(client, user_headers, title="First")
    second = create_incident(client, user_headers, title="Second")

    response = client.get("/incidents", headers=user_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {i["id"] for i in body["incidents"]} == {first["id"], second["id"]}


def test_list_incidents_respects_limit(client, user_headers):
    for n in range(3):
        create_incident(client, user_headers, title=f"Incident {n}")

    response = client.get("/incidents", headers=user_headers, params={"limit": 2})

    assert response.json()["count"] == 2


def test_get_incident_by_id(client, user_headers):
    incident = create_incident(client, user_headers)

    response = client.get(f"/incidents/{incident['id']}", headers=user_headers)

    assert response.status_code == 200
    assert response.json() == incident


def test_get_missing_incident_returns_404(client, user_headers):
    assert client.get("/incidents/999", headers=user_headers).status_code == 404


def test_incident_endpoints_require_login(client):
    assert client.get("/incidents").status_code == 401
    assert client.get("/incidents/1").status_code == 401


def test_user_cannot_read_another_users_incident(client, user_headers, other_user_headers):
    incident = create_incident(client, user_headers)

    response = client.get(f"/incidents/{incident['id']}", headers=other_user_headers)

    assert response.status_code == 404


def test_incident_list_excludes_other_users_incidents(client, user_headers, other_user_headers):
    create_incident(client, user_headers)

    response = client.get("/incidents", headers=other_user_headers)

    assert response.json()["count"] == 0
