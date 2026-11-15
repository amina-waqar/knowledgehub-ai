import json
import pathlib
from datetime import datetime, timedelta, timezone

import log_parsing
from log_matching import (
    FetchResult, LogRecord, compute_window, extract_terms, match_logs,
    rank_clusters, template_of,
)

T0 = datetime(2026, 1, 15, 14, 20, tzinfo=timezone.utc)
SAMPLE = pathlib.Path(__file__).resolve().parent.parent / "sample_data" / "incident_checkout_outage.jsonl"


def rec(i, minutes, level, msg, service=None, trace=None):
    return LogRecord(id=i, timestamp=T0 + timedelta(minutes=minutes), level=level,
                     message=msg, service=service, trace_id=trace, document_id=1,
                     document_title="f.log")


def test_template_collapses_numbers_ids_and_ips():
    a = template_of("Timeout after 5000ms for order 123 from 10.0.0.5:8080")
    b = template_of("Timeout after 300ms for order 999 from 10.0.0.9:80")
    assert a == b == "Timeout after <n>ms for order <n> from <ip>"
    assert template_of("id 123e4567-e89b-12d3-a456-426614174000") == "id <uuid>"


def test_extract_terms_drops_stop_words_and_numbers():
    terms = extract_terms("Checkout failing: payment timeouts", "the DB pool is exhausted 500")
    assert "payment" in terms and "timeout" in terms  # stemmed
    assert "the" not in terms and "500" not in terms
    assert "failing" in terms


def test_window_bounds():
    start, end = compute_window(T0, None, 15, 60)
    assert start == T0 - timedelta(minutes=15) and end == T0 + timedelta(minutes=60)
    _, end = compute_window(T0, T0 + timedelta(minutes=30), 15, 60)
    assert end == T0 + timedelta(minutes=35)
    assert compute_window(None, None, 15, 60) == (None, None)


def test_clusters_group_repeats_and_explain():
    records = [rec(i, i, "ERROR", f"Database timeout after {100*i}ms", "payments-svc") for i in range(1, 6)]
    out = rank_clusters(records, ["database", "timeout"], ["payments-svc"], anchor=T0, windowed=True)
    assert len(out) == 1
    c = out[0]
    assert c["occurrence_count"] == 5
    assert set(c["matching_terms"]) == {"database", "timeout"}
    assert c["score_breakdown"]["service"] > 0
    assert any("terms" in w for w in c["why"])
    assert abs(sum(c["score_breakdown"].values()) - c["score"]) < 0.02


def test_unrelated_info_excluded_but_unrelated_error_included_in_window():
    records = [rec(1, 1, "INFO", "Order created", "checkout-api"),
               rec(2, 1, "ERROR", "Index shard rebalancing", "search-svc")]
    windowed = rank_clusters(records, ["payment"], [], anchor=T0, windowed=True)
    assert [c["service"] for c in windowed] == ["search-svc"]
    # Without a window only term/service hits count.
    assert rank_clusters(records, ["payment"], [], anchor=None, windowed=False) == []


def test_closer_to_incident_start_scores_higher():
    near = rec(1, 1, "ERROR", "alpha failure", "s")
    far = rec(2, 50, "ERROR", "beta failure", "s")
    out = rank_clusters([near, far], ["failure"], [], anchor=T0, windowed=True)
    assert out[0]["message"] == "alpha failure"


def test_ranking_is_deterministic():
    records = [rec(i, i % 7, "ERROR", f"thing {i % 3} broke", "s") for i in range(30)]
    assert rank_clusters(records, ["broke"], [], T0, True) == rank_clusters(list(reversed(records)), ["broke"], [], T0, True)


class ListSource:
    """Stands in for any LogSource, proving the matcher only needs the interface."""
    name = "list"

    def __init__(self, records):
        self.records = records

    def fetch(self, query):
        return FetchResult(records=self.records, truncated=False)


def load_sample():
    records = []
    for i, p in enumerate(log_parsing.parse_log_lines(SAMPLE.read_text().splitlines()), 1):
        records.append(LogRecord(id=i, timestamp=p["timestamp"], level=p["level"], message=p["message"],
                                 service=p["service"], trace_id=p["trace_id"], host=p["host"],
                                 document_id=1, document_title="sample"))
    return records


def test_sample_incident_ranks_root_cause_logs_above_noise():
    result = match_logs(
        ListSource(load_sample()), workspace_id=1,
        title="Checkout failing: payment timeouts", description=None,
        services=["checkout-api"], anchor=T0, resolved_at=None,
    )
    messages = [c["message"] for c in result["clusters"]]
    assert result["source"] == "list" and result["window"]["applied"]
    top5 = " | ".join(messages[:5]).lower()
    assert "database timeout" in top5
    assert "payment request failed" in top5
    # the noisy, unrelated services rank below the related ones
    ranks = {c["service"]: i for i, c in reversed(list(enumerate(result["clusters"])))}
    assert ranks["payments-svc"] < ranks["email-worker"]
    assert ranks["payments-svc"] < ranks["search-svc"]
    assert result["timeline"] and sum(b["count"] for b in result["timeline"]) > 0


def test_service_name_is_not_a_keyword_hit_and_info_from_service_is_not_evidence():
    records = [
        rec(1, 1, "INFO", "Order 1018 created in 86ms", "checkout-api"),
        rec(2, 1, "ERROR", "Database timeout after 5000ms waiting for connection", "payments-svc"),
    ]
    out = rank_clusters(records, ["checkout", "timeout"], ["checkout-api"], anchor=T0, windowed=True)
    # "checkout" appears only in the service name, so the routine INFO line is excluded...
    assert [c["service"] for c in out] == ["payments-svc"]
    # ...and a WARN+ line from the incident's service still earns the service bonus.
    warn = rec(3, 1, "WARN", "Retrying request", "checkout-api")
    out = rank_clusters([warn], ["timeout"], ["checkout-api"], anchor=T0, windowed=True)
    assert out[0]["score_breakdown"]["service"] > 0


def test_sample_incident_info_noise_never_outranks_errors():
    result = match_logs(
        ListSource(load_sample()), workspace_id=1,
        title="Checkout failing: payment timeouts", description=None,
        services=["checkout-api"], anchor=T0, resolved_at=None,
    )
    levels = [c["level"] for c in result["clusters"]]
    first_info = next((i for i, lv in enumerate(levels) if lv == "INFO"), len(levels))
    assert all(lv != "INFO" for lv in levels[:first_info])
    notable_ranks = [i for i, c in enumerate(result["clusters"]) if c["level"] in ("ERROR", "CRITICAL", "FATAL")]
    assert max(notable_ranks) < first_info or first_info == len(levels)
