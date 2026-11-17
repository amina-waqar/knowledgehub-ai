import os
import re
import zlib

# config.py reads the environment (and .env) at import time, so the test
# settings must be in place before any app module is imported. Values set here
# win over .env because load_dotenv() does not override existing variables,
# which keeps the real bootstrap/demo credentials out of the test run.
TEST_ENV = {
    "SECRET_KEY": "test-only-secret-key",
    "DATABASE_URL": "sqlite:///:memory:",
    "USE_PGVECTOR": "false",
    "LLM_PROVIDER": "mock",
    "LLM_MODEL": "",
    "LLM_BASE_URL": "",
    "LLM_API_KEY": "",
    "ENABLE_DEBUG_ENDPOINTS": "false",
    "BOOTSTRAP_ADMIN_USERNAME": "",
    "BOOTSTRAP_ADMIN_EMAIL": "",
    "BOOTSTRAP_ADMIN_PASSWORD": "",
    "DEMO_USERNAME": "",
    "DEMO_EMAIL": "",
    "DEMO_PASSWORD": "",
    "CORS_ORIGINS": "",
}
os.environ.update(TEST_ENV)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
from config import settings
from database import Base, get_database
from workspaces import create_personal_workspace

TEST_PASSWORD = "correct-horse-battery"

SAMPLE_LOG = (
    b"2024-06-01T10:42:11Z INFO User 123 logged in\n"
    b"2024-06-01T10:44:03Z ERROR Database timeout on query xyz\n"
    b"\n"
    b"malformed line without timestamp\n"
    b"2024-06-01T10:45:00Z WARN Slow response from Redis\n"
)

SAMPLE_RUNBOOK = (
    b"# Database runbook\n\n"
    b"Database timeouts are usually caused by connection pool exhaustion.\n\n"
    b"Restart the affected application workers if the pool is above 90 percent.\n"
)


def fake_embedding(text: str) -> list[float]:
    """Bag-of-words vector, so texts sharing words still score as similar
    without loading the real sentence-transformers model."""
    vector = [0.0] * settings.EMBEDDING_DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(word.encode()) % len(vector)] += 1.0
    return vector


def _model_must_not_load():
    raise AssertionError("Tests must not load the real embedding model.")


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def upload_dir(tmp_path):
    path = tmp_path / "uploads"
    path.mkdir()
    return path


@pytest.fixture
def client(session_factory, upload_dir, monkeypatch):
    def get_test_database():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(main, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(main, "generate_embedding", fake_embedding)
    monkeypatch.setattr(main, "get_embedding_model", _model_must_not_load)
    main.app.dependency_overrides[get_database] = get_test_database

    # Not used as a context manager on purpose: that would run the app lifespan,
    # which connects to the configured database and warms the real model.
    yield TestClient(main.app)

    main.app.dependency_overrides.clear()


@pytest.fixture
def make_user(db):
    def _make_user(username: str, is_admin: bool = False) -> models.User:
        user = models.User(
            username=username,
            email=f"{username}@example.com",
            hashed_password=main.get_password_hash(TEST_PASSWORD),
            is_admin=is_admin,
        )
        db.add(user)
        db.flush()
        create_personal_workspace(db, user)
        db.commit()
        db.refresh(user)
        return user

    return _make_user


def login(client: TestClient, username: str, password: str = TEST_PASSWORD) -> dict:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def user_headers(client, make_user):
    make_user("alice")
    return login(client, "alice")


@pytest.fixture
def other_user_headers(client, make_user):
    make_user("bob")
    return login(client, "bob")


@pytest.fixture
def admin_headers(client, make_user):
    make_user("admin_user", is_admin=True)
    return login(client, "admin_user")


def upload(client: TestClient, headers: dict, filename: str, content: bytes, title: str = "Test file"):
    return client.post(
        "/admin/upload",
        headers=headers,
        data={"title": title},
        files={"file": (filename, content, "application/octet-stream")},
    )


def create_incident(client: TestClient, headers: dict, **fields) -> dict:
    payload = {"title": "Checkout API timeouts", "description": "Requests failing", **fields}
    response = client.post("/incidents", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def ask(client: TestClient, headers: dict, incident_id: int, question: str):
    return client.post(
        f"/incidents/{incident_id}/ask", headers=headers, data={"question": question}
    )
