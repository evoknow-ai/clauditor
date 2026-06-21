"""JSON API routes for the clauditor dashboard (SPEC.md §7).

Implemented endpoints (SPEC.md §7):

* ``GET /api/summary``      -- totals + cache-efficiency % for the range.
* ``GET /api/timeseries``   -- spend + tokens per time bucket, split by model.
* ``GET /api/breakdown``    -- grouped spend + tokens (by project|model|source).
* ``GET /api/health``       -- liveness + db/pricing metadata.
* ``GET /api/suggestions``  -- analyzer savings suggestions (§6.2) (Phase 8).
* ``GET /api/alerts``       -- recent fired alerts + current budget status (§6.1)
  (Phase 8).

The suggestions/alerts endpoints stay read-only: they run the analyzer's pure
read functions over ``usage_events`` and never fire or persist an alert.

The remaining §7 endpoints (pricing, ingest) are not built in this file.

Safety / correctness (SPEC.md §7, §11):

* All reads go through SQLite **parameterized** queries -- no user input is ever
  interpolated into SQL text.
* The database is opened **read-only** (immutable URI) for these endpoints.
* Bad input (unparseable date, unknown ``by`` / ``granularity``) yields a clean
  HTTP 4xx JSON error -- never a 500/traceback.
* The default range is the last 30 days, measured from a single "now" captured
  per request from the system clock (UTC).
* An empty DB / no matching rows returns well-formed zeros, not an error.
* A **missing or unopenable** DB file also returns well-formed zeros for the
  read endpoints (so a fresh dashboard renders empty rather than 500-ing), and a
  ``degraded`` status on ``/api/health`` (CARRIED-FORWARD ITEM 1).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

# Filters shared by summary / timeseries / breakdown (SPEC.md §7).
# ``by`` values allowed on /api/breakdown -> column name in usage_events.
_BREAKDOWN_COLUMNS = {
    "project": "project",
    "model": "model",
    "source": "source",
}

# Granularities supported by /api/timeseries. ``day`` is the required default;
# the others are a documented plus. Each maps to an ISO-8601 prefix length used
# to bucket the ``ts`` string (which is ISO 8601 UTC, e.g. "2026-06-20T10:00:00").
_GRANULARITIES = {"hour", "day", "week", "month"}

_DEFAULT_RANGE_DAYS = 30

router = APIRouter()


# --- Errors -----------------------------------------------------------------

class _BadRequest(Exception):
    """Signals invalid client input -> mapped to an HTTP 400 JSON body."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- Read-only DB access ----------------------------------------------------

def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the DB read-only so the API can never mutate usage data (§11).

    Uses SQLite's ``mode=ro`` URI. Rows come back as ``sqlite3.Row`` for
    name-based access.

    Raises :class:`sqlite3.OperationalError` if the file does not exist (``mode=ro``
    refuses to create it) -- callers harden against that via
    :func:`_try_connect_readonly`.
    """
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _try_connect_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    """Open the DB read-only, or return ``None`` if it is missing/unopenable.

    A fresh machine may have no ``data/clauditor.db`` yet (ingest never ran). The
    read endpoints must still render -- as well-formed zeros -- rather than 500
    on the resulting ``OperationalError`` (CARRIED-FORWARD ITEM 1; SPEC.md §7's
    "empty DB returns well-formed zeros" intent extended to a missing file).
    """
    try:
        return _connect_readonly(db_path)
    except sqlite3.Error:
        return None


# --- Date / filter parsing --------------------------------------------------

def _parse_iso_datetime(value: str, field: str) -> _dt.datetime:
    """Parse an ISO-8601 date or datetime into a timezone-aware UTC datetime.

    Accepts both bare dates (``2026-06-20``) and full datetimes
    (``2026-06-20T10:00:00+00:00`` / trailing ``Z``). Naive values are assumed
    UTC. Raises :class:`_BadRequest` on anything unparseable (SPEC.md §7).
    """
    text = value.strip()
    if not text:
        raise _BadRequest(f"'{field}' must be a non-empty ISO-8601 date/datetime")

    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    parsed: _dt.datetime | None = None
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        # Fall back to a date-only parse.
        try:
            d = _dt.date.fromisoformat(candidate)
            parsed = _dt.datetime(d.year, d.month, d.day)
        except ValueError as exc:
            raise _BadRequest(
                f"'{field}' is not a valid ISO-8601 date/datetime: {value!r}"
            ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    """Render a UTC datetime as an ISO-8601 string matching the ``ts`` column."""
    return dt.astimezone(_dt.timezone.utc).isoformat()


def _resolve_range(
    from_: str | None,
    to: str | None,
    now: _dt.datetime,
) -> tuple[_dt.datetime, _dt.datetime]:
    """Resolve the [from, to) window, defaulting to the last 30 days (§7).

    ``now`` is captured once per request from the system clock so the default
    window is deterministic within a request. Raises :class:`_BadRequest` if the
    resolved range is inverted (from > to).
    """
    end = _parse_iso_datetime(to, "to") if to else now
    if from_:
        start = _parse_iso_datetime(from_, "from")
    else:
        start = end - _dt.timedelta(days=_DEFAULT_RANGE_DAYS)

    if start > end:
        raise _BadRequest("'from' must be on or before 'to'")
    return start, end


def _filter_clause(
    start: _dt.datetime,
    end: _dt.datetime,
    project: str | None,
    model: str | None,
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + bound params for the four filters.

    The time window is half-open ``[start, end]`` inclusive on both ends so a
    caller passing an explicit ``to`` instant still sees a row exactly at ``to``.
    All values are bound parameters -- never string-interpolated (SPEC.md §7/§11).
    """
    clauses = ["ts >= ?", "ts <= ?"]
    params: list[Any] = [_iso(start), _iso(end)]

    if project is not None and project != "":
        clauses.append("project = ?")
        params.append(project)
    if model is not None and model != "":
        clauses.append("model = ?")
        params.append(model)

    return " AND ".join(clauses), params


