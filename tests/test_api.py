import io
import json
import pathlib
from datetime import datetime, timedelta, timezone

import models
import main
from config import settings
from database import SessionLocal
from tests.conftest import auth, make_user

SAMPLE = pathlib.Path(__file__).resolve().parent.parent / "sample_data" / "incident_checkout_outage.jsonl"
STARTED = "2026-01-15T14:20:00Z"


def create_incident(client, h, **over):
    body = {"title": "Checkout failing: payment timeouts", "service": "checkout-api",
            "started_at": STARTED, **over}
    r = client.post("/incidents", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def upload(client, h, incident_id, content, name="logs.jsonl"):
    return client.post(f"/incidents/{incident_id}/logs", headers=h,
                       files={"file": (name, io.BytesIO(content), "text/plain")})


# ---------------------------------------------------------------- workflow
def test_end_to_end_create_upload_match(client):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    assert inc["service"] == "checkout-api" and inc["started_at"].startswith("2026-01-15T14:20")

    up = upload(client, h, inc["id"], SAMPLE.read_bytes())
    assert up.status_code == 201, up.text
    body = up.json()
    assert body["log_entries_created"] == 51 and body["entries_with_timestamp"] == 51
    assert "payments-svc" in body["services_detected"]

    r = client.get(f"/incidents/{inc['id']}/logs", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["window"]["applied"] and data["clusters"]
    top = data["clusters"][0]
    assert top["why"] and top["score_breakdown"] and top["examples"]
    joined = " ".join(c["message"].lower() for c in data["clusters"][:5])
    assert "database timeout" in joined

    cid = top["cluster_id"]
    d = client.get(f"/incidents/{inc['id']}/logs/clusters/{cid}/entries", headers=h)
    assert d.status_code == 200 and d.json()["count"] == top["occurrence_count"]


def test_plain_text_logs_and_keyword_fallback_without_started_at(client):
    make_user("alice")
    h = auth(client, "alice")
    r = client.post("/incidents", json={"title": "Database timeout errors"}, headers=h)
    inc = r.json()
    # default anchor is "now", so historical logs fall outside the window...
    text = b"2024-06-01T10:44:03Z ERROR Database timeout on query xyz\n2024-06-01T10:42:11Z INFO User 123 logged in\n"
    assert upload(client, h, inc["id"], text, "app.log").status_code == 201
    far = client.get(f"/incidents/{inc['id']}/logs", headers=h).json()
    assert far["clusters"] == []
    # ...and an explicit started_at near them finds the match.
    with SessionLocal() as db:
        i = db.get(models.Incident, inc["id"])
        i.started_at = datetime(2024, 6, 1, 10, 44, tzinfo=timezone.utc)
        db.commit()
    near = client.get(f"/incidents/{inc['id']}/logs", headers=h).json()
    assert near["clusters"] and "timeout" in near["clusters"][0]["matching_terms"]


def test_duplicate_upload_is_not_reingested(client):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    first = upload(client, h, inc["id"], SAMPLE.read_bytes()).json()
    second = upload(client, h, inc["id"], SAMPLE.read_bytes()).json()
    assert second["duplicate"] is True and second["id"] == first["id"]
    with SessionLocal() as db:
        assert db.query(models.LogEntry).count() == 51


# --------------------------------------------------------------- isolation
def test_other_workspace_cannot_see_or_touch_incident_or_logs(client):
    make_user("alice")
    make_user("bob")
    ha, hb = auth(client, "alice"), auth(client, "bob")
    inc = create_incident(client, ha)
    doc = upload(client, ha, inc["id"], SAMPLE.read_bytes()).json()

    assert client.get(f"/incidents/{inc['id']}", headers=hb).status_code == 404
    assert client.get(f"/incidents/{inc['id']}/logs", headers=hb).status_code == 404
    assert upload(client, hb, inc["id"], b"x").status_code == 404
    assert client.post(f"/incidents/{inc['id']}/ask", headers=hb, data={"question": "hi"}).status_code == 404
    assert client.get("/incidents", headers=hb).json()["count"] == 0
    assert client.get("/documents", headers=hb).json()["count"] == 0
    assert client.get(f"/logs/{doc['id']}/summary", headers=hb).status_code == 404
    # Bob's own incident must not match Alice's logs.
    binc = create_incident(client, hb)
    assert client.get(f"/incidents/{binc['id']}/logs", headers=hb).json()["clusters"] == []
    assert client.get("/analytics/summary", headers=hb).json()["total_log_entries"] == 0


def test_feedback_cannot_target_other_workspace(client):
    make_user("alice")
    make_user("bob")
    ha, hb = auth(client, "alice"), auth(client, "bob")
    inc = create_incident(client, ha)
    ask = client.post(f"/incidents/{inc['id']}/ask", headers=ha, data={"question": "what happened?"})
    assert ask.status_code == 200, ask.text
    qid = ask.json()["question_log_id"]
    assert client.post("/feedback", headers=hb, json={"question_log_id": qid, "is_helpful": True}).status_code == 404
    assert client.post("/feedback", headers=ha, json={"question_log_id": qid, "is_helpful": True}).status_code == 201


def test_admin_shared_logs_visible_to_workspaces_but_not_user_logs_to_each_other(client):
    make_user("admin", admin=True)
    make_user("alice")
    hadm, ha = auth(client, "admin"), auth(client, "alice")
    r = client.post("/admin/upload", headers=hadm, data={"title": "shared"},
                    files={"file": ("shared.jsonl", io.BytesIO(SAMPLE.read_bytes()), "text/plain")})
    assert r.status_code == 201, r.text
    inc = create_incident(client, ha)
    assert client.get(f"/incidents/{inc['id']}/logs", headers=ha).json()["clusters"]


def test_ask_uses_new_matcher_and_keeps_response_shape(client):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    upload(client, h, inc["id"], SAMPLE.read_bytes())
    r = client.post(f"/incidents/{inc['id']}/ask", headers=h, data={"question": "why are payments timing out?"})
    body = r.json()
    assert r.status_code == 200
    assert body["retrieval"]["historical_log_patterns_used"] > 0
    assert body["historical_log_patterns"][0]["why"]


# ------------------------------------------------------------ permissions
def test_regular_user_cannot_use_admin_upload_or_admin_endpoints(client):
    make_user("alice")
    h = auth(client, "alice")
    assert client.post("/admin/upload", headers=h, data={"title": "x"},
                       files={"file": ("a.log", io.BytesIO(b"x"), "text/plain")}).status_code == 403
    assert client.get("/admin/storage", headers=h).status_code == 403
    assert client.post("/admin/maintenance/purge", headers=h).status_code == 403


def test_endpoints_require_auth(client):
    for method, path in [("get", "/incidents"), ("get", "/logs/1/summary"),
                         ("get", "/incidents/1/logs"), ("get", "/analytics/summary")]:
        assert getattr(client, method)(path).status_code in (401, 403)


# ---------------------------------------------------------- signup & limits
def test_signup_disabled_by_default(client):
    assert settings.SIGNUP_ENABLED is False
    r = client.post("/auth/signup", json={"username": "newbie", "email": "n@example.com", "password": "Password-12345"})
    assert r.status_code == 403
    assert client.get("/health").json()["signup_enabled"] is False


def test_signup_when_enabled_gives_private_workspace_and_no_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "SIGNUP_ENABLED", True)
    for name in ("u1", "u2"):
        r = client.post("/auth/signup", json={"username": name + "xx", "email": f"{name}@example.com", "password": "Password-12345"})
        assert r.status_code == 201 and r.json()["is_admin"] is False
    me1 = client.get("/me", headers=auth(client, "u1xx")).json()
    me2 = client.get("/me", headers=auth(client, "u2xx")).json()
    assert me1["workspace_id"] and me1["workspace_id"] != me2["workspace_id"]
    inc = create_incident(client, auth(client, "u1xx"))
    assert client.get(f"/incidents/{inc['id']}", headers=auth(client, "u2xx")).status_code == 404


def test_signup_validation_and_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "SIGNUP_ENABLED", True)
    assert client.post("/auth/signup", json={"username": "ab", "email": "a@example.com", "password": "Password-12345"}).status_code == 422
    assert client.post("/auth/signup", json={"username": "abcd", "email": "a@example.com", "password": "short"}).status_code == 422
    codes = [client.post("/auth/signup", json={"username": f"user{i}", "email": f"u{i}@example.com", "password": "Password-12345"}).status_code for i in range(7)]
    assert codes[:5] == [201] * 5 and codes[5] == 429


