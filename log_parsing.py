"""
Log line parsing.

Pure functions with no third-party imports, so they are cheap to unit test.

Supported formats
-----------------
1. JSON lines (one object per line). Common field aliases are understood:
     timestamp: timestamp, ts, time, @timestamp, datetime, t
     level:     level, severity, lvl, log.level
     message:   message, msg, log, event
     service:   service, service.name, service_name, app, application, component
     trace id:  trace_id, traceId, trace.id, request_id, requestId, correlation_id
     host:      host, hostname, host.name
   Everything else is kept in `attributes`.

2. Plain text:  <timestamp> <LEVEL> [service] message
   The original format (`<timestamp> <LEVEL> message`) still works. The
   `[service]` prefix is optional. `key=value` tokens inside the message are
   read for service=, trace_id=, request_id= and host=.

Timestamps without a timezone are assumed to be UTC, so every parsed
timestamp is timezone-aware and safe to compare against an incident window.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

MAX_MESSAGE_CHARS = 4000

LEVEL_ALIASES = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "SEVERE": "ERROR",
    "CRIT": "CRITICAL",
    "CRITICAL": "CRITICAL",
    "FATAL": "FATAL",
    "EMERG": "FATAL",
    "ALERT": "FATAL",
}

_TS_KEYS = ("timestamp", "ts", "time", "@timestamp", "datetime", "t")
_LEVEL_KEYS = ("level", "severity", "lvl", "log.level", "loglevel")
_MESSAGE_KEYS = ("message", "msg", "log", "event")
_SERVICE_KEYS = (
    "service", "service.name", "service_name", "app", "application", "component",
)
_TRACE_KEYS = (
    "trace_id", "traceId", "trace.id", "request_id", "requestId",
    "correlation_id", "correlationId",
)
_HOST_KEYS = ("host", "hostname", "host.name")

_PLAIN_REGEX = re.compile(
    r"^(?P<ts>\S+(?:[ T]\d{2}:\d{2}:\d{2}[\.,]?\d*(?:Z|[+-]\d{2}:?\d{2})?)?)\s+"
    r"(?P<level>[A-Za-z]+)\s*[:\-]?\s+(?P<rest>.+)$"
)
_BRACKET_SERVICE = re.compile(r"^\[(?P<svc>[\w.\-/]+)\]\s*(?P<msg>.*)$")
_KV_REGEX = re.compile(
    r"\b(?P<key>service|svc|trace_id|traceId|request_id|req_id|host)=(?P<val>[^\s,;]+)"
)
_KV_TARGET = {
    "service": "service", "svc": "service",
    "trace_id": "trace_id", "traceId": "trace_id",
    "request_id": "trace_id", "req_id": "trace_id",
    "host": "host",
}


def normalize_level(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    return LEVEL_ALIASES.get(str(raw).strip().upper())


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse ISO-8601-ish strings or epoch seconds/milliseconds. Always UTC-aware."""
    if value is None or value == "":
        return None

    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
            if seconds > 1e11:  # milliseconds
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, tz=timezone.utc)

        text = str(value).strip()
        if re.fullmatch(r"\d{10,13}(\.\d+)?", text):
            return parse_timestamp(float(text))

        text = text.replace(",", ".")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first(obj: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _flatten_once(obj: dict) -> dict:
    """Make nested keys like {"log": {"level": "error"}} reachable as "log.level"."""
    flat = dict(obj)
    for key, value in obj.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat.setdefault(f"{key}.{sub_key}", sub_value)
    return flat


def _parse_json_line(line: str) -> Optional[dict]:
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    flat = _flatten_once(obj)
    message = _first(flat, _MESSAGE_KEYS)
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    message = str(message) if message is not None else line

    used = {
        key
        for keys in (_TS_KEYS, _LEVEL_KEYS, _MESSAGE_KEYS, _SERVICE_KEYS, _TRACE_KEYS, _HOST_KEYS)
        for key in keys
    }
    attributes = {
        k: v for k, v in obj.items()
        if k not in used and not (isinstance(v, dict) and any(f"{k}.{s}" in used for s in v))
    }

    service = _first(flat, _SERVICE_KEYS)
    trace_id = _first(flat, _TRACE_KEYS)
    host = _first(flat, _HOST_KEYS)

    return {
        "timestamp": parse_timestamp(_first(flat, _TS_KEYS)),
        "level": normalize_level(_first(flat, _LEVEL_KEYS)),
        "message": message[:MAX_MESSAGE_CHARS],
        "raw": line,
        "service": str(service) if service is not None else None,
        "trace_id": str(trace_id) if trace_id is not None else None,
        "host": str(host) if host is not None else None,
        "attributes": attributes or None,
    }


def parse_log_line(line: str, default_service: Optional[str] = None) -> dict:
    """
    Parse one line. Returns {} for blank lines.

    Result keys: timestamp, level, message, raw, service, trace_id, host, attributes.
    Lines that match no known format are kept whole as the message, with
    timestamp and level set to None (same as the original parser).
    """
    line = line.strip()
    if not line:
        return {}

    result: Optional[dict] = None
    if line.startswith("{"):
        result = _parse_json_line(line)

    if result is None:
        result = {
            "timestamp": None, "level": None, "message": line[:MAX_MESSAGE_CHARS],
            "raw": line, "service": None, "trace_id": None, "host": None,
            "attributes": None,
        }
        match = _PLAIN_REGEX.match(line)
        if match:
            ts = parse_timestamp(match.group("ts"))
            level = normalize_level(match.group("level"))
            if ts is not None and level is not None:
                rest = match.group("rest")
                bracket = _BRACKET_SERVICE.match(rest)
                if bracket:
                    result["service"] = bracket.group("svc")
                    rest = bracket.group("msg") or rest
                for kv in _KV_REGEX.finditer(rest):
                    target = _KV_TARGET[kv.group("key")]
                    if result[target] is None:
                        result[target] = kv.group("val")
                result["timestamp"] = ts
                result["level"] = level
                result["message"] = rest[:MAX_MESSAGE_CHARS]

    if result["service"] is None and default_service:
        result["service"] = default_service
    return result


def parse_log_lines(
    lines: Iterable[str], default_service: Optional[str] = None
) -> Iterator[dict]:
    """
    Parse a stream of lines, folding continuation lines (stack traces and
    other indented lines with no timestamp) into the entry before them.
    """
    previous: Optional[dict] = None
    for line in lines:
        is_continuation = bool(line) and line[0] in (" ", "\t") and line.strip() != ""
        parsed = parse_log_line(line, default_service)
        if not parsed:
            continue

        if (
            is_continuation
            and previous is not None
            and parsed["timestamp"] is None
            and parsed["level"] is None
        ):
            if len(previous["message"]) < MAX_MESSAGE_CHARS:
                previous["message"] = (
                    previous["message"] + "\n" + parsed["raw"]
                )[:MAX_MESSAGE_CHARS]
            continue

        if previous is not None:
            yield previous
        previous = parsed

    if previous is not None:
        yield previous