def _bucket_expr(granularity: str) -> str:
    """Return an SQL expression bucketing the ``ts`` column for a granularity.

    ``ts`` is an ISO-8601 string, so bucketing is a deterministic substring/date
    operation. This expression contains **no user input** (granularity is
    validated against a fixed allow-list before this is called).
    """
    if granularity == "hour":
        return "substr(ts, 1, 13)"          # YYYY-MM-DDTHH
    if granularity == "day":
        return "substr(ts, 1, 10)"          # YYYY-MM-DD
    if granularity == "month":
        return "substr(ts, 1, 7)"           # YYYY-MM
    if granularity == "week":
        # ISO week key (YYYY-Www) computed by SQLite from the date portion.
        return "strftime('%Y-W%W', substr(ts, 1, 10))"
    # Unreachable: granularity is validated by the caller.
    raise _BadRequest(f"unsupported granularity: {granularity!r}")


# --- Endpoints --------------------------------------------------------------

@router.get("/api/summary")
def get_summary(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
) -> Any:
    """Totals for the range: spend, tokens, call count, cache-efficiency %.

    Cache efficiency = ``cache_read_tokens / (input_tokens + cache_read_tokens)``
    (SPEC.md §6.3), guarded against divide-by-zero (returns 0.0 when there is no
    input/cache-read traffic).
    """
    try:
        now = _now()
        start, end = _resolve_range(from_, to, now)
        where, params = _filter_clause(start, end, project, model)
    except _BadRequest as exc:
        return _error(exc)

    db_path = request.app.state.db_path
    conn = _try_connect_readonly(db_path)
    if conn is None:
        # Missing/unopenable DB -> same shape as the empty-DB case (§7).
        return _empty_summary(start, end, project, model)
    try:
        row = conn.execute(
            f"""
            SELECT
              COUNT(*)                              AS call_count,
              COALESCE(SUM(cost_usd), 0)            AS total_spend_usd,
              COALESCE(SUM(input_tokens), 0)        AS input_tokens,
              COALESCE(SUM(output_tokens), 0)       AS output_tokens,
              COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
              COALESCE(SUM(cache_read_tokens), 0)   AS cache_read_tokens
            FROM usage_events
            WHERE {where}
            """,
            params,
        ).fetchone()
    finally:
        conn.close()

    input_tokens = int(row["input_tokens"])
    output_tokens = int(row["output_tokens"])
    cache_creation = int(row["cache_creation_tokens"])
    cache_read = int(row["cache_read_tokens"])
    total_tokens = input_tokens + output_tokens + cache_creation + cache_read

    denom = input_tokens + cache_read
    cache_efficiency = (cache_read / denom) if denom > 0 else 0.0

    return {
        "range": {"from": _iso(start), "to": _iso(end)},
        "filters": {"project": project or None, "model": model or None},
        "call_count": int(row["call_count"]),
        "total_spend_usd": round(float(row["total_spend_usd"]), 6),
        "total_tokens": total_tokens,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_creation": cache_creation,
            "cache_read": cache_read,
        },
        "cache_efficiency": round(cache_efficiency, 6),
    }


