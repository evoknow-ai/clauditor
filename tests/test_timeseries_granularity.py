"""Granularity coverage for /api/timeseries (Phase 6 QA addition).

Seeds a temp DB via core.db with rows spanning multiple hours / weeks / months
and asserts that granularity=hour, =week, =month each bucket correctly:

* Distinct buckets for distinct periods.
* spend/tokens within each bucket reconcile to the seeded values.
* split-by-model preserved (each row in the series carries a model key).
* An invalid granularity returns 4xx (SPEC.md §7: "Return clean JSON, HTTP 4xx
  on bad input").

Style matches tests/test_server.py (same imports, same _seed_row helper).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.config import load_pricing
from core.db import init_db, insert_usage_event
from core.pricing import compute_cost
from server.app import build_app

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"
PRICING_UPDATED = "2026-06-20"


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


def _seed_row(conn, pricing, **overrides):
    """Insert one usage event, computing cost via the real pricing engine."""
    event = {
        "event_uid": overrides["event_uid"],
        "ts": overrides["ts"],
        "source": overrides.get("source", "claude_code"),
        "project": overrides.get("project", "demo"),
        "model": overrides.get("model", "claude-opus-4-8"),
        "input_tokens": overrides.get("input_tokens", 0),
        "output_tokens": overrides.get("output_tokens", 0),
        "cache_creation_tokens": overrides.get("cache_creation_tokens", 0),
        "cache_read_tokens": overrides.get("cache_read_tokens", 0),
        "is_batch": overrides.get("is_batch", 0),
        "cache_ttl": overrides.get("cache_ttl"),
        "session_id": overrides.get("session_id", "sess"),
        "raw_meta": None,
    }
    event["cost_usd"] = compute_cost(event, pricing)
    inserted = insert_usage_event(conn, event)
    assert inserted, f"duplicate uid? {overrides['event_uid']}"
    return event


@pytest.fixture
def pricing():
    return load_pricing(PRICING_PATH)


@pytest.fixture
def multi_span_db(tmp_path, pricing):
    """DB seeded with rows spanning multiple hours, weeks, and months.

    Layout (all UTC):
      hour_a1: 2026-01-10T08:00:00  model=claude-opus-4-8    input=1000 out=200
      hour_a2: 2026-01-10T08:30:00  model=claude-opus-4-8    input=500  out=100
      hour_b1: 2026-01-10T09:00:00  model=claude-sonnet-4-6  input=300  out=50
      week_w1: 2026-01-05T12:00:00  model=claude-haiku-4-5   input=200  out=40
      week_w2: 2026-01-12T12:00:00  model=claude-haiku-4-5   input=200  out=40
      month_m1:2026-02-15T10:00:00  model=claude-opus-4-8    input=800  out=100
      month_m2:2026-03-20T10:00:00  model=claude-opus-4-8    input=600  out=80
    """
    db_path = tmp_path / "gran.db"
    conn = init_db(db_path)

    # Two rows in the same hour+model -> merge into one bucket
    hour_a1 = _seed_row(conn, pricing, event_uid="ha1",
        ts="2026-01-10T08:00:00+00:00", model="claude-opus-4-8",
        input_tokens=1000, output_tokens=200)
    hour_a2 = _seed_row(conn, pricing, event_uid="ha2",
        ts="2026-01-10T08:30:00+00:00", model="claude-opus-4-8",
        input_tokens=500, output_tokens=100)

    # Different hour, different model -> separate bucket
    hour_b1 = _seed_row(conn, pricing, event_uid="hb1",
        ts="2026-01-10T09:00:00+00:00", model="claude-sonnet-4-6",
        input_tokens=300, output_tokens=50)

    # Different ISO weeks (week W01 vs W02 of 2026)
    week_w1 = _seed_row(conn, pricing, event_uid="ww1",
        ts="2026-01-05T12:00:00+00:00", model="claude-haiku-4-5",
        input_tokens=200, output_tokens=40)
    week_w2 = _seed_row(conn, pricing, event_uid="ww2",
        ts="2026-01-12T12:00:00+00:00", model="claude-haiku-4-5",
        input_tokens=200, output_tokens=40)

    # Different months
    month_m1 = _seed_row(conn, pricing, event_uid="mm1",
        ts="2026-02-15T10:00:00+00:00", model="claude-opus-4-8",
        input_tokens=800, output_tokens=100)
    month_m2 = _seed_row(conn, pricing, event_uid="mm2",
        ts="2026-03-20T10:00:00+00:00", model="claude-opus-4-8",
        input_tokens=600, output_tokens=80)

    conn.commit()
    conn.close()

    # Use a wide explicit range that covers all rows
    app = build_app(db_path=db_path, pricing_updated=PRICING_UPDATED)
    client = TestClient(app)

    return {
        "client": client,
        "pricing": pricing,
        "rows": {
            "ha1": hour_a1, "ha2": hour_a2, "hb1": hour_b1,
            "ww1": week_w1, "ww2": week_w2,
            "mm1": month_m1, "mm2": month_m2,
        }
    }


# A wide time range that covers every seeded row.
_WIDE = {"from": "2026-01-01", "to": "2026-12-31"}


# --- granularity=hour -------------------------------------------------------

def test_hour_distinct_buckets(multi_span_db):
    """ha1 and ha2 share 2026-01-10T08; hb1 is 2026-01-10T09 -> distinct."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={**_WIDE, "granularity": "hour"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["granularity"] == "hour"

    buckets = {s["bucket"] for s in body["series"]}
    # The two opus rows land in 2026-01-10T08; the sonnet row in 2026-01-10T09
    assert "2026-01-10T08" in buckets
    assert "2026-01-10T09" in buckets
    # Bucket string length for hours is 13 chars (YYYY-MM-DDTHH).
    for s in body["series"]:
        assert len(s["bucket"]) == 13, f"unexpected bucket format: {s['bucket']!r}"


