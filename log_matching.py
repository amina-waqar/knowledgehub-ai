"""
Deterministic incident-to-log matching.

No database, network or ML imports: this module takes plain `LogRecord`s that a
`LogSource` fetched, groups repeated lines into clusters, scores each cluster
and explains the score. That keeps it fast to test and independent of where the
logs come from (uploads today; CloudWatch, Loki or Datadog later).
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol

STOP_WORDS = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in",
    "on", "for", "with", "what", "why", "how", "do", "does", "did", "this",
    "that", "it", "my", "our", "from", "has", "have", "had", "not", "but",
    "can", "could", "after", "before", "since", "while", "all", "any", "when",
    "incident", "issue", "problem", "error", "errors", "failed", "failure",
}
# "error"/"failed" are in the stop list above because nearly every incident
# title contains them; they carry no information about *which* logs matter.
# Severity is scored separately through the log level.

LEVEL_WEIGHTS = {
    "FATAL": 4.0, "CRITICAL": 4.0, "ERROR": 3.0, "WARN": 2.0,
    "INFO": 0.0, "DEBUG": 0.0,
}
UNKNOWN_LEVEL_WEIGHT = 1.0
NOTABLE_LEVEL_WEIGHT = 2.0  # WARN and above

TERM_POINTS = 3.0
MAX_TERM_HITS = 5
SERVICE_POINTS = 4.0
PROXIMITY_MAX_POINTS = 3.0
PROXIMITY_HALF_LIFE_MIN = 10.0
FREQUENCY_STEP = 0.1
FREQUENCY_CAP = 10


@dataclass
class LogRecord:
    id: int
    timestamp: Optional[datetime]
    level: Optional[str]
    message: str
    service: Optional[str] = None
    trace_id: Optional[str] = None
    host: Optional[str] = None
    document_id: Optional[int] = None
    document_title: Optional[str] = None
    raw: Optional[str] = None


@dataclass
class LogQuery:
    """What a LogSource is asked for. Sources translate this to their own query language."""
    workspace_id: Optional[int]
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    services: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    limit: int = 5000


@dataclass
class FetchResult:
    records: list[LogRecord]
    truncated: bool = False


class LogSource(Protocol):
    """
    Interface every log backend implements.

    Uploaded files are `DatabaseLogSource` (log_sources.py). A CloudWatch, Loki
    or Datadog connector would implement the same single method by translating
    the LogQuery into that system's query and returning LogRecords. The matcher
    below never needs to know which one it is talking to.
    """

    name: str

    def fetch(self, query: LogQuery) -> FetchResult: ...


# ---------------------------------------------------------------- helpers
def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokens(text: str) -> set[str]:
    """Lowercase word tokens, plus the parts of hyphen/underscore compounds, lightly stemmed."""
    found: set[str] = set()
    for word in re.findall(r"[a-zA-Z0-9_\-]+", (text or "").lower()):
        if len(word) < 3:
            continue
        found.add(_stem(word))
        for part in re.split(r"[_\-]+", word):
            if len(part) >= 3:
                found.add(_stem(part))
    return found


def extract_terms(*texts: Optional[str]) -> list[str]:
    """Search terms from incident text: informative tokens, no stop words or bare numbers."""
    merged: set[str] = set()
    for text in texts:
        merged |= tokens(text or "")
    return sorted(t for t in merged if t not in STOP_WORDS and not t.isdigit())


def compute_window(
    anchor: Optional[datetime],
    resolved_at: Optional[datetime],
    before_minutes: int,
    after_minutes: int,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Time window to search. No anchor means no window (keyword-only matching,
    which is how the original implementation behaved).
    """
    anchor = as_utc(anchor)
    if anchor is None:
        return None, None
    start = anchor - timedelta(minutes=before_minutes)
    resolved = as_utc(resolved_at)
    end = (resolved + timedelta(minutes=5)) if resolved else anchor + timedelta(minutes=after_minutes)
    return start, end


_TEMPLATE_RULES = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<hex>"),
    (re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?"), "<n>"),  # also 5000ms; leaves sha256/utf8 alone
]


def template_of(message: str) -> str:
    """Collapse ids, addresses and numbers so repeats of one event become one cluster."""
    text = (message or "").strip().split("\n", 1)[0]
    for pattern, replacement in _TEMPLATE_RULES:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text)[:300]


def cluster_id_for(template: str, level: Optional[str], service: Optional[str]) -> str:
    key = f"{template}|{level or ''}|{(service or '').lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return as_utc(dt).isoformat() if dt else None