@router.get("/api/timeseries")
def get_timeseries(
    request: Request,
    granularity: str = Query(default="day"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
) -> Any:
    """Spend + tokens per time bucket, split by model (SPEC.md §7).

    ``granularity`` defaults to ``day`` (the required resolution); ``hour`` /
    ``week`` / ``month`` are also accepted. An unknown granularity is a 400.
    """
    try:
        if granularity not in _GRANULARITIES:
            raise _BadRequest(
                f"'granularity' must be one of "
                f"{sorted(_GRANULARITIES)}, got {granularity!r}"
            )
        now = _now()
        start, end = _resolve_range(from_, to, now)
        where, params = _filter_clause(start, end, project, model)
        bucket = _bucket_expr(granularity)
    except _BadRequest as exc:
        return _error(exc)

    db_path = request.app.state.db_path
    conn = _try_connect_readonly(db_path)
    if conn is None:
        # Missing/unopenable DB -> empty series, well-formed (§7).
        return {
            "range": {"from": _iso(start), "to": _iso(end)},
            "granularity": granularity,
            "filters": {"project": project or None, "model": model or None},
            "series": [],
        }
    try:
        rows = conn.execute(
            f"""
            SELECT
              {bucket}                        AS bucket,
              model                           AS model,
              COUNT(*)                        AS call_count,
              COALESCE(SUM(cost_usd), 0)      AS spend_usd,
              COALESCE(SUM(input_tokens
                + output_tokens
                + cache_creation_tokens
                + cache_read_tokens), 0)      AS tokens
            FROM usage_events
            WHERE {where}
            GROUP BY bucket, model
            ORDER BY bucket ASC, model ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    series = [
        {
            "bucket": r["bucket"],
            "model": r["model"],
            "call_count": int(r["call_count"]),
            "spend_usd": round(float(r["spend_usd"]), 6),
            "tokens": int(r["tokens"]),
        }
        for r in rows
    ]

    return {
        "range": {"from": _iso(start), "to": _iso(end)},
        "granularity": granularity,
        "filters": {"project": project or None, "model": model or None},
        "series": series,
    }


@router.get("/api/breakdown")
def get_breakdown(
    request: Request,
    by: str = Query(default="project"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
) -> Any:
    """Grouped spend + tokens, grouped by project | model | source (SPEC.md §7).

    ``by`` must be one of the three allowed dimensions; anything else is a 400.
    The grouping column is resolved through a fixed allow-list, so the value
    placed into SQL is never raw user input.
    """
    try:
        column = _BREAKDOWN_COLUMNS.get(by)
        if column is None:
            raise _BadRequest(
                f"'by' must be one of "
                f"{sorted(_BREAKDOWN_COLUMNS)}, got {by!r}"
            )
        now = _now()
        start, end = _resolve_range(from_, to, now)
        where, params = _filter_clause(start, end, project, model)
    except _BadRequest as exc:
        return _error(exc)

    db_path = request.app.state.db_path
    conn = _try_connect_readonly(db_path)
    if conn is None:
        # Missing/unopenable DB -> empty groups, well-formed (§7).
        return {
            "range": {"from": _iso(start), "to": _iso(end)},
            "by": by,
            "filters": {"project": project or None, "model": model or None},
            "groups": [],
        }
    try:
        rows = conn.execute(
            f"""
            SELECT
              {column}                        AS key,
              COUNT(*)                        AS call_count,
              COALESCE(SUM(cost_usd), 0)      AS spend_usd,
              COALESCE(SUM(input_tokens
                + output_tokens
                + cache_creation_tokens
                + cache_read_tokens), 0)      AS tokens
            FROM usage_events
            WHERE {where}
            GROUP BY {column}
            ORDER BY spend_usd DESC, key ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    groups = [
        {
            "key": r["key"],
            "call_count": int(r["call_count"]),
            "spend_usd": round(float(r["spend_usd"]), 6),
            "tokens": int(r["tokens"]),
        }
        for r in rows
    ]

    return {
        "range": {"from": _iso(start), "to": _iso(end)},
        "by": by,
        "filters": {"project": project or None, "model": model or None},
        "groups": groups,
    }


@router.get("/api/suggestions")
def get_suggestions(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
    project: str | None = Query(default=None),
    model: str | None = Query(default=None),
) -> Any:
    """Analyzer savings suggestions over the lookback window (SPEC.md §6.2, §7).

    Honors the shared ``from``/``to``/``project``/``model`` filters; when no
    range is given the analyzer uses ``config.lookback_days`` (default 30).
    Read-only. A missing/unopenable DB returns a well-formed empty list (never a
    500), and bad input yields a clean 4xx.
    """
    try:
        now = _now()
        start, end = _resolve_range(from_, to, now)
    except _BadRequest as exc:
        return _error(exc)

    config = getattr(request.app.state, "config", None)
    pricing = getattr(request.app.state, "pricing", None)
    range_body = {
        "range": {"from": _iso(start), "to": _iso(end)},
        "filters": {"project": project or None, "model": model or None},
    }
    if config is None or pricing is None:
        return {**range_body, "suggestions": []}

    db_path = request.app.state.db_path
    conn = _try_connect_readonly(db_path)
    if conn is None:
        # Missing/unopenable DB -> empty suggestions, well-formed (§7).
        return {**range_body, "suggestions": []}

    try:
        from core.analyzer import savings_suggestions

        # Honor explicit range; otherwise let the analyzer apply lookback_days.
        suggestions = savings_suggestions(
            conn,
            config,
            pricing,
            now=now,
            from_=start if from_ else None,
            to=end if to else None,
            project=project or None,
            model=model or None,
        )
    except Exception:  # noqa: BLE001 -- read endpoint must never 500 (§7).
        suggestions = []
    finally:
        conn.close()

    return {**range_body, "suggestions": suggestions}


@router.get("/api/alerts")
def get_alerts(request: Request) -> Any:
    """Recent fired alerts + current budget status/gauges (SPEC.md §6.1, §7).

    This endpoint is a PURE READ: it returns the already-fired rows from
    ``alerts_log`` plus the current-period budget gauges. It NEVER fires or
    persists alerts (that happens in the analyze step wired into ingest), so a
    desktop notification / webhook is never a side effect of a dashboard refresh.

    Read-only; a missing/unopenable DB returns well-formed empties, not a 500.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return {"alerts": [], "budgets": []}

    db_path = request.app.state.db_path
    conn = _try_connect_readonly(db_path)
    if conn is None:
        # Missing/unopenable DB -> well-formed empty alerts + gauges (§7).
        return {"alerts": [], "budgets": []}

    try:
        from core.analyzer import budget_status, recent_alerts

        alerts = recent_alerts(conn)
        budgets = budget_status(conn, config, _now())
    except Exception:  # noqa: BLE001 -- read endpoint must never 500 (§7).
        alerts, budgets = [], []
    finally:
        conn.close()

    return {"alerts": alerts, "budgets": budgets}


@router.get("/api/health")
def get_health(request: Request) -> Any:
    """Liveness + metadata: status, db_path, event_count, pricing_updated (§7).

    Reads the total event count read-only; if the DB is unreadable it still
    returns a well-formed body with a degraded status rather than erroring.
    """
    db_path = request.app.state.db_path
    pricing_updated = request.app.state.pricing_updated

    status = "ok"
    count = 0
    try:
        conn = _connect_readonly(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM usage_events"
            ).fetchone()
            count = int(row["n"])
        finally:
            conn.close()
    except sqlite3.Error:
        status = "degraded"

    return {
        "status": status,
        "db_path": str(db_path),
        "event_count": count,
        "pricing_updated": pricing_updated,
    }


# --- helpers ----------------------------------------------------------------

def _now() -> _dt.datetime:
    """Capture a single 'now' (UTC) per request for deterministic defaults."""
    return _dt.datetime.now(tz=_dt.timezone.utc)


def _empty_summary(
    start: _dt.datetime,
    end: _dt.datetime,
    project: str | None,
    model: str | None,
) -> dict[str, Any]:
    """Well-formed all-zero summary body (missing DB / no rows; §7)."""
    return {
        "range": {"from": _iso(start), "to": _iso(end)},
        "filters": {"project": project or None, "model": model or None},
        "call_count": 0,
        "total_spend_usd": 0.0,
        "total_tokens": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_creation": 0,
            "cache_read": 0,
        },
        "cache_efficiency": 0.0,
    }


def _error(exc: _BadRequest) -> JSONResponse:
    """Render a clean 400 JSON body (never a 500/traceback) for bad input."""
    return JSONResponse(status_code=400, content={"error": exc.message})
