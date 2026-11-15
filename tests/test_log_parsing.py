from datetime import timezone

from log_parsing import parse_log_line, parse_log_lines, parse_timestamp


def test_original_plain_format_still_parses():
    p = parse_log_line("2024-06-01T10:44:03Z ERROR Database timeout on query xyz")
    assert p["level"] == "ERROR"
    assert p["message"] == "Database timeout on query xyz"
    assert p["timestamp"].tzinfo is not None
    assert p["timestamp"].isoformat() == "2024-06-01T10:44:03+00:00"


def test_malformed_line_kept_as_message():
    p = parse_log_line("malformed line without timestamp")
    assert p["timestamp"] is None and p["level"] is None
    assert p["message"] == "malformed line without timestamp"


def test_blank_line_is_empty():
    assert parse_log_line("   \n") == {}


def test_bracket_service_and_kv_fields():
    p = parse_log_line("2026-01-15 14:20:01,250 WARNING [payments-svc] pool low trace_id=abc123 host=pay-1")
    assert p["level"] == "WARN"
    assert p["service"] == "payments-svc"
    assert p["trace_id"] == "abc123"
    assert p["host"] == "pay-1"
    assert p["timestamp"].isoformat() == "2026-01-15T14:20:01.250000+00:00"


def test_json_line_with_aliases_and_attributes():
    p = parse_log_line(
        '{"@timestamp":"2026-01-15T14:20:00+01:00","severity":"error","msg":"boom",'
        '"service.name":"api","requestId":"r-1","region":"eu"}'
    )
    assert p["level"] == "ERROR"
    assert p["message"] == "boom"
    assert p["service"] == "api"
    assert p["trace_id"] == "r-1"
    assert p["timestamp"].isoformat() == "2026-01-15T13:20:00+00:00"  # converted to UTC
    assert p["attributes"] == {"region": "eu"}


def test_json_nested_log_level():
    p = parse_log_line('{"ts":1768486800,"log":{"level":"warn"},"message":"x"}')
    assert p["level"] == "WARN"
    assert p["timestamp"].tzinfo == timezone.utc


def test_invalid_json_falls_back_to_plain():
    p = parse_log_line('{"not json')
    assert p["message"] == '{"not json'


def test_naive_timestamp_assumed_utc():
    assert parse_timestamp("2026-01-15 14:20:00").isoformat() == "2026-01-15T14:20:00+00:00"


def test_epoch_millis():
    assert parse_timestamp(1768486800000).isoformat() == "2026-01-15T14:20:00+00:00"


def test_unparseable_timestamp_is_none():
    assert parse_timestamp("yesterday-ish") is None


def test_default_service_applied():
    assert parse_log_line("2026-01-15T14:20:00Z INFO hi", default_service="svc")["service"] == "svc"


def test_continuation_lines_fold_into_previous_entry():
    lines = [
        "2026-01-15T14:20:00Z ERROR Unhandled exception",
        "    at app.handler (app.py:10)",
        "    at app.main (app.py:3)",
        "2026-01-15T14:20:05Z INFO recovered",
    ]
    entries = list(parse_log_lines(lines))
    assert len(entries) == 2
    assert "app.py:10" in entries[0]["message"]
    assert entries[1]["message"] == "recovered"
