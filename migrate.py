"""Bring the database schema up to date. Run this before starting the app:

    python migrate.py

Databases created before Alembic (by Base.metadata.create_all) have no
version record. They are checked against the baseline schema and stamped at
0001_baseline without touching any rows, then upgraded like any other database.
"""
import os
import sys

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from config import settings
from database import wait_for_database

BASELINE_REVISION = "0001_baseline"
ALEMBIC_INI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic.ini")

BASELINE_SCHEMA = {
    "users": {"id", "username", "email", "hashed_password", "is_admin", "created_at"},
    "documents": {"id", "title", "file_path", "type", "status", "uploader_id", "uploaded_at"},
    "document_chunks": {"id", "document_id", "chunk_text", "chunk_order", "embedding", "embedding_model"},
    "incidents": {
        "id", "title", "description", "status", "severity", "created_at",
        "updated_at", "started_at", "resolved_at", "creator_id",
    },
    "log_entries": {
        "id", "document_id", "incident_id", "timestamp", "level",
        "message", "raw", "numeric_value", "created_at",
    },
    "answer_feedback": {
        "id", "incident_id", "user_id", "question", "answer",
        "is_helpful", "feedback_text", "created_at",
    },
    "question_logs": {"id", "incident_id", "user_id", "question", "ai_answer", "answered_at"},
}


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(ALEMBIC_INI)
    if database_url:
        config.attributes["database_url"] = database_url
    return config


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_up_to_date(engine: Engine) -> bool:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    return current_revision(engine) == head


def missing_baseline_columns(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = []
    for table, columns in BASELINE_SCHEMA.items():
        if table not in tables:
            missing.append(table)
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        missing.extend(f"{table}.{name}" for name in sorted(columns - existing))
    return missing


def migrate(database_url: str | None = None) -> None:
    database_url = database_url or settings.DATABASE_URL
    config = alembic_config(database_url)
    engine = create_engine(database_url)
    try:
        has_app_tables = bool(set(inspect(engine).get_table_names()) & BASELINE_SCHEMA.keys())
        if current_revision(engine) is None and has_app_tables:
            missing = missing_baseline_columns(engine)
            if missing:
                raise RuntimeError(
                    "Existing database does not match the baseline schema, so it was not "
                    "stamped. Missing: " + ", ".join(missing)
                )
            command.stamp(config, BASELINE_REVISION)
            print(f"[migrate] Existing database stamped at {BASELINE_REVISION}.")
        command.upgrade(config, "head")
        print(f"[migrate] Database is at revision {current_revision(engine)}.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        wait_for_database()
        migrate()
    except RuntimeError as exc:
        print(f"[migrate] Failed: {exc}")
        sys.exit(1)
