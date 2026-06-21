"""API tests for the Phase 5 server (SPEC.md §7, build-order item 5).

Drives the FastAPI app via ``fastapi.testclient.TestClient`` against a temp DB
seeded through ``core.db`` so ``cost_usd`` is the real pricing-engine value
(never hand-faked). Covers:

* /api/summary totals + cache-efficiency math (§6.3)
* /api/breakdown grouping for each ``by`` value
* /api/timeseries day buckets, split by model
* /api/health shape (§7)
* from / to / project / model filters actually narrowing
* default last-30-day range (an old row is excluded)
* bad params -> clean 4xx (never 500)
* read-only DB + 127.0.0.1-only binding contract

Run:
  uv run --with pytest --with fastapi --with httpx pytest tests/ -q
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.config import load_pricing
from core.db import init_db, insert_usage_event
from core.pricing import compute_cost
from server.app import LOCALHOST, build_app

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"


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
    assert inserted
    return event


@pytest.fixture
def pricing():
    return load_pricing(PRICING_PATH)


@pytest.fixture
def seeded(tmp_path, pricing):
    """A temp DB with four known rows, plus the app/client wired to it.

    Three rows fall inside the last 30 days (recent); one row is ~100 days old
    so the default range excludes it.
    """
    db_path = tmp_path / "clauditor.db"
    conn = init_db(db_path)

    now = _dt.datetime.now(tz=_dt.timezone.utc)
    recent_a = now - _dt.timedelta(days=1)
    # recent_b and recent_c must share a UTC calendar day so the timeseries
    # day-bucket test merges them. Anchoring both to NOON (and 12:30) of the
    # day two days ago guarantees they never straddle a UTC midnight regardless
    # of the wall-clock time the suite runs at -- whereas a plain ``now - 2d``
    # / ``now - 2d - 1h`` pair would split across days in the [00:00, 01:00)
    # UTC window. The day is still ~2 days ago, so both rows remain inside the
    # server's real-clock default 30-day range.
    two_days_ago = (now - _dt.timedelta(days=2)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    recent_b = two_days_ago
    recent_c = two_days_ago - _dt.timedelta(minutes=30)  # same UTC day as recent_b
    old = now - _dt.timedelta(days=100)

    rows = {}
    rows["a"] = _seed_row(
        conn, pricing,
        event_uid="a", ts=_iso(recent_a),
        source="claude_code", project="alpha", model="claude-opus-4-8",
        input_tokens=1000, output_tokens=200, cache_read_tokens=4000,
    )
    rows["b"] = _seed_row(
        conn, pricing,
        event_uid="b", ts=_iso(recent_b),
        source="api", project="beta", model="claude-sonnet-4-6",
        input_tokens=300, output_tokens=50,
    )
    rows["c"] = _seed_row(
        conn, pricing,
        event_uid="c", ts=_iso(recent_c),
        source="api", project="beta", model="claude-sonnet-4-6",
        input_tokens=100, output_tokens=10,
    )
    rows["old"] = _seed_row(
        conn, pricing,
        event_uid="old", ts=_iso(old),
        source="claude_code", project="alpha", model="claude-opus-4-8",
        input_tokens=999999, output_tokens=999999,
    )
    conn.commit()
    conn.close()

    app = build_app(db_path=db_path, pricing_updated=pricing["updated"])
    client = TestClient(app)

    class Seeded:
        pass

    Seeded.client = client
    Seeded.db_path = db_path
    Seeded.rows = rows
    Seeded.now = now
    Seeded.recent_dates = (recent_a, recent_b, recent_c)
    Seeded.pricing = pricing
    return Seeded


# --- summary ----------------------------------------------------------------

def test_summary_totals_default_range_excludes_old(seeded):
    """Default range = last 30 days; the 100-day-old row is excluded (§7)."""
    r = seeded.client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()

    # Only the three recent rows counted.
    assert body["call_count"] == 3

    expected_spend = round(
        sum(seeded.rows[k]["cost_usd"] for k in ("a", "b", "c")), 6
    )
    assert body["total_spend_usd"] == expected_spend

    # total_tokens sums all four token kinds across recent rows.
    expected_tokens = (
        (1000 + 200 + 0 + 4000)  # row a
        + (300 + 50)             # row b
        + (100 + 10)             # row c
    )
    assert body["total_tokens"] == expected_tokens


def test_summary_cache_efficiency_math(seeded):
    """cache_read / (input + cache_read) over the window (§6.3)."""
    r = seeded.client.get("/api/summary")
    body = r.json()

    input_sum = 1000 + 300 + 100
    cache_read_sum = 4000
    expected = round(cache_read_sum / (input_sum + cache_read_sum), 6)
    assert body["cache_efficiency"] == expected


def test_summary_empty_db_returns_zeros(tmp_path, pricing):
    """No matching rows -> well-formed zeros, not an error (§7)."""
    db_path = tmp_path / "empty.db"
    init_db(db_path).close()
    client = TestClient(build_app(db_path=db_path, pricing_updated=pricing["updated"]))

    body = client.get("/api/summary").json()
    assert body["call_count"] == 0
    assert body["total_spend_usd"] == 0
    assert body["total_tokens"] == 0
    # Divide-by-zero guard: efficiency is 0.0, not an error.
    assert body["cache_efficiency"] == 0.0


# --- filters ----------------------------------------------------------------

def test_project_filter_narrows(seeded):
    body = seeded.client.get("/api/summary", params={"project": "beta"}).json()
    assert body["call_count"] == 2  # rows b + c
    assert body["filters"]["project"] == "beta"


def test_model_filter_narrows(seeded):
    body = seeded.client.get(
        "/api/summary", params={"model": "claude-opus-4-8"}
    ).json()
    assert body["call_count"] == 1  # only row a (old opus row is out of range)


def test_explicit_from_to_includes_old_row(seeded):
    """An explicit wide range pulls in the otherwise-excluded old row."""
    start = _iso(seeded.now - _dt.timedelta(days=200))
    end = _iso(seeded.now + _dt.timedelta(days=1))
    body = seeded.client.get(
        "/api/summary", params={"from": start, "to": end}
    ).json()
    assert body["call_count"] == 4  # all rows now in range


def test_to_filter_narrows(seeded):
    """A 'to' before the recent rows excludes them."""
    end = _iso(seeded.now - _dt.timedelta(days=50))
    start = _iso(seeded.now - _dt.timedelta(days=200))
    body = seeded.client.get(
        "/api/summary", params={"from": start, "to": end}
    ).json()
    assert body["call_count"] == 1  # only the old row


# --- breakdown --------------------------------------------------------------

def test_breakdown_by_project(seeded):
    body = seeded.client.get("/api/breakdown", params={"by": "project"}).json()
    assert body["by"] == "project"
    keys = {g["key"]: g for g in body["groups"]}
    assert set(keys) == {"alpha", "beta"}
    assert keys["beta"]["call_count"] == 2
    assert keys["alpha"]["call_count"] == 1


def test_breakdown_by_model(seeded):
    body = seeded.client.get("/api/breakdown", params={"by": "model"}).json()
    keys = {g["key"]: g for g in body["groups"]}
    assert set(keys) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    assert keys["claude-sonnet-4-6"]["call_count"] == 2


def test_breakdown_by_source(seeded):
    body = seeded.client.get("/api/breakdown", params={"by": "source"}).json()
    keys = {g["key"]: g for g in body["groups"]}
    assert set(keys) == {"claude_code", "api"}
    assert keys["api"]["call_count"] == 2
    assert keys["claude_code"]["call_count"] == 1


def test_breakdown_bad_by_is_4xx(seeded):
    r = seeded.client.get("/api/breakdown", params={"by": "wizard"})
    assert r.status_code == 400
    assert "error" in r.json()


# --- timeseries -------------------------------------------------------------

def test_timeseries_day_buckets_split_by_model(seeded):
    body = seeded.client.get(
        "/api/timeseries", params={"granularity": "day"}
    ).json()
    assert body["granularity"] == "day"

    # Rows b and c share a day + model -> one merged bucket of 2 calls.
    merged = [
        s for s in body["series"]
        if s["model"] == "claude-sonnet-4-6"
    ]
    assert len(merged) == 1
    assert merged[0]["call_count"] == 2

    # Bucket keys are YYYY-MM-DD (length 10).
    for s in body["series"]:
        assert len(s["bucket"]) == 10


def test_timeseries_default_granularity_is_day(seeded):
    body = seeded.client.get("/api/timeseries").json()
    assert body["granularity"] == "day"


def test_timeseries_bad_granularity_is_4xx(seeded):
    r = seeded.client.get("/api/timeseries", params={"granularity": "fortnight"})
    assert r.status_code == 400
    assert "error" in r.json()


# --- health -----------------------------------------------------------------

def test_health_shape(seeded):
    body = seeded.client.get("/api/health").json()
    assert set(body) >= {"status", "db_path", "event_count", "pricing_updated"}
    assert body["status"] == "ok"
    assert body["event_count"] == 4  # health counts all rows, no range filter
    assert body["db_path"] == str(seeded.db_path)
    assert body["pricing_updated"] == seeded.pricing["updated"]


# --- bad dates --------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["/api/summary", "/api/timeseries", "/api/breakdown"])
def test_bad_from_date_is_4xx_not_500(seeded, endpoint):
    r = seeded.client.get(endpoint, params={"from": "not-a-date"})
    assert r.status_code == 400
    assert r.status_code != 500
    assert "error" in r.json()


def test_inverted_range_is_4xx(seeded):
    start = _iso(seeded.now)
    end = _iso(seeded.now - _dt.timedelta(days=10))
    r = seeded.client.get("/api/summary", params={"from": start, "to": end})
    assert r.status_code == 400


# --- safety contracts -------------------------------------------------------

def test_binding_host_is_loopback_only():
    """The server's bind host is hardcoded to loopback (SPEC.md §7/§11)."""
    assert LOCALHOST == "127.0.0.1"


def test_endpoints_do_not_mutate_db(seeded):
    """Hitting the read endpoints must not change the row count (read-only)."""
    seeded.client.get("/api/summary")
    seeded.client.get("/api/breakdown", params={"by": "model"})
    seeded.client.get("/api/timeseries")
    body = seeded.client.get("/api/health").json()
    assert body["event_count"] == 4
