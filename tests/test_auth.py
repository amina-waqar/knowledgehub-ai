from datetime import timedelta

from jose import jwt

import main
from config import settings
from conftest import TEST_PASSWORD, login, upload


def test_test_run_does_not_use_real_credentials():
    assert settings.SECRET_KEY == "test-only-secret-key"
    assert settings.BOOTSTRAP_ADMIN_PASSWORD == ""
    assert settings.DEMO_PASSWORD == ""
    assert settings.LLM_PROVIDER == "mock"


def test_signup_creates_non_admin_user(client):
    response = client.post(
        "/auth/signup",
        json={"username": "carol", "email": "carol@example.com", "password": TEST_PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "carol"
    assert body["is_admin"] is False
    assert "password" not in body
    assert "hashed_password" not in body


def test_signup_rejects_duplicate_username_or_email(client, make_user):
    make_user("alice")

    same_username = client.post(
        "/auth/signup",
        json={"username": "alice", "email": "new@example.com", "password": TEST_PASSWORD},
    )
    same_email = client.post(
        "/auth/signup",
        json={"username": "new", "email": "alice@example.com", "password": TEST_PASSWORD},
    )

    assert same_username.status_code == 400
    assert same_email.status_code == 400


def test_signup_rejects_invalid_email(client):
    response = client.post(
        "/auth/signup",
        json={"username": "carol", "email": "not-an-email", "password": TEST_PASSWORD},
    )
    assert response.status_code == 422


def test_login_returns_bearer_token(client, make_user):
    user = make_user("alice")

    response = client.post("/auth/login", json={"username": "alice", "password": TEST_PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    claims = jwt.decode(body["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert claims["sub"] == "alice"
    assert claims["user_id"] == user.id


def test_login_gives_same_error_for_wrong_password_and_unknown_user(client, make_user):
    make_user("alice")

    wrong_password = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    unknown_user = client.post("/auth/login", json={"username": "nobody", "password": "wrong"})

    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_me_returns_current_user(client, user_headers):
    response = client.get("/me", headers=user_headers)

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert response.json()["is_admin"] is False


def test_me_requires_token(client):
    assert client.get("/me").status_code == 401


def test_me_rejects_token_signed_with_another_key(client, make_user):
    user = make_user("alice")
    forged = jwt.encode({"sub": "alice", "user_id": user.id}, "some-other-key", algorithm="HS256")

    response = client.get("/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


def test_me_rejects_expired_token(client, make_user):
    user = make_user("alice")
    expired = main.create_access_token(
        {"sub": "alice", "user_id": user.id}, expires_delta=timedelta(minutes=-1)
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


def test_me_rejects_token_for_deleted_user(client, db, make_user):
    user = make_user("alice")
    headers = login(client, "alice")
    db.delete(user)
    db.commit()

    assert client.get("/me", headers=headers).status_code == 401


def test_me_rejects_token_whose_username_does_not_match_user_id(client, make_user):
    alice = make_user("alice")
    make_user("bob")
    mismatched = main.create_access_token({"sub": "bob", "user_id": alice.id})

    response = client.get("/me", headers={"Authorization": f"Bearer {mismatched}"})

    assert response.status_code == 401


def test_admin_claim_in_token_is_not_trusted(client, make_user):
    user = make_user("alice")
    token = main.create_access_token({"sub": "alice", "user_id": user.id, "is_admin": True})
    headers = {"Authorization": f"Bearer {token}"}

    response = upload(client, headers, "app.log", b"2024-06-01T10:00:00Z INFO ok\n")

    assert response.status_code == 403
