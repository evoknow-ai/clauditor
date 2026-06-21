"""Seed the clauditor database with synthetic demo data (SPEC.md §12, §13, §14).

Run this to populate the dashboard WITHOUT any real ``~/.claude`` history::

    python seed_demo.py                 # writes into data/clauditor.db
    python seed_demo.py --db /tmp/x.db  # writes into a throwaway DB
    clauditor serve                     # open the populated dashboard

The synthetic rows are inserted through ``core.db`` and priced with the real
``core.pricing.compute_cost`` (never hardcoded), so every dollar figure on the
dashboard is internally consistent. The dataset is designed to exercise EVERY
panel -- summary (with real cache-read so cache-efficiency is non-zero),
timeseries (multiple models across many days), breakdown (multiple
projects/models/sources) -- and to make ALL THREE savings rules fire:

* **Rule 1 (model downgrade)** -- ``etl-pipeline``: 120 Opus 4.8 calls with tiny
  identical outputs (100 tok) and identical inputs (1000 tok). Meets
  ``MIN_CALLS=100``, ``median_output < 300``, and CV=0 <= ``MAX_INPUT_CV``.
* **Rule 2 (missing prompt cache)** -- ``support-bot``: 30 Sonnet calls with a
  fat 8000-token uncached input, all in the same 500-token bucket. Meets
  ``input >= 2000``, ``cache_read == 0``, repeats >= ``CACHE_MIN_REPEATS=10``.
* **Rule 3 (batch candidates)** -- ``nightly-report``: 150 synchronous
  ``source='api'`` ``is_batch=0`` Sonnet calls clustered inside ~2.5 minutes
  (< the 60-min window) -- meets ``BATCH_MIN_CALLS=100``.

Timestamps are spread across the last ~30 days, anchored to a recent UTC instant
(start of the current hour) so the rows always land inside the default lookback
window regardless of when the demo is run.

Safety (SPEC.md §11): writes ONLY to the chosen SQLite file (default
``data/clauditor.db``), makes no network calls, and never touches ``~/.claude``.
It is re-runnable: each row carries a stable ``event_uid`` so a second run is
deduped by the UNIQUE(event_uid) constraint rather than double-counting.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
from pathlib import Path
from typing import Any

from core.config import load_pricing
from core.db import init_db, insert_usage_event
from core.pricing import compute_cost


def _uid(*parts: Any) -> str:
    """Stable dedupe key so re-running the seed never double-counts."""
    raw = "\x1f".join(["seed_demo", *(str(p) for p in parts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


def _anchor_now() -> _dt.datetime:
    """A recent UTC anchor: the start of the current hour.

    Anchoring (rather than using the exact instant) keeps the dataset stable
    within an hour while still landing every row inside the rolling 30-day
    lookback window whenever the demo is run.
    """
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def seed(db_path: str | Path | None, pricing_path: str | Path | None) -> dict[str, Any]:
    """Insert the synthetic demo dataset; return a summary of what was written.

    Returns ``{"inserted": int, "by_source": {...}, "by_project": {...},
    "total_spend_usd": float, "db_path": str}``.
    """
    pricing = load_pricing(pricing_path)
    conn = init_db(db_path)

    now = _anchor_now()
    inserted = 0
    by_source: dict[str, int] = {}
    by_project: dict[str, int] = {}
    total_spend = 0.0

    def add(
        *,
        tag: str,
        i: int,
        ts: _dt.datetime,
        project: str,
        model: str,
        source: str = "claude_code",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        is_batch: int = 0,
        cache_ttl: str | None = None,
    ) -> None:
        nonlocal inserted, total_spend
        event = {
            "event_uid": _uid(tag, i),
            "ts": _iso(ts),
            "source": source,
            "project": project,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "is_batch": is_batch,
            "cache_ttl": cache_ttl,
            "session_id": f"{tag}-session",
            "raw_meta": None,
        }
        event["cost_usd"] = compute_cost(event, pricing)
        if insert_usage_event(conn, event):
            inserted += 1
            total_spend += float(event["cost_usd"])
            by_source[source] = by_source.get(source, 0) + 1
            by_project[project] = by_project.get(project, 0) + 1

    # --- Rule 1: model-downgrade pattern (etl-pipeline) --------------------
    # 120 Opus 4.8 calls, identical tiny outputs + identical inputs (CV = 0).
    base1 = now - _dt.timedelta(days=12)
    for i in range(120):
        add(
            tag="downgrade", i=i,
            ts=base1 + _dt.timedelta(minutes=i),
            project="etl-pipeline", model="claude-opus-4-8",
            input_tokens=1000, output_tokens=100,
        )

    # --- Rule 2: missing-cache pattern (support-bot) ----------------------
    # 30 Sonnet calls, fat uncached 8000-token input, same 500-token bucket.
    base2 = now - _dt.timedelta(days=9)
    for i in range(30):
        add(
            tag="cache", i=i,
            ts=base2 + _dt.timedelta(hours=i),
            project="support-bot", model="claude-sonnet-4-6",
            input_tokens=8000, output_tokens=220, cache_read_tokens=0,
        )

    # --- Rule 3: batch-candidate pattern (nightly-report) -----------------
    # 150 synchronous source='api' is_batch=0 calls within ~2.5 minutes.
    base3 = now - _dt.timedelta(days=6)
    for i in range(150):
        add(
            tag="batch", i=i,
            ts=base3 + _dt.timedelta(seconds=i),
            project="nightly-report", model="claude-sonnet-4-6", source="api",
            input_tokens=600, output_tokens=120, is_batch=0,
        )

    # --- Healthy traffic so the dashboard looks real ----------------------
    # A well-cached RAG app: high cache_read so the headline cache-efficiency
    # metric is a meaningful percentage rather than 0 (SPEC.md §6.3). Spread
    # across many days/models so the timeseries has multiple buckets, and a
    # third source so the by-source breakdown has variety.
    base4 = now - _dt.timedelta(days=28)
    cached_models = ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-7"]
    for i in range(120):
        day_offset = i % 28  # spread across the whole 28-day span
        model = cached_models[i % len(cached_models)]
        add(
            tag="rag", i=i,
            ts=base4 + _dt.timedelta(days=day_offset, minutes=i),
            project="rag-search", model=model, source="api",
            input_tokens=1500, output_tokens=400,
            cache_read_tokens=6000, cache_creation_tokens=0,
            cache_ttl="5m",
        )

    # A small batch of already-batched API work + an interactive Claude Code
    # project so source/project/model breakdowns all have several slices and the
    # global monthly budget gauge has meaningful (sub-limit) spend to show.
    base5 = now - _dt.timedelta(days=3)
    for i in range(40):
        add(
            tag="webapp", i=i,
            ts=base5 + _dt.timedelta(hours=i),
            project="web-app", model="claude-haiku-4-5", source="claude_code",
            input_tokens=2200, output_tokens=600, cache_read_tokens=1800,
        )

    conn.commit()
    conn.close()

    return {
        "inserted": inserted,
        "by_source": by_source,
        "by_project": by_project,
        "total_spend_usd": round(total_spend, 2),
        "db_path": str(db_path) if db_path is not None else "data/clauditor.db",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_demo.py",
        description=(
            "Fill the clauditor database with synthetic demo data so the "
            "dashboard can be demoed without real ~/.claude history."
        ),
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="Target SQLite database (default: data/clauditor.db).",
    )
    parser.add_argument(
        "--pricing", default=None, metavar="PATH",
        help="Path to pricing.json (default: project-root pricing.json).",
    )
    args = parser.parse_args(argv)

    summary = seed(args.db, args.pricing)

    print(f"Seeded {summary['inserted']} synthetic usage events.")
    print(f"  Database:    {summary['db_path']}")
    print(f"  Total spend: ${summary['total_spend_usd']:.2f}")
    print("  By source:")
    for src, n in sorted(summary["by_source"].items()):
        print(f"    {src}: {n}")
    print("  By project:")
    for proj, n in sorted(summary["by_project"].items()):
        print(f"    {proj}: {n}")
    print()
    print("All three savings rules should now fire. Next:")
    print("  clauditor serve      # open the populated dashboard")
    print("  clauditor suggest    # see the savings suggestions in the terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
