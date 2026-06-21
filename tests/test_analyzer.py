"""Analyzer tests: savings rules + budget alerts (SPEC.md §6, §12).

Covers, against TEMP DBs only (never the real data/clauditor.db):

* Each savings rule fires on its planted target, and the reported
  ``estimated_monthly_savings_usd`` equals a value recomputed independently here
  from the planted numbers (reproducibility, not just > 0).
* A CLEAN DB (no qualifying pattern) yields ZERO suggestions from all three
  rules (false-positive resistance).
* Budget alerts fire once per (scope, period, period_key-with-fraction): 0.8 and
  1.0 each fire once; a second analyze run adds NO new rows (alerts_log UNIQUE).
* The /api/suggestions and /api/alerts endpoints (cards, gauges, fraction math,
  missing-DB -> empty not 500, bad params -> 4xx) via FastAPI TestClient.

Run:
  uv run --with pytest --with fastapi --with httpx --with uvicorn pytest tests/ -q
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.analyzer import (
    DOWNGRADE_TARGET_MODEL,
    analyze_and_fire,
    budget_status,
    evaluate_budget_alerts,
    rule_batch_candidates,
    rule_missing_prompt_cache,
    rule_model_downgrade,
    savings_suggestions,
)
from core.config import load_pricing
from core.db import init_db, insert_usage_event
from core.pricing import compute_cost
from server.app import build_app

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


@pytest.fixture
def pricing():
    return load_pricing(PRICING_PATH)


def _seed(conn, pricing, *, uid, ts, project, model, source="claude_code",
          input_tokens=0, output_tokens=0, cache_read_tokens=0,
          cache_creation_tokens=0, is_batch=0, cache_ttl=None):
    event = {
        "event_uid": uid,
        "ts": ts,
        "source": source,
        "project": project,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "is_batch": is_batch,
        "cache_ttl": cache_ttl,
        "session_id": "s",
        "raw_meta": None,
    }
    event["cost_usd"] = compute_cost(event, pricing)
    assert insert_usage_event(conn, event)
    return event


# ---------------------------------------------------------------------------
# Planted-pattern fixtures
# ---------------------------------------------------------------------------

# A fixed 30-day window so monthly scaling is exactly 1.0 (30/30).
_NOW = _dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)


@pytest.fixture
def planted_db(tmp_path, pricing):
    """Temp DB with one planted pattern per rule, inside the lookback window."""
    db_path = tmp_path / "clauditor.db"
    conn = init_db(db_path)

    base = _NOW - _dt.timedelta(days=10)  # well within last 30 days

    # Rule 1: 120 Opus calls in 'etl-pipeline', tiny + identical outputs/inputs.
    for i in range(120):
        _seed(
            conn, pricing,
            uid=f"dg-{i}", ts=_iso(base + _dt.timedelta(seconds=i)),
            project="etl-pipeline", model="claude-opus-4-8",
            input_tokens=1000, output_tokens=100,
        )

    # Rule 2: 20 large uncached identical-size sonnet calls in 'support-bot'.
    for i in range(20):
        _seed(
            conn, pricing,
            uid=f"cache-{i}", ts=_iso(base + _dt.timedelta(minutes=i)),
            project="support-bot", model="claude-sonnet-4-6",
            input_tokens=8000, output_tokens=200, cache_read_tokens=0,
        )

    # Rule 3: 150 synchronous api calls in 'nightly-report' within ~2.5 min.
    # Small input (500) so Rule 2 does NOT also catch them (< 2000 floor).
    for i in range(150):
        _seed(
            conn, pricing,
            uid=f"batch-{i}", ts=_iso(base + _dt.timedelta(seconds=i)),
            project="nightly-report", model="claude-sonnet-4-6", source="api",
            input_tokens=500, output_tokens=100, is_batch=0,
        )

    conn.commit()
    return db_path, conn


@pytest.fixture
def clean_db(tmp_path, pricing):
    """Temp DB with traffic that matches NO rule (false-positive resistance)."""
    db_path = tmp_path / "clean.db"
    conn = init_db(db_path)
    base = _NOW - _dt.timedelta(days=5)

    # Opus but only 10 calls (< MIN_CALLS) and big outputs (>= 300) -> no Rule 1.
    for i in range(10):
        _seed(
            conn, pricing,
            uid=f"c1-{i}", ts=_iso(base + _dt.timedelta(minutes=i)),
            project="big-opus", model="claude-opus-4-8",
            input_tokens=1000, output_tokens=2000,
        )
    # Large inputs but already cached (cache_read > 0) -> no Rule 2.
    for i in range(20):
        _seed(
            conn, pricing,
            uid=f"c2-{i}", ts=_iso(base + _dt.timedelta(minutes=i)),
            project="cached-bot", model="claude-sonnet-4-6",
            input_tokens=8000, output_tokens=200, cache_read_tokens=8000,
        )
    # api calls but spread out (1/hour) -> no dense burst -> no Rule 3.
    for i in range(150):
        _seed(
            conn, pricing,
            uid=f"c3-{i}", ts=_iso(base + _dt.timedelta(hours=i)),
            project="slow-api", model="claude-sonnet-4-6", source="api",
            input_tokens=500, output_tokens=100, is_batch=0,
        )
    conn.commit()
    return db_path, conn


# ---------------------------------------------------------------------------
# Rule 1 -- model downgrade (reproducible savings)
# ---------------------------------------------------------------------------

def test_rule1_downgrade_reproducible(planted_db, pricing):
    _, conn = planted_db
    suggestions = savings_suggestions(conn, _config(), pricing, now=_NOW)
    matches = [s for s in suggestions if "etl-pipeline" in s["title"]]
    assert len(matches) == 1
    s = matches[0]
    assert s["confidence"] == "high"

    # Recompute by hand. Opus 4.8 $5/$25; Haiku 4.5 $1/$5; per million.
    opus_per = (1000 * 5.0 + 100 * 25.0) / 1_000_000   # 0.0075
    haiku_per = (1000 * 1.0 + 100 * 5.0) / 1_000_000   # 0.0015
    delta = (opus_per - haiku_per) * 120               # 0.72
    # Window is exactly 30 days -> monthly factor 30/30 = 1.0.
    assert s["estimated_monthly_savings_usd"] == round(delta, 2)
    assert DOWNGRADE_TARGET_MODEL == "claude-haiku-4-5"


def test_rule1_unit_function(planted_db, pricing):
    _, conn = planted_db
    rows = conn.execute(
        "SELECT * FROM usage_events WHERE project = 'etl-pipeline'"
    ).fetchall()
    out = rule_model_downgrade(rows, pricing, span_days=30.0)
    assert len(out) == 1
    assert out[0]["estimated_monthly_savings_usd"] == 0.72


# ---------------------------------------------------------------------------
# Rule 2 -- missing prompt cache (reproducible savings)
# ---------------------------------------------------------------------------

def test_rule2_cache_reproducible(planted_db, pricing):
    _, conn = planted_db
    suggestions = savings_suggestions(conn, _config(), pricing, now=_NOW)
    matches = [s for s in suggestions if "support-bot" in s["title"]]
    assert len(matches) == 1
    s = matches[0]
    assert s["confidence"] == "medium"

    in_rate = 3.0  # sonnet input rate
    repeated = 19 * 8000                                  # all calls after the first
    gross = repeated * in_rate * 0.90 / 1_000_000         # 0.4104
    write = 8000 * in_rate * 1.25 / 1_000_000             # 0.03
    net = gross - write                                   # 0.3804
    assert s["estimated_monthly_savings_usd"] == round(net, 2)


# ---------------------------------------------------------------------------
# Rule 3 -- batch candidates (reproducible savings)
# ---------------------------------------------------------------------------

def test_rule3_batch_reproducible(planted_db, pricing):
    _, conn = planted_db
    suggestions = savings_suggestions(conn, _config(), pricing, now=_NOW)
    matches = [s for s in suggestions if "nightly-report" in s["title"]]
    assert len(matches) == 1
    s = matches[0]

    per = (500 * 3.0 + 100 * 15.0) / 1_000_000            # sonnet 0.003
    total = per * 150                                     # 0.45
    savings = total * 0.50                                # 0.225
    assert s["estimated_monthly_savings_usd"] == round(savings, 2)


def test_all_three_fire_on_planted(planted_db, pricing):
    _, conn = planted_db
    suggestions = savings_suggestions(conn, _config(), pricing, now=_NOW)
    titles = " ".join(s["title"] for s in suggestions)
    assert "etl-pipeline" in titles
    assert "support-bot" in titles
    assert "nightly-report" in titles
    # Sorted by savings descending.
    vals = [s["estimated_monthly_savings_usd"] for s in suggestions]
    assert vals == sorted(vals, reverse=True)


# ---------------------------------------------------------------------------
# False-positive resistance (clean DB -> zero suggestions)
# ---------------------------------------------------------------------------

def test_clean_db_zero_suggestions(clean_db, pricing):
    _, conn = clean_db
    suggestions = savings_suggestions(conn, _config(), pricing, now=_NOW)
    assert suggestions == []

    rows = conn.execute("SELECT * FROM usage_events").fetchall()
    assert rule_model_downgrade(rows, pricing, 30.0) == []
    assert rule_missing_prompt_cache(rows, pricing, 30.0) == []
    assert rule_batch_candidates(rows, pricing, 30.0) == []


# ---------------------------------------------------------------------------
# Budget alerts: fire-once + both fractions
# ---------------------------------------------------------------------------

def _config(monthly_budget=None):
    cfg = {
        "lookback_days": 30,
        "alert_fractions": [0.8, 1.0],
        "alert_webhook_url": None,
        "desktop_notifications": False,
        "budgets": {"global": {"daily": None, "weekly": None, "monthly": None},
                    "projects": {}},
    }
    if monthly_budget is not None:
        cfg["budgets"]["global"]["monthly"] = monthly_budget
    return cfg


@pytest.fixture
def budget_db(tmp_path, pricing):
    """Temp DB whose current-month global spend crosses both 0.8 and 1.0."""
    db_path = tmp_path / "budget.db"
    conn = init_db(db_path)

    # Spend exactly $10 in the current month: 1 opus call.
    # opus per-call here is engineered to a round number via tokens.
    # input=2,000,000 tokens * $5/M = $10.00 ; output 0.
    _seed(
        conn, pricing,
        uid="spend", ts=_iso(_NOW), project="x", model="claude-opus-4-8",
        input_tokens=2_000_000, output_tokens=0,
    )
    conn.commit()
    return db_path, conn


def test_budget_both_fractions_fire_once(budget_db, pricing):
    _, conn = budget_db
    # Budget $10 monthly; spend is $10 -> crosses 0.8 ($8) AND 1.0 ($10).
    cfg = _config(monthly_budget=10)

    crossings = evaluate_budget_alerts(conn, cfg, _NOW)
    fracs = sorted(c["fraction"] for c in crossings)
    assert fracs == [0.8, 1.0]

    fired = analyze_and_fire(conn, cfg, now=_NOW, deliver=False)
    assert len(fired) == 2

    rows = conn.execute(
        "SELECT scope, period, period_key, threshold FROM alerts_log "
        "ORDER BY period_key"
    ).fetchall()
    assert len(rows) == 2
    keys = sorted(r["period_key"] for r in rows)
    # Fraction encoded into period_key so UNIQUE(scope,period,period_key) holds.
    assert keys == ["2026-06@0.8", "2026-06@1"]
    thresholds = sorted(r["threshold"] for r in rows)
    assert thresholds == [8.0, 10.0]

    # Re-run: nothing new (fire-once via alerts_log UNIQUE).
    fired_again = analyze_and_fire(conn, cfg, now=_NOW, deliver=False)
    assert fired_again == []
    n = conn.execute("SELECT COUNT(*) AS n FROM alerts_log").fetchone()["n"]
    assert n == 2


def test_budget_only_lower_fraction_fires(budget_db, pricing):
    _, conn = budget_db
    # Budget $12.5 -> 0.8 threshold $10 met (spend $10), 1.0 threshold $12.5 not.
    cfg = _config(monthly_budget=12.5)
    fired = analyze_and_fire(conn, cfg, now=_NOW, deliver=False)
    fracs = sorted(f["fraction"] for f in fired)
    assert fracs == [0.8]


def test_budget_status_gauges(budget_db, pricing):
    _, conn = budget_db
    cfg = _config(monthly_budget=20)  # spend $10 -> 50% used
    statuses = budget_status(conn, cfg, _NOW)
    monthly = [s for s in statuses if s["period"] == "monthly" and s["scope"] == "global"]
    assert len(monthly) == 1
    s = monthly[0]
    assert s["budget"] == 20.0
    assert s["spend"] == 10.0
    assert s["fraction_used"] == 0.5
    assert s["level"] == "ok"


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

def test_api_suggestions_returns_cards(planted_db, pricing):
    db_path, conn = planted_db
    # Build the app with a config whose lookback covers the planted window.
    client = TestClient(build_app(db_path=db_path, pricing=pricing, config=_config()))
    # The analyzer uses "now" = system clock; widen via explicit from/to to the
    # planted window so the deterministic _NOW data is in range.
    frm = (_NOW - _dt.timedelta(days=30)).date().isoformat()
    to = (_NOW + _dt.timedelta(days=1)).date().isoformat()
    r = client.get("/api/suggestions", params={"from": frm, "to": to})
    assert r.status_code == 200
    body = r.json()
    assert "suggestions" in body
    titles = " ".join(s["title"] for s in body["suggestions"])
    assert "etl-pipeline" in titles
    for s in body["suggestions"]:
        assert "estimated_monthly_savings_usd" in s
        assert "confidence" in s


def test_api_suggestions_bad_params(planted_db, pricing):
    db_path, _ = planted_db
    client = TestClient(build_app(db_path=db_path, pricing=pricing, config=_config()))
    r = client.get("/api/suggestions?from=not-a-date")
    assert r.status_code == 400
    assert "error" in r.json()


def test_api_suggestions_missing_db_empty(tmp_path, pricing):
    missing = tmp_path / "nope.db"
    client = TestClient(build_app(db_path=missing, pricing=pricing, config=_config()))
    r = client.get("/api/suggestions")
    assert r.status_code == 200
    assert r.json()["suggestions"] == []


def test_api_alerts_gauges_and_alerts(budget_db, pricing):
    db_path, conn = budget_db
    cfg = _config(monthly_budget=20)
    # Persist alerts first (as the ingest step would).
    analyze_and_fire(conn, cfg, now=_NOW, deliver=False)

    client = TestClient(build_app(db_path=db_path, pricing=pricing, config=cfg))
    r = client.get("/api/alerts")
    assert r.status_code == 200
    body = r.json()

    # Gauges: fraction-used math = spend/budget = 10/20 = 0.5.
    monthly = [
        b for b in body["budgets"]
        if b["period"] == "monthly" and b["scope"] == "global"
    ]
    assert len(monthly) == 1
    assert monthly[0]["fraction_used"] == 0.5
    assert monthly[0]["budget"] == 20.0
    assert monthly[0]["spend"] == 10.0

    # Fired alerts present (0.8 crossed since spend $10 >= 0.8*$20=$16? no).
    # $10 < $16, so at $20 budget neither fraction crosses -> no alerts here.
    assert body["alerts"] == []


def test_api_alerts_reports_fired(budget_db, pricing):
    db_path, conn = budget_db
    cfg = _config(monthly_budget=10)  # spend $10 crosses both
    analyze_and_fire(conn, cfg, now=_NOW, deliver=False)

    client = TestClient(build_app(db_path=db_path, pricing=pricing, config=cfg))
    body = client.get("/api/alerts").json()
    fracs = sorted(a["fraction"] for a in body["alerts"])
    assert fracs == [0.8, 1.0]


def test_api_alerts_missing_db_empty(tmp_path, pricing):
    missing = tmp_path / "nope.db"
    client = TestClient(build_app(db_path=missing, pricing=pricing, config=_config(10)))
    r = client.get("/api/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body["alerts"] == []
    assert body["budgets"] == []


def test_api_alerts_does_not_fire(budget_db, pricing):
    """/api/alerts is a pure read -- it must NOT persist alerts."""
    db_path, conn = budget_db
    cfg = _config(monthly_budget=10)
    client = TestClient(build_app(db_path=db_path, pricing=pricing, config=cfg))
    # Hit the endpoint several times WITHOUT calling analyze_and_fire.
    for _ in range(3):
        client.get("/api/alerts")
    n = conn.execute("SELECT COUNT(*) AS n FROM alerts_log").fetchone()["n"]
    assert n == 0
