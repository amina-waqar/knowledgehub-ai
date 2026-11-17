import pytest

import main as app_main
from conftest import TEST_PASSWORD, login
from scripts import set_password as script

NEW_PASSWORD = "a-brand-new-passphrase"


@pytest.fixture
def script_db(session_factory, monkeypatch):
    monkeypatch.setattr(script, "SessionLocal", session_factory)


def fake_prompts(monkeypatch, *answers):
    replies = iter(answers)
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": next(replies))


def test_rotated_password_replaces_the_old_one(client, make_user, script_db, monkeypatch, capsys):
    make_user("admin_user", is_admin=True)
    fake_prompts(monkeypatch, NEW_PASSWORD, NEW_PASSWORD)

    assert script.main(["admin_user"]) == 0

    old_login = client.post("/auth/login", json={"username": "admin_user", "password": TEST_PASSWORD})
    assert old_login.status_code == 401
    login(client, "admin_user", NEW_PASSWORD)
    assert NEW_PASSWORD not in capsys.readouterr().out


def test_remove_admin_flag_revokes_admin(client, db, make_user, script_db, monkeypatch):
    user = make_user("admin_user", is_admin=True)
    fake_prompts(monkeypatch, NEW_PASSWORD, NEW_PASSWORD)

    assert script.main(["admin_user", "--remove-admin"]) == 0

    db.refresh(user)
    assert user.is_admin is False
    assert app_main.verify_password(NEW_PASSWORD, user.hashed_password)


@pytest.mark.parametrize(
    "answers",
    [("short", "short"), (NEW_PASSWORD, NEW_PASSWORD + "x")],
    ids=["too-short", "mismatch"],
)
def test_bad_input_leaves_password_unchanged(db, make_user, script_db, monkeypatch, answers):
    user = make_user("admin_user", is_admin=True)
    original_hash = user.hashed_password
    fake_prompts(monkeypatch, *answers)

    assert script.main(["admin_user"]) == 1

    db.refresh(user)
    assert user.hashed_password == original_hash
    assert user.is_admin is True


def test_unknown_user_is_reported(script_db, monkeypatch, capsys):
    fake_prompts(monkeypatch, NEW_PASSWORD, NEW_PASSWORD)

    assert script.main(["ghost"]) == 1
    assert "No user named 'ghost'" in capsys.readouterr().out
