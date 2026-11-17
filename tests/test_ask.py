from conftest import SAMPLE_LOG, SAMPLE_RUNBOOK, ask, create_incident, upload


def test_answer_is_grounded_when_a_runbook_matches(client, admin_headers):
    upload(client, admin_headers, "database.md", SAMPLE_RUNBOOK, title="DB runbook")
    incident = create_incident(client, admin_headers)

    response = ask(client, admin_headers, incident["id"], "database timeouts connection pool")

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is True
    assert body["citations"][0]["document_title"] == "DB runbook"
    assert body["llm"]["provider"] == "mock"
    assert body["llm"]["degraded"] is False
    assert body["question_log_id"] > 0


def test_answer_is_ungrounded_without_runbooks(client, user_headers):
    incident = create_incident(client, user_headers)

    body = ask(client, user_headers, incident["id"], "why is checkout slow").json()

    assert body["grounded"] is False
    assert body["citations"] == []


def test_matching_log_patterns_are_included(client, admin_headers):
    upload(client, admin_headers, "app.log", SAMPLE_LOG)
    incident = create_incident(client, admin_headers, title="Database timeout", description=None)

    body = ask(client, admin_headers, incident["id"], "what happened to the database").json()

    messages = [p["message"] for p in body["historical_log_patterns"]]
    assert "Database timeout on query xyz" in messages
    assert body["retrieval"]["historical_log_patterns_used"] == len(messages)


def test_answers_never_use_another_workspaces_runbooks_or_logs(client, admin_headers, user_headers):
    upload(client, admin_headers, "database.md", SAMPLE_RUNBOOK, title="DB runbook")
    upload(client, admin_headers, "app.log", SAMPLE_LOG)
    incident = create_incident(client, user_headers, title="Database timeout")

    body = ask(client, user_headers, incident["id"], "database timeouts connection pool").json()

    assert body["grounded"] is False
    assert body["citations"] == []
    assert body["historical_log_patterns"] == []


def test_blank_question_is_rejected(client, user_headers):
    incident = create_incident(client, user_headers)
    assert ask(client, user_headers, incident["id"], "   ").status_code == 400


def test_question_about_missing_incident_returns_404(client, user_headers):
    assert ask(client, user_headers, 999, "anything").status_code == 404


def test_ask_requires_login(client):
    response = client.post("/incidents/1/ask", data={"question": "anything"})
    assert response.status_code == 401