def test_login_rate_limit(client):
    make_user("alice")
    codes = [client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


# ------------------------------------------------------- upload protections
def test_upload_rejections(client, monkeypatch):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    assert upload(client, h, inc["id"], b"x", "evil.exe").status_code == 400
    assert upload(client, h, inc["id"], b"x", "../../etc/passwd").status_code == 400
    assert upload(client, h, inc["id"], b"a\x00b\x00" * 10, "bin.log").status_code == 415
    assert upload(client, h, inc["id"], b"\n\n", "empty.log").status_code == 422
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)
    assert upload(client, h, inc["id"], b"2026-01-15T14:20:00Z INFO hi\n", "big.log").status_code == 413


def test_line_and_workspace_quota(client, monkeypatch):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    monkeypatch.setattr(settings, "MAX_LINES_PER_UPLOAD", 2)
    assert upload(client, h, inc["id"], b"a\nb\nc\n", "x.log").status_code == 413
    monkeypatch.setattr(settings, "MAX_LINES_PER_UPLOAD", 100)
    monkeypatch.setattr(settings, "MAX_LOG_ENTRIES_PER_WORKSPACE", 3)
    assert upload(client, h, inc["id"], b"a\nb\n", "ok.log").status_code == 201
    assert upload(client, h, inc["id"], b"c\nd\n", "over.log").status_code == 413


