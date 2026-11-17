from conftest import SAMPLE_LOG, SAMPLE_RUNBOOK, upload


def test_upload_requires_admin(client, user_headers):
    response = upload(client, user_headers, "app.log", SAMPLE_LOG)
    assert response.status_code == 403


def test_upload_requires_login(client):
    response = client.post(
        "/admin/upload",
        data={"title": "x"},
        files={"file": ("app.log", SAMPLE_LOG, "text/plain")},
    )
    assert response.status_code == 401


def test_log_upload_parses_lines_and_skips_blank_ones(client, admin_headers, upload_dir):
    response = upload(client, admin_headers, "app.log", SAMPLE_LOG, title="App log")

    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "log"
    assert body["status"] == "processed"
    assert body["log_entries_created"] == 4
    assert len(list(upload_dir.iterdir())) == 1


def test_log_summary_counts_levels_and_time_range(client, admin_headers):
    document_id = upload(client, admin_headers, "app.log", SAMPLE_LOG).json()["id"]

    response = client.get(f"/logs/{document_id}/summary", headers=admin_headers)

    assert response.status_code == 200
    summary = response.json()
    assert summary["total_log_entries"] == 4
    assert summary["level_counts"] == {"INFO": 1, "ERROR": 1, "WARN": 1, "UNKNOWN_LEVEL": 1}
    assert summary["first_log_entry_at"].startswith("2024-06-01T10:42:11")
    assert summary["last_log_entry_at"].startswith("2024-06-01T10:45:00")


def test_log_summary_rejects_missing_and_non_log_documents(client, admin_headers):
    runbook_id = upload(client, admin_headers, "guide.md", SAMPLE_RUNBOOK).json()["id"]

    assert client.get("/logs/999/summary", headers=admin_headers).status_code == 404
    assert client.get(f"/logs/{runbook_id}/summary", headers=admin_headers).status_code == 400


def test_log_summary_requires_login(client, admin_headers):
    document_id = upload(client, admin_headers, "app.log", SAMPLE_LOG).json()["id"]

    response = client.get(f"/logs/{document_id}/summary")

    assert response.status_code == 401


def test_runbook_upload_creates_embedded_chunks(client, admin_headers):
    response = upload(client, admin_headers, "database.md", SAMPLE_RUNBOOK, title="DB runbook")

    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "runbook"
    assert body["status"] == "processed"
    assert body["chunk_count"] >= 1


def test_empty_runbook_is_rejected(client, admin_headers):
    response = upload(client, admin_headers, "empty.txt", b"   \n\n")
    assert response.status_code == 422


def test_unsupported_file_type_is_rejected(client, admin_headers, upload_dir):
    response = upload(client, admin_headers, "tool.exe", b"MZ\x00\x00")

    assert response.status_code == 400
    assert list(upload_dir.iterdir()) == []


def test_documents_list_and_type_filter(client, admin_headers):
    upload(client, admin_headers, "app.log", SAMPLE_LOG, title="App log")
    upload(client, admin_headers, "database.md", SAMPLE_RUNBOOK, title="DB runbook")

    everything = client.get("/documents", headers=admin_headers).json()
    runbooks = client.get("/documents", headers=admin_headers, params={"type": "runbook"}).json()

    assert everything["count"] == 2
    assert [d["title"] for d in runbooks["documents"]] == ["DB runbook"]
    assert runbooks["documents"][0]["chunk_count"] >= 1


def test_documents_list_requires_login(client):
    assert client.get("/documents").status_code == 401
