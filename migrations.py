"""
Small idempotent schema upgrades.

`Base.metadata.create_all()` creates missing tables but never alters existing
ones, so columns and indexes added after the first deploy are applied here.
Every step is safe to run on every startup. This avoids Alembic for now; if the
schema starts changing often, move these steps into Alembic revisions.
"""
from sqlalchemy import inspect, text

from database import engine, SessionLocal
import models

# (table, column, postgres type, sqlite type)
_COLUMNS = [
    ("users", "default_workspace_id", "INTEGER", "INTEGER"),
    ("documents", "workspace_id", "INTEGER", "INTEGER"),
    ("documents", "incident_id", "INTEGER", "INTEGER"),
    ("documents", "size_bytes", "INTEGER", "INTEGER"),
    ("documents", "sha256", "VARCHAR(64)", "VARCHAR(64)"),
    ("incidents", "workspace_id", "INTEGER", "INTEGER"),
    ("incidents", "service", "VARCHAR", "VARCHAR"),
    ("incidents", "source", "VARCHAR NOT NULL DEFAULT 'manual'", "VARCHAR NOT NULL DEFAULT 'manual'"),
    ("log_entries", "workspace_id", "INTEGER", "INTEGER"),
    ("log_entries", "service", "VARCHAR", "VARCHAR"),
    ("log_entries", "trace_id", "VARCHAR", "VARCHAR"),
    ("log_entries", "host", "VARCHAR", "VARCHAR"),
    ("log_entries", "attributes", "JSON", "JSON"),
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_logentry_ws_ts ON log_entries (workspace_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_logentry_ws_svc_ts ON log_entries (workspace_id, service, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_logentry_trace ON log_entries (trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_logentry_document ON log_entries (document_id)",
    "CREATE INDEX IF NOT EXISTS idx_logentry_created ON log_entries (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_documents_workspace_id ON documents (workspace_id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_incident_id ON documents (incident_id)",
    "CREATE INDEX IF NOT EXISTS ix_incidents_workspace_id ON incidents (workspace_id)",
]

DEFAULT_WORKSPACE_NAME = "Default workspace"


def add_missing_columns() -> None:
    inspector = inspect(engine)
    is_pg = engine.dialect.name == "postgresql"
    with engine.begin() as conn:
        for table, column, pg_type, sqlite_type in _COLUMNS:
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column in existing:
                continue
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {pg_type if is_pg else sqlite_type}")
            )
            print(f"[migrate] added {table}.{column}")
        for statement in _INDEXES:
            conn.execute(text(statement))


def backfill_default_workspace() -> None:
    """
    Give pre-workspace data a home so nothing becomes invisible.

    Users, incidents and log uploads that existed before workspaces are moved
    into one default workspace. Runbooks and admin uploads stay shared
    (workspace_id NULL), which is how the knowledge base already behaved.
    """
    db = SessionLocal()
    try:
        needs_work = (
            db.query(models.User).filter(models.User.default_workspace_id.is_(None)).first()
            or db.query(models.Incident).filter(models.Incident.workspace_id.is_(None)).first()
        )
        if not needs_work:
            return

        workspace = (
            db.query(models.Workspace)
            .filter(models.Workspace.name == DEFAULT_WORKSPACE_NAME)
            .first()
        )
        if workspace is None:
            workspace = models.Workspace(name=DEFAULT_WORKSPACE_NAME)
            db.add(workspace)
            db.flush()

        for user in db.query(models.User).filter(models.User.default_workspace_id.is_(None)):
            user.default_workspace_id = workspace.id
            exists = (
                db.query(models.WorkspaceMember)
                .filter_by(workspace_id=workspace.id, user_id=user.id)
                .first()
            )
            if not exists:
                db.add(
                    models.WorkspaceMember(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        role="owner" if user.is_admin else "member",
                    )
                )

        db.query(models.Incident).filter(models.Incident.workspace_id.is_(None)).update(
            {"workspace_id": workspace.id}
        )

        # Pre-workspace log files uploaded by admins stay shared; the log rows
        # inherit the document's scope (NULL = shared) so nothing is orphaned.
        db.commit()
        print("[migrate] backfilled default workspace")
    finally:
        db.close()


def run_migrations() -> None:
    add_missing_columns()
    backfill_default_workspace()