def test_hour_same_model_same_hour_merged(multi_span_db):
    """ha1 + ha2 are same model, same hour -> single series entry, 2 calls."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={**_WIDE, "granularity": "hour"}
    )
    body = r.json()
    opus_08 = [
        s for s in body["series"]
        if s["model"] == "claude-opus-4-8" and s["bucket"] == "2026-01-10T08"
    ]
    assert len(opus_08) == 1, "ha1 + ha2 should be one merged row"
    assert opus_08[0]["call_count"] == 2


def test_hour_spend_reconciles(multi_span_db):
    """Spend in the merged opus-08 hour bucket equals ha1.cost + ha2.cost."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={**_WIDE, "granularity": "hour"}
    )
    body = r.json()
    opus_08 = next(
        s for s in body["series"]
        if s["model"] == "claude-opus-4-8" and s["bucket"] == "2026-01-10T08"
    )
    rows = multi_span_db["rows"]
    expected = round(rows["ha1"]["cost_usd"] + rows["ha2"]["cost_usd"], 6)
    assert abs(opus_08["spend_usd"] - expected) < 1e-9, (
        f"spend mismatch: got {opus_08['spend_usd']}, expected {expected}"
    )


def test_hour_tokens_reconciles(multi_span_db):
    """Token count in the merged opus-08 hour bucket = sum of ha1+ha2 tokens."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={**_WIDE, "granularity": "hour"}
    )
    body = r.json()
    opus_08 = next(
        s for s in body["series"]
        if s["model"] == "claude-opus-4-8" and s["bucket"] == "2026-01-10T08"
    )
    # ha1: 1000 in + 200 out; ha2: 500 in + 100 out
    expected_tokens = (1000 + 200) + (500 + 100)
    assert opus_08["tokens"] == expected_tokens


def test_hour_split_by_model_preserved(multi_span_db):
    """Different models in the same wall-clock hour appear as separate rows."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-01-10T00:00:00+00:00", "to": "2026-01-10T23:59:59+00:00", "granularity": "hour"}
    )
    body = r.json()
    models_seen = {s["model"] for s in body["series"]}
    assert "claude-opus-4-8" in models_seen
    assert "claude-sonnet-4-6" in models_seen


# --- granularity=week -------------------------------------------------------

