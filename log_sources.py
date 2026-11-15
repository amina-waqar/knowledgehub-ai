"""
Log source implementations.

Milestone 1 ships one: `DatabaseLogSource`, which reads logs from uploaded
files that were parsed into the `log_entries` table.

Adding a production integration later means adding a class with the same
`name` and `fetch(query) -> FetchResult` shape (see `log_matching.LogSource`),
for example:

    class LokiLogSource:        # translate LogQuery -> LogQL, call /loki/api/v1/query_range
    class CloudWatchLogSource:  # translate LogQuery -> Logs Insights / FilterLogEvents
    class DatadogLogSource:     # translate LogQuery -> POST /api/v2/logs/events/search

and registering it in `SOURCES` below. No matcher or endpoint changes are
needed. None of these are implemented or connected yet.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
from log_matching import FetchResult, LogQuery, LogRecord

NOTABLE_LEVELS = ("WARN", "ERROR", "CRITICAL", "FATAL")


class DatabaseLogSource:
    name = "uploads"

    def __init__(self, db: Session):
        self.db = db

    def fetch(self, query: LogQuery) -> FetchResult:
        E, D = models.LogEntry, models.Document

        q = self.db.query(E, D.title).join(D, D.id == E.document_id)

        # Isolation: a workspace sees its own logs plus platform-shared logs
        # (workspace_id NULL, i.e. admin-uploaded demo data). Never anyone else's.
        q = q.filter(or_(E.workspace_id == query.workspace_id, E.workspace_id.is_(None)))

        windowed = query.window_start is not None and query.window_end is not None
        term_filters = [E.message.ilike(f"%{t}%") for t in query.terms[:12]]
        if query.services:
            term_filters.append(E.service.in_(query.services))

        if windowed:
            q = q.filter(E.timestamp >= query.window_start, E.timestamp <= query.window_end)
            q = q.filter(or_(E.level.in_(NOTABLE_LEVELS), *term_filters))
        else:
            # No time anchor: keep the keyword-only behaviour, but still bounded in SQL.
            if not term_filters:
                return FetchResult(records=[], truncated=False)
            q = q.filter(or_(*term_filters))

        rows = (
            q.order_by(E.timestamp.desc().nulls_last())
            .limit(query.limit + 1)
            .all()
        )
        truncated = len(rows) > query.limit
        rows = rows[: query.limit]

        records = [
            LogRecord(
                id=entry.id,
                timestamp=entry.timestamp,
                level=entry.level,
                message=entry.message,
                service=entry.service,
                trace_id=entry.trace_id,
                host=entry.host,
                document_id=entry.document_id,
                document_title=title,
                raw=entry.raw,
            )
            for entry, title in rows
        ]
        return FetchResult(records=records, truncated=truncated)


SOURCES = {DatabaseLogSource.name: DatabaseLogSource}
