"""Upgrading a database created before workspaces existed must keep its data visible."""
import sqlalchemy as sa

import migrations
import models
from database import Base, SessionLocal, engine


def test_old_schema_is_upgraded_and_backfilled():
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, email VARCHAR, hashed_password VARCHAR, is_admin BOOLEAN, created_at TIMESTAMP)"))
        conn.execute(sa.text("CREATE TABLE incidents (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, description TEXT, status VARCHAR NOT NULL, severity VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, started_at TIMESTAMP, resolved_at TIMESTAMP, creator_id INTEGER)"))
        conn.execute(sa.text("CREATE TABLE documents (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, file_path VARCHAR NOT NULL, type VARCHAR NOT NULL, status VARCHAR NOT NULL, uploader_id INTEGER, uploaded_at TIMESTAMP)"))
        conn.execute(sa.text("CREATE TABLE log_entries (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, incident_id INTEGER, timestamp TIMESTAMP, level VARCHAR, message TEXT NOT NULL, raw TEXT, numeric_value FLOAT, created_at TIMESTAMP)"))
        conn.execute(sa.text("INSERT INTO users (id, username, email, hashed_password, is_admin) VALUES (1,'old','o@x.com','h',false)"))
        conn.execute(sa.text("INSERT INTO incidents (id, title, status, creator_id) VALUES (1,'old incident','open',1)"))
    models.Workspace.__table__.create(bind=engine)
    models.WorkspaceMember.__table__.create(bind=engine)

    migrations.add_missing_columns()
    migrations.add_missing_columns()  # idempotent
    migrations.backfill_default_workspace()
    migrations.backfill_default_workspace()

    with SessionLocal() as db:
        user = db.query(models.User).one()
        incident = db.query(models.Incident).one()
        assert user.default_workspace_id is not None
        assert incident.workspace_id == user.default_workspace_id
        assert incident.source == "manual"
        assert db.query(models.Workspace).count() == 1
        assert db.query(models.WorkspaceMember).count() == 1