# ------------------------------------------------------------- matching
def rank_clusters(
    records: Iterable[LogRecord],
    terms: Iterable[str],
    services: Iterable[str] = (),
    anchor: Optional[datetime] = None,
    windowed: bool = False,
    top_n: int = 20,
) -> list[dict]:
    """
    Group records into clusters and return the top_n most relevant, best first.

    A cluster is included when it shares a term with the incident, runs on the
    incident's service, or (inside a time window) is WARN or worse. Without a
    window only term/service hits count, so unrelated errors are not returned.
    Every cluster carries a score breakdown and plain-language reasons.
    """
    term_set = {t.lower() for t in terms}
    service_set = {s.lower() for s in services if s}
    anchor = as_utc(anchor)

    groups: dict[tuple, list[LogRecord]] = defaultdict(list)
    for record in records:
        level = (record.level or "").upper() or None
        key = (template_of(record.message), level, (record.service or "").lower() or None)
        groups[key].append(record)

    ranked: list[dict] = []
    for (template, level, service_key), members in groups.items():
        # Terms are matched against the message text only. The service name is
        # scored separately; counting it here too would double-reward every
        # line from the incident's service.
        message_tokens: set[str] = set()
        for member in members[:20]:  # a cluster's lines are near-identical
            message_tokens |= tokens(member.message)
        matching_terms = sorted(term_set & message_tokens)

        level_weight = LEVEL_WEIGHTS.get(level or "", UNKNOWN_LEVEL_WEIGHT)
        notable = level_weight >= NOTABLE_LEVEL_WEIGHT
        # Running on the incident's service only counts for WARN and worse:
        # routine INFO lines from that service are not evidence of anything.
        service_match = bool(service_key and service_key in service_set) and notable

        included = bool(matching_terms) or service_match or (windowed and notable)
        if not included:
            continue

        stamps = sorted(as_utc(m.timestamp) for m in members if m.timestamp)
        first_seen = stamps[0] if stamps else None
        last_seen = stamps[-1] if stamps else None

        breakdown = {
            "terms": round(min(len(matching_terms), MAX_TERM_HITS) * TERM_POINTS, 2),
            "service": SERVICE_POINTS if service_match else 0.0,
            "level": level_weight,
            "proximity": 0.0,
            "frequency": round(min(len(members), FREQUENCY_CAP) * FREQUENCY_STEP, 2),
        }

        minutes_from_start: Optional[float] = None
        if anchor and stamps:
            closest = min(abs((s - anchor).total_seconds()) for s in stamps) / 60.0
            breakdown["proximity"] = round(
                PROXIMITY_MAX_POINTS / (1.0 + closest / PROXIMITY_HALF_LIFE_MIN), 2
            )
            minutes_from_start = round((first_seen - anchor).total_seconds() / 60.0, 1)

        score = round(sum(breakdown.values()), 2)

        why: list[str] = []
        if matching_terms:
            why.append("Shares terms with the incident: " + ", ".join(matching_terms))
        if service_match:
            why.append(f"Logged by '{members[0].service}', the incident's service")
        if level_weight >= NOTABLE_LEVEL_WEIGHT:
            why.append(f"{level} severity")
        if minutes_from_start is not None:
            if minutes_from_start >= 0:
                why.append(f"First seen {minutes_from_start:g} min after the incident started")
            else:
                why.append(f"First seen {abs(minutes_from_start):g} min before the incident started")
        if len(members) > 1:
            why.append(f"Repeated {len(members)} times")

        by_time = sorted(members, key=lambda m: (as_utc(m.timestamp) or datetime.max.replace(tzinfo=timezone.utc), m.id))
        document_counts = Counter((m.document_id, m.document_title) for m in members)
        (document_id, document_title), _ = document_counts.most_common(1)[0]
        representative = by_time[-1]

        ranked.append(
            {
                "cluster_id": cluster_id_for(template, level, service_key),
                "template": template,
                "message": representative.message.split("\n", 1)[0][:500],
                "level": level or "UNKNOWN",
                "service": members[0].service,
                "occurrence_count": len(members),
                "first_seen": _iso(first_seen),
                "last_seen": _iso(last_seen),
                "latest_timestamp": _iso(last_seen),  # name used by build_log_context
                "document_id": document_id,
                "document_title": document_title,
                "trace_ids": sorted({m.trace_id for m in members if m.trace_id})[:5],
                "matching_terms": matching_terms,
                "score": score,
                "score_breakdown": breakdown,
                "why": why,
                "examples": [
                    {
                        "id": m.id,
                        "timestamp": _iso(m.timestamp),
                        "level": m.level,
                        "service": m.service,
                        "text": (m.raw or m.message)[:500],
                    }
                    for m in by_time[:3]
                ],
            }
        )

    ranked.sort(key=lambda c: (-c["score"], c["first_seen"] or "", c["cluster_id"]))
    return ranked[:top_n]


