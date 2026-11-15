"""
Log retention, to stay inside Neon Free's storage allowance.

Rows are purged by *ingest time* (`created_at`), not by log timestamp, so
uploading an old log file for an investigation does not delete it immediately.
Deleted space is reused by Postgres but the on-disk size only shrinks after
autovacuum/VACUUM, so the storage cap in config (`DB_STORAGE_LIMIT_MB`) is
deliberately set below the provider limit.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, func

import models
from config import settings
from database import SessionLocal

BATCH = 5000


def purge_old_logs(retention_days: int | None = None) -> dict:
    days = settings.LOG_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return {"enabled": False, "entries_deleted": 0, "documents_deleted": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    entries_deleted = 0
    db = SessionLocal()
    try:
        while True:
            ids = db.execute(
                select(models.LogEntry.id)
                .where(models.LogEntry.created_at < cutoff)
                .limit(BATCH)
            ).scalars().all()
            if not ids:
                break
            db.execute(delete(models.LogEntry).where(models.LogEntry.id.in_(ids)))
            db.commit()
            entries_deleted += len(ids)

        # Remove log documents that no longer have any entries.
        has_entries = select(models.LogEntry.id).where(
            models.LogEntry.document_id == models.Document.id
        ).exists()
        stale = db.execute(
            select(models.Document.id).where(
                models.Document.type == models.DocumentType.LOG,
                models.Document.uploaded_at < cutoff,
                ~has_entries,
            )
        ).scalars().all()
        if stale:
            db.execute(delete(models.Document).where(models.Document.id.in_(stale)))
            db.commit()
        return {
            "enabled": True,
            "retention_days": days,
            "entries_deleted": entries_deleted,
            "documents_deleted": len(stale),
        }
    finally:
        db.close()
