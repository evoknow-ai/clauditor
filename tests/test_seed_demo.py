"""Tests for seed_demo.py (SPEC.md §12 last bullet, §13 deliverable 1).

Runs the demo seeder against a TEMP db only (never the real data/clauditor.db)
and asserts:

* Rows are actually inserted (and re-running is deduped, not double-counted).
* The analyzer over the seeded DB yields >= 1 suggestion -- and in fact all
  THREE rule types fire (downgrade, missing-cache, batch), proving the planted
  patterns match the analyzer's live thresholds.
* The summary the script returns is self-consistent (counts + spend > 0).

Run:
  uv run --with pytest --with fastapi --with httpx --with uvicorn pytest tests/ -q
"""

from __future__ import annotations

from pathlib import Path

import seed_demo
from core.analyzer import savings_suggestions
from core.config import load_pricing
from core.db import event_count, get_connection

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"
DEFAULT_LOOKBACK_CONFIG = {"lookback_days": 30}


def test_seed_inserts_rows(tmp_path):
    db_path = tmp_path / "demo.db"
    summary = seed_demo.seed(db_path, PRICING_PATH)

    assert summary["inserted"] > 0
    assert summary["total_spend_usd"] > 0

    conn = get_connection(db_path)
    try:
        assert event_count(conn) == summary["inserted"]
    finally:
        conn.close()


def test_seed_is_idempotent(tmp_path):
    """A second seed run inserts nothing new (event_uid dedupe)."""
    db_path = tmp_path / "demo.db"
    first = seed_demo.seed(db_path, PRICING_PATH)
    second = seed_demo.seed(db_path, PRICING_PATH)

    assert first["inserted"] > 0
    assert second["inserted"] == 0

    conn = get_connection(db_path)
    try:
        assert event_count(conn) == first["inserted"]
    finally:
        conn.close()


def test_seed_makes_all_three_rules_fire(tmp_path):
    db_path = tmp_path / "demo.db"
    seed_demo.seed(db_path, PRICING_PATH)
    pricing = load_pricing(PRICING_PATH)

    conn = get_connection(db_path)
    try:
        suggestions = savings_suggestions(conn, DEFAULT_LOOKBACK_CONFIG, pricing)
    finally:
        conn.close()

    assert len(suggestions) >= 1
    titles = " ".join(s["title"] for s in suggestions)
    # Rule 1 (downgrade), Rule 2 (cache), Rule 3 (batch) all present.
    assert "etl-pipeline" in titles
    assert "support-bot" in titles
    assert "nightly-report" in titles
    # Every suggestion carries a positive dollar estimate.
    for s in suggestions:
        assert s["estimated_monthly_savings_usd"] > 0


def test_seed_has_cache_read_for_efficiency_metric(tmp_path):
    """The summary panel's cache-efficiency must be a meaningful non-zero %."""
    db_path = tmp_path / "demo.db"
    seed_demo.seed(db_path, PRICING_PATH)

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(cache_read_tokens) AS cr, SUM(input_tokens) AS it "
            "FROM usage_events"
        ).fetchone()
    finally:
        conn.close()

    cache_read = int(row["cr"] or 0)
    input_tokens = int(row["it"] or 0)
    assert cache_read > 0
    efficiency = cache_read / (input_tokens + cache_read)
    assert 0.0 < efficiency < 1.0


def test_seed_has_multiple_sources_and_projects(tmp_path):
    """Breakdown panels need variety across source + project + model."""
    db_path = tmp_path / "demo.db"
    summary = seed_demo.seed(db_path, PRICING_PATH)

    assert set(summary["by_source"].keys()) >= {"claude_code", "api"}
    assert len(summary["by_project"]) >= 4