def test_week_distinct_buckets(multi_span_db):
    """ww1 (2026-01-05, week 01) and ww2 (2026-01-12, week 02) are distinct."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-01-01", "to": "2026-01-31", "granularity": "week"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["granularity"] == "week"

    haiku_buckets = sorted(
        s["bucket"] for s in body["series"] if s["model"] == "claude-haiku-4-5"
    )
    # ww1 is 2026-01-05 (W01) and ww2 is 2026-01-12 (W02) -> two distinct weeks.
    assert len(haiku_buckets) == 2, (
        f"expected 2 distinct week buckets, got: {haiku_buckets}"
    )
    # Bucket keys must differ.
    assert haiku_buckets[0] != haiku_buckets[1]
    # Bucket keys must look like ISO week strings (YYYY-Www).
    for b in haiku_buckets:
        assert b.startswith("2026-W"), f"unexpected week bucket: {b!r}"


def test_week_spend_reconciles(multi_span_db):
    """Each weekly haiku bucket has spend equal to the single row's cost."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-01-01", "to": "2026-01-31", "granularity": "week"}
    )
    body = r.json()
    rows = multi_span_db["rows"]
    haiku_series = sorted(
        [s for s in body["series"] if s["model"] == "claude-haiku-4-5"],
        key=lambda s: s["bucket"]
    )
    assert len(haiku_series) == 2
    # ww1 and ww2 have identical params so identical cost
    expected_per_week = rows["ww1"]["cost_usd"]
    for s in haiku_series:
        assert abs(s["spend_usd"] - expected_per_week) < 1e-9, (
            f"week spend mismatch in {s['bucket']}: {s['spend_usd']} vs {expected_per_week}"
        )


def test_week_each_bucket_has_one_call(multi_span_db):
    """Each week bucket for haiku has call_count == 1 (one row each)."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-01-01", "to": "2026-01-31", "granularity": "week"}
    )
    body = r.json()
    for s in body["series"]:
        if s["model"] == "claude-haiku-4-5":
            assert s["call_count"] == 1, (
                f"expected 1 call in bucket {s['bucket']}, got {s['call_count']}"
            )


# --- granularity=month -------------------------------------------------------

def test_month_distinct_buckets(multi_span_db):
    """mm1 (Feb) and mm2 (Mar) are different months -> distinct buckets."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-02-01", "to": "2026-03-31", "granularity": "month"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["granularity"] == "month"

    opus_buckets = sorted(
        s["bucket"] for s in body["series"] if s["model"] == "claude-opus-4-8"
    )
    assert len(opus_buckets) == 2, (
        f"expected 2 month buckets, got: {opus_buckets}"
    )
    assert "2026-02" in opus_buckets
    assert "2026-03" in opus_buckets
    # Bucket keys must be 7 chars (YYYY-MM).
    for b in opus_buckets:
        assert len(b) == 7, f"unexpected month bucket format: {b!r}"


def test_month_spend_reconciles(multi_span_db):
    """Feb bucket spend == mm1.cost; Mar bucket spend == mm2.cost."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-02-01", "to": "2026-03-31", "granularity": "month"}
    )
    body = r.json()
    rows = multi_span_db["rows"]
    by_bucket = {s["bucket"]: s for s in body["series"] if s["model"] == "claude-opus-4-8"}

    assert abs(by_bucket["2026-02"]["spend_usd"] - rows["mm1"]["cost_usd"]) < 1e-9
    assert abs(by_bucket["2026-03"]["spend_usd"] - rows["mm2"]["cost_usd"]) < 1e-9


def test_month_tokens_reconciles(multi_span_db):
    """Feb bucket tokens == mm1 tokens; Mar bucket tokens == mm2 tokens."""
    r = multi_span_db["client"].get(
        "/api/timeseries",
        params={"from": "2026-02-01", "to": "2026-03-31", "granularity": "month"}
    )
    body = r.json()
    by_bucket = {s["bucket"]: s for s in body["series"] if s["model"] == "claude-opus-4-8"}

    # mm1: 800 in + 100 out = 900
    assert by_bucket["2026-02"]["tokens"] == 900
    # mm2: 600 in + 80 out = 680
    assert by_bucket["2026-03"]["tokens"] == 680


def test_month_split_by_model_preserved(multi_span_db):
    """All series rows carry a model key (split-by-model is always present)."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={**_WIDE, "granularity": "month"}
    )
    body = r.json()
    for s in body["series"]:
        assert "model" in s, f"series row missing 'model' key: {s}"
        assert s["model"] is not None


# --- invalid granularity -> 4xx ---------------------------------------------

def test_invalid_granularity_returns_4xx(multi_span_db):
    """An unknown granularity must be a clean 4xx JSON error, never a 500."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={"granularity": "fortnight"}
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}"
    body = r.json()
    assert "error" in body, f"expected 'error' key in {body}"


@pytest.mark.parametrize("bad", ["", "minute", "quarter", "yearly", "HOUR", "Day"])
def test_invalid_granularity_variants_all_4xx(multi_span_db, bad):
    """Several invalid values all produce 400, not 200 or 500."""
    r = multi_span_db["client"].get(
        "/api/timeseries", params={"granularity": bad}
    )
    assert r.status_code == 400, (
        f"granularity={bad!r}: expected 400 but got {r.status_code}"
    )
    assert "error" in r.json()
