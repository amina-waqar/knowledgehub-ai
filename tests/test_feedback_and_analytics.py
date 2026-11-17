from conftest import ask, create_incident


def test_health_check(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["llm_provider"] == "mock"


def test_feedback_is_linked_to_the_question(client, user_headers):
    incident = create_incident(client, user_headers)
    question_log_id = ask(client, user_headers, incident["id"], "what now").json()["question_log_id"]

    response = client.post(
        "/feedback",
        headers=user_headers,
        json={"question_log_id": question_log_id, "is_helpful": True, "feedback_text": "useful"},
    )

    assert response.status_code == 201
    assert response.json()["question_log_id"] == question_log_id
    assert response.json()["is_helpful"] is True


def test_feedback_for_unknown_question_returns_404(client, user_headers):
    response = client.post(
        "/feedback", headers=user_headers, json={"question_log_id": 999, "is_helpful": False}
    )
    assert response.status_code == 404


def test_feedback_requires_login(client):
    response = client.post("/feedback", json={"question_log_id": 1, "is_helpful": True})
    assert response.status_code == 401


def test_analytics_summary_counts_questions_and_feedback(client, user_headers):
    incident = create_incident(client, user_headers)
    first = ask(client, user_headers, incident["id"], "first question").json()
    ask(client, user_headers, incident["id"], "second question")
    client.post(
        "/feedback",
        headers=user_headers,
        json={"question_log_id": first["question_log_id"], "is_helpful": True},
    )

    summary = client.get("/analytics/summary", headers=user_headers).json()

    assert summary["total_questions"] == 2
    assert summary["total_feedback"] == 1
    assert summary["helpful_count"] == 1
    assert summary["helpful_rate"] == 1.0
    assert len(summary["recent_questions"]) == 2


def test_analytics_requires_login(client):
    assert client.get("/analytics/summary").status_code == 401
