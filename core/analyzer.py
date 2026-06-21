"""Analyzer: budget alerts + savings suggestions (SPEC.md §6).

This module is the product differentiator. It has two jobs:

* **Budget alerts** (§6.1): for each configured budget (global + per-project)
  and each period (daily / weekly / monthly), compute current-period spend and
  fire once per fraction crossed (default 0.8 and 1.0). Fires are de-duplicated
  via the ``alerts_log`` UNIQUE(scope, period, period_key) index so re-running
  ingest never re-fires the same crossing.

* **Savings suggestions** (§6.2): three rules (model downgrade, missing prompt
  cache, batch candidates) computed over a lookback window. Every dollar figure
  is derived from real rows so a reviewer can recompute it by hand -- there are
  no fabricated/hardcoded numbers.

Every function here is **pure over the ``usage_events`` table**: the analyzer
NEVER mutates usage data. The only table it writes to is ``alerts_log`` (alert
de-duplication state), and only from the explicit :func:`analyze_and_fire` step
that the ingest path calls -- never from the read-only API endpoints.

The optional alert deliveries (desktop notification via ``plyer``; webhook POST
via stdlib ``urllib``) are best-effort and degrade gracefully: a missing
``plyer`` or a failed webhook never raises and never blocks ingest.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import statistics
from typing import Any, Mapping

from core.pricing import compute_cost

# ===========================================================================
# TUNABLE THRESHOLDS (SPEC.md §6.2: "tunable constants at top of file")
# ===========================================================================

# --- Rule 1: model downgrade -----------------------------------------------
# Opus-family events whose pattern looks like cheaper (Haiku/Sonnet) work.
MEDIAN_OUTPUT_TOKENS_MAX = 300          # median output tokens below this = "small"
MIN_CALLS = 100                         # need this many Opus calls to be confident
# "Low input variance / repetitive": coefficient of variation (stdev / mean) of
# input_tokens must be at or below this for the prompts to look repetitive.
MAX_INPUT_CV = 0.5
# The cheaper model we recompute matching Opus events against (§6.2 example uses
# Haiku 4.5). Must exist in pricing.json; if absent we skip the rule.
DOWNGRADE_TARGET_MODEL = "claude-haiku-4-5"
# Model substrings that mark the "Opus family" (§6.2: "model in Opus family").
OPUS_FAMILY_MARKERS = ("opus",)

# --- Rule 2: missing prompt cache ------------------------------------------
# Recurring large-input uncached calls, grouped by (project, model, input bucket).
CACHE_INPUT_TOKENS_MIN = 2000           # "large input" floor (per call)
CACHE_BUCKET_SIZE = 500                 # bucket input_tokens to nearest 500 (§6.2)
CACHE_MIN_REPEATS = 10                  # a bucket must recur at least this often
# Cache-read saves ~90% of the input rate (cache read = 0.10x input).
CACHE_READ_SAVINGS_FRACTION = 0.90
# A 5-minute cache write costs 1.25x the input rate, paid once per recurring set.
CACHE_WRITE_5M_MULTIPLIER = 1.25

# --- Rule 3: batch candidates ----------------------------------------------
# Bursts of non-interactive source='api', is_batch=0 calls that tolerate latency.
BATCH_MIN_CALLS = 100                   # a burst must contain at least this many calls
BATCH_WINDOW_MINUTES = 60               # "short window" defining a burst
BATCH_SAVINGS_FRACTION = 0.50           # Batch API = 50% off (§6.2)

# --- Budget periods ---------------------------------------------------------
BUDGET_PERIODS = ("daily", "weekly", "monthly")


# ===========================================================================
# Period helpers
# ===========================================================================

def period_key(period: str, now: _dt.datetime) -> str:
    """Return the canonical key for the CURRENT period at ``now`` (UTC).

    * daily   -> ``YYYY-MM-DD``      (UTC calendar day)
    * weekly  -> ``YYYY-Www``        (ISO week, zero-padded)
    * monthly -> ``YYYY-MM``         (calendar month)

    These keys are what ``alerts_log.period_key`` is built from (with the fired
    fraction appended; see :func:`_dedupe_key`).
    """
    now = now.astimezone(_dt.timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    if period == "monthly":
        return now.strftime("%Y-%m")
    raise ValueError(f"unknown period: {period!r}")


def period_bounds(period: str, now: _dt.datetime) -> tuple[_dt.datetime, _dt.datetime]:
    """Return the half-open ``[start, end)`` UTC bounds of the current period.

    ``end`` is the start of the next period, so a ``ts >= start AND ts < end``
    filter selects exactly the current period's events.
    """
    now = now.astimezone(_dt.timezone.utc)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + _dt.timedelta(days=1)
    if period == "weekly":
        # ISO week starts Monday.
        start_day = now - _dt.timedelta(days=now.weekday())
        start = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + _dt.timedelta(days=7)
    if period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end
    raise ValueError(f"unknown period: {period!r}")


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


# ===========================================================================
# Spend queries (pure reads over usage_events)
# ===========================================================================

def _period_spend(
    conn: sqlite3.Connection,
    project: str | None,
    period: str,
    now: _dt.datetime,
) -> float:
    """Sum ``cost_usd`` over the current period for a scope.

    ``project=None`` means the global scope (all projects). Uses parameterized
    SQL only (SPEC.md §11). Pure read -- never mutates usage_events.
    """
    start, end = period_bounds(period, now)
    clauses = ["ts >= ?", "ts < ?"]
    params: list[Any] = [_iso(start), _iso(end)]
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    where = " AND ".join(clauses)
    row = conn.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM usage_events WHERE {where}",
        params,
    ).fetchone()
    return float(row["spend"] if row["spend"] is not None else 0.0)


def _iter_configured_budgets(
    config: Mapping[str, Any],
) -> list[tuple[str, str | None, dict[str, Any]]]:
    """Yield ``(scope, project_or_None, periods_dict)`` for every budget.

    ``scope`` is ``'global'`` or ``'project:<name>'``. ``periods_dict`` maps
    period name -> budget value (or ``None`` for no limit).
    """
    budgets = config.get("budgets") or {}
    result: list[tuple[str, str | None, dict[str, Any]]] = []

    glob = budgets.get("global") or {}
    if isinstance(glob, dict):
        result.append(("global", None, glob))

    projects = budgets.get("projects") or {}
    if isinstance(projects, dict):
        for name, periods in projects.items():
            if isinstance(periods, dict):
                result.append((f"project:{name}", name, periods))
    return result


# ===========================================================================
# Budget status / gauges (read-only; consumed by /api/alerts)
# ===========================================================================

def budget_status(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    now: _dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Return current-period status for every configured budget (gauges, §6.1).

    Each entry::

        {scope, project, period, period_key, budget, spend, fraction_used,
         level}

    ``level`` is ``'ok' | 'amber' | 'red'`` (amber at >=0.8, red at >=1.0) so the
    frontend gauges can colour without re-deriving the thresholds. ``budget`` may
    be ``None`` (no limit) in which case ``fraction_used`` is ``None``.

    Pure read over usage_events -- fires nothing.
    """
    if now is None:
        now = _dt.datetime.now(tz=_dt.timezone.utc)

    out: list[dict[str, Any]] = []
    for scope, project, periods in _iter_configured_budgets(config):
        for period in BUDGET_PERIODS:
            budget = periods.get(period)
            if budget is None:
                continue
            spend = _period_spend(conn, project, period, now)
            fraction = (spend / budget) if budget else None
            if fraction is None:
                level = "ok"
            elif fraction >= 1.0:
                level = "red"
            elif fraction >= 0.8:
                level = "amber"
            else:
                level = "ok"
            out.append(
                {
                    "scope": scope,
                    "project": project,
                    "period": period,
                    "period_key": period_key(period, now),
                    "budget": round(float(budget), 6),
                    "spend": round(spend, 6),
                    "fraction_used": (round(fraction, 6) if fraction is not None else None),
                    "level": level,
                }
            )
    return out