def build_timeline(records: Iterable[LogRecord], buckets: int = 24) -> list[dict]:
    """Event counts over time for the dashboard strip. Returns [] if nothing has a timestamp."""
    stamped = [(as_utc(r.timestamp), (r.level or "").upper()) for r in records if r.timestamp]
    if not stamped:
        return []
    low = min(t for t, _ in stamped)
    high = max(t for t, _ in stamped)
    span = (high - low).total_seconds()
    if span <= 0:
        return [{"start": low.isoformat(), "count": len(stamped),
                 "notable": sum(1 for _, lv in stamped if LEVEL_WEIGHTS.get(lv, 1) >= NOTABLE_LEVEL_WEIGHT)}]
    width = span / buckets
    counts = [0] * buckets
    notable = [0] * buckets
    for stamp, level in stamped:
        index = min(int((stamp - low).total_seconds() / width), buckets - 1)
        counts[index] += 1
        if LEVEL_WEIGHTS.get(level, UNKNOWN_LEVEL_WEIGHT) >= NOTABLE_LEVEL_WEIGHT:
            notable[index] += 1
    return [
        {"start": (low + timedelta(seconds=width * i)).isoformat(), "count": counts[i], "notable": notable[i]}
        for i in range(buckets)
    ]


def _prepare(
    *, workspace_id, title, description, services, anchor, resolved_at,
    extra_text, before_minutes, after_minutes, max_rows,
):
    service_list = [s for s in services if s]
    terms = extract_terms(title, description, extra_text)
    start, end = compute_window(anchor, resolved_at, before_minutes, after_minutes)
    query = LogQuery(
        workspace_id=workspace_id,
        window_start=start,
        window_end=end,
        services=service_list,
        terms=terms,
        limit=max_rows,
    )
    return service_list, terms, start, end, query


def _record_cluster_id(record: LogRecord) -> str:
    return cluster_id_for(
        template_of(record.message),
        (record.level or "").upper() or None,
        (record.service or "").lower() or None,
    )


def match_logs(
    source: LogSource,
    *,
    workspace_id: Optional[int],
    title: str,
    description: Optional[str],
    services: Iterable[str],
    anchor: Optional[datetime],
    resolved_at: Optional[datetime],
    extra_text: Optional[str] = None,
    before_minutes: int = 15,
    after_minutes: int = 60,
    max_rows: int = 5000,
    top_n: int = 20,
) -> dict:
    """End-to-end: build the query, fetch from the source, cluster, rank, explain."""
    service_list, terms, start, end, query = _prepare(
        workspace_id=workspace_id, title=title, description=description,
        services=services, anchor=anchor, resolved_at=resolved_at,
        extra_text=extra_text, before_minutes=before_minutes,
        after_minutes=after_minutes, max_rows=max_rows,
    )
    windowed = start is not None
    fetched = source.fetch(query)
    clusters = rank_clusters(
        fetched.records, terms, service_list, anchor=anchor, windowed=windowed, top_n=top_n
    )
    ranked_cluster_ids = {c["cluster_id"] for c in clusters}
    in_ranked = [r for r in fetched.records if _record_cluster_id(r) in ranked_cluster_ids]
    return {
        "source": getattr(source, "name", "unknown"),
        "window": {
            "start": _iso(start),
            "end": _iso(end),
            "anchor": _iso(anchor),
            "applied": windowed,
        },
        "services": service_list,
        "terms": terms,
        "candidates_scanned": len(fetched.records),
        "truncated": fetched.truncated,
        "clusters": clusters,
        "timeline": build_timeline(in_ranked),
    }


def cluster_entries(
    source: LogSource,
    cluster_id: str,
    *,
    limit: int = 200,
    **kwargs,
) -> list[dict]:
    """Every raw line in one cluster, oldest first (for the drill-down view)."""
    *_, query = _prepare(
        workspace_id=kwargs["workspace_id"], title=kwargs["title"],
        description=kwargs.get("description"), services=kwargs.get("services", ()),
        anchor=kwargs.get("anchor"), resolved_at=kwargs.get("resolved_at"),
        extra_text=kwargs.get("extra_text"),
        before_minutes=kwargs.get("before_minutes", 15),
        after_minutes=kwargs.get("after_minutes", 60),
        max_rows=kwargs.get("max_rows", 5000),
    )
    members = [r for r in source.fetch(query).records if _record_cluster_id(r) == cluster_id]
    members.sort(key=lambda r: (as_utc(r.timestamp) or datetime.max.replace(tzinfo=timezone.utc), r.id))
    return [
        {
            "id": r.id,
            "timestamp": _iso(r.timestamp),
            "level": r.level,
            "service": r.service,
            "trace_id": r.trace_id,
            "host": r.host,
            "text": (r.raw or r.message)[:2000],
        }
        for r in members[:limit]
    ]