def test_storage_cap_blocks_upload(client, monkeypatch):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    monkeypatch.setattr(main, "database_size_mb", lambda: 999.0)
    r = upload(client, h, inc["id"], b"2026-01-15T14:20:00Z INFO hi\n", "x.log")
    assert r.status_code == 507


def test_upload_rate_limit(client):
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    codes = [upload(client, h, inc["id"], f"2026-01-15T14:20:00Z INFO line {i}\n".encode(), f"f{i}.log").status_code for i in range(22)]
    assert codes[:20] == [201] * 20 and codes[20] == 429


def test_filename_never_used_on_disk(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    assert upload(client, h, inc["id"], b"2026-01-15T14:20:00Z INFO hi\n", "ok.log").status_code == 201
    assert list(tmp_path.iterdir()) == []  # nothing written to disk
    with SessionLocal() as db:
        assert db.query(models.Document).one().file_path == "(not retained)"


# ----------------------------------------------------------------- retention
def test_retention_purges_old_rows_only(client):
    import retention
    make_user("alice")
    h = auth(client, "alice")
    inc = create_incident(client, h)
    upload(client, h, inc["id"], SAMPLE.read_bytes())
    with SessionLocal() as db:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        entries = db.query(models.LogEntry).limit(10).all()
        for e in entries:
            e.created_at = old
        db.commit()
    out = retention.purge_old_logs(retention_days=14)
    assert out["entries_deleted"] == 10
    with SessionLocal() as db:
        assert db.query(models.LogEntry).count() == 41
    assert retention.purge_old_logs(retention_days=0)["enabled"] is False


def test_retention_never_purges_shared_or_pre_workspace_logs(client):
    """Shared logs (workspace_id NULL) include all data from before workspaces existed."""
    import retention
    make_user("admin", admin=True)
    r = client.post("/admin/upload", headers=auth(client, "admin"), data={"title": "shared"},
                    files={"file": ("shared.jsonl", io.BytesIO(SAMPLE.read_bytes()), "text/plain")})
    assert r.status_code == 201, r.text
    old = datetime.now(timezone.utc) - timedelta(days=400)
    with SessionLocal() as db:
        db.query(models.LogEntry).update({"created_at": old})
        db.query(models.Document).update({"uploaded_at": old})
        db.commit()
    out = retention.purge_old_logs(retention_days=14)
    assert out["entries_deleted"] == 0 and out["documents_deleted"] == 0
    with SessionLocal() as db:
        assert db.query(models.LogEntry).count() == 51
        assert db.query(models.Document).count() == 1


def test_admin_storage_endpoint(client):
    make_user("admin", admin=True)
    r = client.get("/admin/storage", headers=auth(client, "admin"))
    assert r.status_code == 200 and r.json()["retention_days"] == settings.LOG_RETENTION_DAYS

