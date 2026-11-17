"""Set a new password for an existing user.

Usage (from the project root, e.g. in the Render shell):
    python scripts/set_password.py <username>
    python scripts/set_password.py <username> --remove-admin

The password is read interactively so it never appears in shell history,
process listings, or logs. Changing BOOTSTRAP_ADMIN_PASSWORD does not rotate an
existing admin, because bootstrap only creates the account once.

Tokens issued before the change stay valid until they expire
(ACCESS_TOKEN_EXPIRE_MINUTES).
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from passlib.context import CryptContext
from sqlalchemy.orm import Session

import models
from database import SessionLocal

MIN_PASSWORD_LENGTH = 12

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def set_password(db: Session, username: str, new_password: str, remove_admin: bool = False) -> bool:
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        return False

    user.hashed_password = pwd_context.hash(new_password)
    if remove_admin:
        user.is_admin = False
    db.commit()
    return True


def prompt_for_password() -> str | None:
    password = getpass.getpass("New password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        return None
    if getpass.getpass("Repeat new password: ") != password:
        print("Passwords do not match.")
        return None
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a new password for an existing user.")
    parser.add_argument("username")
    parser.add_argument("--remove-admin", action="store_true", help="also revoke admin rights")
    args = parser.parse_args(argv)

    password = prompt_for_password()
    if password is None:
        return 1

    db = SessionLocal()
    try:
        if not set_password(db, args.username, password, remove_admin=args.remove_admin):
            print(f"No user named '{args.username}'.")
            return 1
    finally:
        db.close()

    message = f"Password updated for '{args.username}'."
    if args.remove_admin:
        message += " Admin rights removed."
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