# ===========================================================================
# Budget alerts: fire-once persistence (§6.1)
# ===========================================================================

def _dedupe_key(period: str, now: _dt.datetime, fraction: float) -> str:
    """Build the ``alerts_log.period_key`` value that encodes the fired fraction.

    The schema's UNIQUE index is on ``(scope, period, period_key)`` ONLY, so the
    fraction MUST live inside one of those three columns for 0.8 and 1.0 to each
    fire exactly once within the same period. We append it to the period key as
    ``<period-key>@<fraction>`` -- e.g. ``2026-06@0.8`` and ``2026-06@1.0``.

    This makes the dedupe identity ``(scope, period, '<key>@<fraction>')`` unique
    per (scope, period, calendar-period, fraction): both fractions fire once and
    neither re-fires on a subsequent analyze run.
    """
    return f"{period_key(period, now)}@{_fraction_token(fraction)}"


def _fraction_token(fraction: float) -> str:
    """Render a fraction stably for the dedupe key (e.g. 0.8 -> '0.8')."""
    # Normalize to avoid float noise like 0.8000000001 producing distinct keys.
    return format(round(float(fraction), 4), "g")


def evaluate_budget_alerts(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    now: _dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Compute which budget-fraction crossings are currently TRUE (no writes).

    Returns one entry per crossing whose spend currently meets-or-exceeds a
    configured fraction of a budget::

        {scope, project, period, period_key (dedupe key), fraction, threshold,
         actual, calendar_key}

    ``threshold`` is the dollar value crossed (fraction * budget); ``actual`` is
    current spend. This is the pure detection step; :func:`analyze_and_fire`
    persists/de-dupes these via ``alerts_log``.
    """
    if now is None:
        now = _dt.datetime.now(tz=_dt.timezone.utc)

    fractions = sorted(float(f) for f in config.get("alert_fractions", [0.8, 1.0]))
    crossings: list[dict[str, Any]] = []

    for scope, project, periods in _iter_configured_budgets(config):
        for period in BUDGET_PERIODS:
            budget = periods.get(period)
            if budget is None or budget <= 0:
                continue
            spend = _period_spend(conn, project, period, now)
            for fraction in fractions:
                threshold = fraction * budget
                if spend >= threshold:
                    crossings.append(
                        {
                            "scope": scope,
                            "project": project,
                            "period": period,
                            "calendar_key": period_key(period, now),
                            "period_key": _dedupe_key(period, now, fraction),
                            "fraction": fraction,
                            "threshold": round(float(threshold), 6),
                            "actual": round(spend, 6),
                        }
                    )
    return crossings


def analyze_and_fire(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    *,
    now: _dt.datetime | None = None,
    deliver: bool = True,
) -> list[dict[str, Any]]:
    """Detect budget crossings and persist NEW ones to ``alerts_log`` (§6.1).

    This is the only analyzer step that writes. It is wired into the ingest path
    (so alerts persist after fresh data lands) and is fail-soft at the call site
    -- an analyzer error must never break ingest.

    For each crossing not already recorded, an ``alerts_log`` row is inserted via
    ``INSERT OR IGNORE`` on UNIQUE(scope, period, period_key). The fraction is
    encoded into ``period_key`` (see :func:`_dedupe_key`) so 0.8 and 1.0 each
    fire once and neither re-fires.

    Returns the list of crossings that were NEWLY fired this call (already-fired
    crossings are silently skipped). When ``deliver`` is True, newly-fired alerts
    trigger the optional desktop notification + webhook (best-effort, never
    raises).
    """
    if now is None:
        now = _dt.datetime.now(tz=_dt.timezone.utc)

    crossings = evaluate_budget_alerts(conn, config, now)
    newly_fired: list[dict[str, Any]] = []
    ts = _iso(now)

    for c in crossings:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO alerts_log
              (ts, scope, period, threshold, actual, period_key)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts, c["scope"], c["period"], c["threshold"], c["actual"], c["period_key"]),
        )
        if cur.rowcount > 0:
            newly_fired.append(c)
    conn.commit()

    if deliver and newly_fired:
        for alert in newly_fired:
            _deliver_alert(config, alert)

    return newly_fired


def recent_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Return recently fired alerts from ``alerts_log`` (read-only; §7 /api/alerts).

    Newest first. The fired fraction is recovered from the encoded ``period_key``
    so the frontend can show "80% crossed" vs "100% crossed".
    """
    rows = conn.execute(
        """
        SELECT id, ts, scope, period, threshold, actual, period_key
        FROM alerts_log
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        pk = r["period_key"]
        calendar_key, _, fraction_tok = pk.partition("@")
        out.append(
            {
                "id": int(r["id"]),
                "ts": r["ts"],
                "scope": r["scope"],
                "period": r["period"],
                "threshold": round(float(r["threshold"]), 6),
                "actual": round(float(r["actual"]), 6),
                "period_key": pk,
                "calendar_key": calendar_key,
                "fraction": (float(fraction_tok) if fraction_tok else None),
            }
        )
    return out


# ===========================================================================
# Optional alert delivery (best-effort; never raises) -- §6.1
# ===========================================================================

def _alert_message(alert: Mapping[str, Any]) -> tuple[str, str]:
    """Build a (title, body) pair for an alert notification."""
    pct = int(round(alert["fraction"] * 100))
    title = f"clauditor budget alert: {alert['scope']} {alert['period']}"
    body = (
        f"{alert['scope']} {alert['period']} spend ${alert['actual']:.2f} "
        f"crossed {pct}% of budget (${alert['threshold']:.2f})."
    )
    return title, body


def _deliver_alert(config: Mapping[str, Any], alert: Mapping[str, Any]) -> None:
    """Fire optional desktop notification + webhook for a newly-fired alert.

    Both deliveries are best-effort: a missing ``plyer`` or a failed webhook is
    swallowed so delivery never blocks or breaks ingest (SPEC.md §6.1, §11).
    """
    title, body = _alert_message(alert)
    if config.get("desktop_notifications"):
        _notify_desktop(title, body)
    webhook = config.get("alert_webhook_url")
    if webhook:
        _post_webhook(webhook, alert, title, body)


def _notify_desktop(title: str, body: str) -> None:
    """Lazy-import ``plyer`` and show a desktop notification; swallow all errors.

    ``plyer`` is an OPTIONAL extra -- it is never a hard dependency. If it is not
    installed (ImportError) or the platform backend fails, this degrades to a
    no-op (SPEC.md §6.1).
    """
    try:
        from plyer import notification  # type: ignore[import-not-found]

        notification.notify(title=title, message=body, app_name="clauditor")
    except Exception:  # noqa: BLE001 -- notification is strictly best-effort.
        pass


def _post_webhook(
    url: str,
    alert: Mapping[str, Any],
    title: str,
    body: str,
) -> None:
    """POST the alert JSON to ``url`` using stdlib urllib; swallow all errors.

    Uses ``urllib.request`` so no new dependency is added. Best-effort: a network
    failure, timeout, or bad URL never raises and never blocks ingest (§6.1, §11).
    """
    import json as _json
    import urllib.request as _urlreq

    payload = _json.dumps(
        {
            "title": title,
            "message": body,
            "scope": alert["scope"],
            "period": alert["period"],
            "fraction": alert["fraction"],
            "threshold": alert["threshold"],
            "actual": alert["actual"],
            "period_key": alert["period_key"],
        }
    ).encode("utf-8")
    try:
        req = _urlreq.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _urlreq.urlopen(req, timeout=5)  # noqa: S310 -- user-configured local URL.
    except Exception:  # noqa: BLE001 -- webhook delivery is strictly best-effort.
        pass


# ===========================================================================
# Savings suggestions (§6.2)
# ===========================================================================

def _lookback_window(
    config: Mapping[str, Any],
    now: _dt.datetime,
    from_: _dt.datetime | None = None,
    to: _dt.datetime | None = None,
) -> tuple[_dt.datetime, _dt.datetime, int]:
    """Resolve the [start, end] analysis window and its span in days.

    Honors explicit ``from_``/``to`` when given (so the /api/suggestions filters
    work), else defaults to the last ``lookback_days`` (default 30) from config.
    Returns ``(start, end, span_days)`` with ``span_days`` >= 1.
    """
    lookback_days = int(config.get("lookback_days", 30) or 30)
    end = to if to is not None else now
    start = from_ if from_ is not None else end - _dt.timedelta(days=lookback_days)
    span_seconds = max((end - start).total_seconds(), 0.0)
    span_days = max(span_seconds / 86400.0, 1.0)
    return start, end, span_days


def _is_opus(model: str | None) -> bool:
    if not model:
        return False
    lowered = model.lower()
    return any(marker in lowered for marker in OPUS_FAMILY_MARKERS)


def _fetch_window_rows(
    conn: sqlite3.Connection,
    start: _dt.datetime,
    end: _dt.datetime,
    project: str | None,
    model: str | None,
) -> list[sqlite3.Row]:
    """Read the usage rows in the window (parameterized, read-only)."""
    clauses = ["ts >= ?", "ts <= ?"]
    params: list[Any] = [_iso(start), _iso(end)]
    if project:
        clauses.append("project = ?")
        params.append(project)
    if model:
        clauses.append("model = ?")
        params.append(model)
    where = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT project, model, source, is_batch, cache_ttl,
               input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens,
               cost_usd, ts
        FROM usage_events
        WHERE {where}
        ORDER BY ts ASC
        """,
        params,
    ).fetchall()


def _monthly_scale(value: float, span_days: float) -> float:
    """Scale a window total to a per-30-day (monthly) figure (§6.2)."""
    return value * (30.0 / span_days)


def rule_model_downgrade(
    rows: list[sqlite3.Row],
    pricing: Mapping[str, Any],
    span_days: float,
) -> list[dict[str, Any]]:
    """Rule 1 -- Opus calls that look like cheaper-model work (§6.2).

    For each project, considers its Opus-family events. Qualifies when:
      * call count >= MIN_CALLS, AND
      * median output_tokens < MEDIAN_OUTPUT_TOKENS_MAX, AND
      * input_tokens coefficient-of-variation <= MAX_INPUT_CV (repetitive).

    Savings = (sum of Opus cost over window) - (sum of cost recomputed at the
    cheaper DOWNGRADE_TARGET_MODEL rates over the same events), scaled to monthly.
    Every figure is reproducible from the matched rows.
    """
    target = DOWNGRADE_TARGET_MODEL
    if target not in pricing["models"]:
        return []

    # Group Opus-family rows per project.
    by_project: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if _is_opus(r["model"]):
            by_project.setdefault(r["project"] or "", []).append(r)

    suggestions: list[dict[str, Any]] = []
    for project, prows in by_project.items():
        calls = len(prows)
        if calls < MIN_CALLS:
            continue

        outputs = [int(r["output_tokens"] or 0) for r in prows]
        median_out = statistics.median(outputs)
        if median_out >= MEDIAN_OUTPUT_TOKENS_MAX:
            continue

        inputs = [int(r["input_tokens"] or 0) for r in prows]
        mean_in = statistics.fmean(inputs) if inputs else 0.0
        if mean_in <= 0:
            continue
        stdev_in = statistics.pstdev(inputs) if len(inputs) > 1 else 0.0
        cv = stdev_in / mean_in
        if cv > MAX_INPUT_CV:
            continue

        current_cost = sum(float(r["cost_usd"] or 0.0) for r in prows)
        downgraded_cost = 0.0
        for r in prows:
            ev = {
                "model": target,
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                "cache_ttl": r["cache_ttl"],
                "is_batch": int(r["is_batch"] or 0),
            }
            downgraded_cost += compute_cost(ev, pricing)

        delta = current_cost - downgraded_cost
        if delta <= 0:
            continue

        monthly = _monthly_scale(delta, span_days)
        pct = (delta / current_cost * 100.0) if current_cost > 0 else 0.0
        suggestions.append(
            {
                "title": f"Downgrade Opus work in '{project}' to {target}",
                "detail": (
                    f"{calls} Opus calls in '{project}' look like cheaper work "
                    f"(median output {int(median_out)} tokens, repetitive inputs). "
                    f"Re-running on {target} would have cost "
                    f"${downgraded_cost:.2f} instead of ${current_cost:.2f} over "
                    f"the window -- save ~${monthly:.2f}/mo ({pct:.0f}%)."
                ),
                "estimated_monthly_savings_usd": round(monthly, 2),
                "confidence": "high",
            }
        )
    return suggestions


def rule_missing_prompt_cache(
    rows: list[sqlite3.Row],
    pricing: Mapping[str, Any],
    span_days: float,
) -> list[dict[str, Any]]:
    """Rule 2 -- recurring large uncached inputs that should be cached (§6.2).

    Groups events by (project, model, input_tokens bucketed to nearest 500),
    considering only events with input_tokens >= CACHE_INPUT_TOKENS_MIN and
    cache_read_tokens == 0. A group qualifies when it recurs >= CACHE_MIN_REPEATS.

    Savings = repeated input tokens * input_rate * 0.90  (cache-read discount)
              minus one one-time cache write (bucket tokens * input_rate * 1.25).
    "Repeated" = all calls after the first (the first pays the write). Scaled to
    monthly. confidence='medium' (inferring from token sizes alone, §6.2).
    """
    models = pricing["models"]
    groups: dict[tuple[str, str, int], list[sqlite3.Row]] = {}
    for r in rows:
        in_toks = int(r["input_tokens"] or 0)
        if in_toks < CACHE_INPUT_TOKENS_MIN:
            continue
        if int(r["cache_read_tokens"] or 0) != 0:
            continue
        bucket = int(round(in_toks / CACHE_BUCKET_SIZE)) * CACHE_BUCKET_SIZE
        key = (r["project"] or "", r["model"] or "", bucket)
        groups.setdefault(key, []).append(r)

    suggestions: list[dict[str, Any]] = []
    for (project, model, bucket), grows in groups.items():
        repeats = len(grows)
        if repeats < CACHE_MIN_REPEATS:
            continue

        rates = models.get(model)
        if rates is None:
            rates = models[pricing["fallback_model"]]
        in_rate = rates["input"]

        # Tokens that would be served from cache (every call after the first).
        repeated_input_tokens = sum(int(r["input_tokens"] or 0) for r in grows[1:])
        gross_savings = repeated_input_tokens * in_rate * CACHE_READ_SAVINGS_FRACTION / 1_000_000.0
        # One-time write cost for the first occurrence (5-minute cache).
        write_cost = bucket * in_rate * CACHE_WRITE_5M_MULTIPLIER / 1_000_000.0
        net = gross_savings - write_cost
        if net <= 0:
            continue

        monthly = _monthly_scale(net, span_days)
        suggestions.append(
            {
                "title": f"Enable prompt caching for '{project}' ({model})",
                "detail": (
                    f"'{project}' sends ~{bucket} input tokens uncached on "
                    f"{repeats} calls ({model}). Enabling prompt caching would "
                    f"reuse ~{repeated_input_tokens} repeated input tokens at the "
                    f"cache-read rate -- save ~${monthly:.2f}/mo."
                ),
                "estimated_monthly_savings_usd": round(monthly, 2),
                "confidence": "medium",
            }
        )
    return suggestions


def rule_batch_candidates(
    rows: list[sqlite3.Row],
    pricing: Mapping[str, Any],
    span_days: float,
) -> list[dict[str, Any]]:
    """Rule 3 -- synchronous API bursts that could use the Batch API (§6.2).

    Considers only source='api', is_batch=0 events. Per project, finds the
    densest BATCH_WINDOW_MINUTES window; if it holds >= BATCH_MIN_CALLS calls the
    project qualifies. Savings = cost of ALL qualifying (non-batch api) events in
    the project * 0.50, scaled to monthly.
    """
    by_project: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r["source"] != "api":
            continue
        if int(r["is_batch"] or 0) != 0:
            continue
        by_project.setdefault(r["project"] or "", []).append(r)

    suggestions: list[dict[str, Any]] = []
    window = _dt.timedelta(minutes=BATCH_WINDOW_MINUTES)

    for project, prows in by_project.items():
        # Parse timestamps; rows came back ORDER BY ts ASC.
        times: list[_dt.datetime] = []
        for r in prows:
            try:
                times.append(_parse_ts(r["ts"]))
            except (ValueError, TypeError):
                continue
        times.sort()

        # Sliding window: max number of calls within any BATCH_WINDOW_MINUTES span.
        max_in_window = 0
        left = 0
        for right in range(len(times)):
            while times[right] - times[left] > window:
                left += 1
            max_in_window = max(max_in_window, right - left + 1)

        if max_in_window < BATCH_MIN_CALLS:
            continue

        total_cost = sum(float(r["cost_usd"] or 0.0) for r in prows)
        savings = total_cost * BATCH_SAVINGS_FRACTION
        if savings <= 0:
            continue

        monthly = _monthly_scale(savings, span_days)
        suggestions.append(
            {
                "title": f"Use the Batch API for '{project}'",
                "detail": (
                    f"{len(prows)} synchronous API calls in '{project}' "
                    f"(up to {max_in_window} within {BATCH_WINDOW_MINUTES} min) "
                    f"could tolerate latency. The Batch API (50% off) would "
                    f"save ~${monthly:.2f}/mo."
                ),
                "estimated_monthly_savings_usd": round(monthly, 2),
                "confidence": "medium",
            }
        )
    return suggestions


def _parse_ts(value: str) -> _dt.datetime:
    """Parse an ISO-8601 ``ts`` string into a UTC datetime."""
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = _dt.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def savings_suggestions(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    pricing: Mapping[str, Any],
    *,
    now: _dt.datetime | None = None,
    from_: _dt.datetime | None = None,
    to: _dt.datetime | None = None,
    project: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Run all three savings rules over the lookback window (read-only; §6.2).

    Returns a flat list of ``{title, detail, estimated_monthly_savings_usd,
    confidence}`` dicts, sorted by estimated savings descending. A clean dataset
    (no qualifying pattern) yields an empty list (false-positive resistance).
    """
    if now is None:
        now = _dt.datetime.now(tz=_dt.timezone.utc)
    start, end, span_days = _lookback_window(config, now, from_, to)
    rows = _fetch_window_rows(conn, start, end, project, model)

    suggestions: list[dict[str, Any]] = []
    suggestions += rule_model_downgrade(rows, pricing, span_days)
    suggestions += rule_missing_prompt_cache(rows, pricing, span_days)
    suggestions += rule_batch_candidates(rows, pricing, span_days)

    suggestions.sort(
        key=lambda s: s["estimated_monthly_savings_usd"], reverse=True
    )
    return suggestions
