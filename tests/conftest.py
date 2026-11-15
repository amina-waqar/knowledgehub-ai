import os
import tempfile

# Must be set before config/main are imported.
_DB = os.path.join(tempfile.mkdtemp(), "test.db")
_TEST_URL = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_DB}")

# The fixtures DROP ALL TABLES, so only ever allow SQLite or a local Postgres.
# This makes it impossible to point the suite at Neon (or any remote DB) by mistake.
if not _TEST_URL.startswith("sqlite"):
    _host = _TEST_URL.split("@")[-1].split("/")[0].split(":")[0].split("?")[0]
    assert _host in ("localhost", "127.0.0.1", ""), (
        f"Refusing to run tests against non-local database host '{_host}'."
    )
os.environ["DATABASE_URL"] = _TEST_URL
os.environ["USE_PGVECTOR"] = os.environ.get("TEST_USE_PGVECTOR", "false")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("BOOTSTRAP_ADMIN_USERNAME", None)

import pytest
from fastapi.testclient import TestClient

import main
import models
from database import Base, SessionLocal, engine
from ratelimit import limiter


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    limiter.reset()
    yield


@pytest.fixture(autouse=True)
def no_embedding_model(monkeypatch):
    """Tests never download or load the sentence-transformers model."""
    monkeypatch.setattr(main, "generate_embedding", lambda text: [0.0] * 384)


@pytest.fixture
def client():
    # No `with`: skips lifespan, which would load the embedding model.
    return TestClient(main.app)


def make_user(username, *, admin=False, workspace=True, password="Password-12345"):
    db = SessionLocal()
    try:
        user = models.User(username=username, email=f"{username}@example.com",
                           hashed_password=main.get_password_hash(password), is_admin=admin)
        db.add(user)
        db.flush()
        if workspace:
            ws = models.Workspace(name=f"{username} ws")
            db.add(ws)
            db.flush()
            db.add(models.WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
            user.default_workspace_id = ws.id
        db.commit()
        return user.id
    finally:
        db.close()


def auth(client, username, password="Password-12345"):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
