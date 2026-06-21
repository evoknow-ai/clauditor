"""Admin API collector (SPEC.md §5.3) -- OPTIONAL, flag-gated, built LAST.

For org-wide rollups without instrumenting code, this collector can poll
Anthropic's Admin Usage & Cost endpoints on demand and record the returned
usage as ``source='admin_api'`` rows in ``usage_events``.

THE #1 REQUIREMENT IS THAT THIS COLLECTOR IS COMPLETELY INERT BY DEFAULT.

Gating order (SPEC.md §5.3, §11 -- non-negotiable):

1. If ``config.admin_api.enabled`` is falsey (the DEFAULT) -> IMMEDIATE no-op.
   The key is NOT read, no HTTP library is imported, no network call is made,
   nothing is raised. Completely inert.
2. If enabled but NO admin key is resolvable (neither an inline
   ``admin_api.key`` nor the env var named by ``admin_api.key_env``, default
   ``ANTHROPIC_ADMIN_KEY``) -> SILENT no-op (zero result, no crash, no network).
3. Only when enabled AND a key is present is the actual Admin API poll attempted.

The poll itself is defensive and fail-soft (SPEC.md §11): any network error,
non-200 status, timeout, or parse failure is caught and turned into a
no-op-with-warning -- it never aborts ingest and never raises out of the
collector. The HTTP client (stdlib ``urllib.request``, so no new dependency) is
lazily imported INSIDE the enabled+key branch, so importing this module (and the
core tool) never requires it, and the ``anthropic`` SDK is never imported here.

Schema tolerance: the exact Admin Usage/Cost response shape is not verifiable in
this environment, so the payload is parsed by KEY with every field optional --
bad records are skipped and counted, exactly like the Claude Code parser. The
assumptions about the response shape are documented inline in
:func:`_iter_usage_records`.

Token counts, when present, are taken VERBATIM and priced with the shared
:func:`core.pricing.compute_cost` engine -- never re-tokenized (SPEC.md §11). The
Admin Cost API can also return an aggregated dollar amount directly; when that is
all that is available we store it as ``cost_usd`` and mark the row in
``raw_meta`` rather than fabricating token counts.

The public entry point is :func:`ingest_admin_api`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
from typing import Any, Iterator, Mapping

from core.db import insert_usage_event
from core.pricing import compute_cost, is_known_model

SOURCE = "admin_api"

# Default Admin Usage & Cost base. Overridable via ``admin_api.base_url`` in
# config (documented + defaulted to null in DEFAULT_CONFIG) so a user can point
# at a gateway/proxy without code changes. The exact endpoint paths below are an
# assumption (see _build_request); the collector is schema- and shape-tolerant.
DEFAULT_BASE_URL = "https://api.anthropic.com"

# Outbound poll timeout (seconds). Kept small so a hung endpoint can never stall
# an ingest run for long -- on timeout we fail soft to a no-op-with-warning.
REQUEST_TIMEOUT = 15


# --- Result helper ----------------------------------------------------------

def _result(
    rows_added: int = 0,
    records_skipped: int = 0,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Build the collector's return dict.

    Matches the other collectors' shape (a ``rows_added`` int the CLI reports per
    source). ``records_skipped`` mirrors ``lines_skipped`` from the Claude Code
    collector so the CLI's ``_detail_suffix`` could surface it; ``note`` carries
    an optional informational message (e.g. why it no-opped) for callers/tests.
    """
    out: dict[str, Any] = {"rows_added": rows_added, "records_skipped": records_skipped}
    if note is not None:
        out["note"] = note
    return out


# --- Key resolution ---------------------------------------------------------

def resolve_admin_key(admin_cfg: Mapping[str, Any]) -> str | None:
    """Resolve the admin key, or return ``None`` if none is available.

    Precedence (SPEC.md §5.3): an inline ``admin_api.key`` if present and
    non-empty, otherwise the environment variable named by ``admin_api.key_env``
    (default ``ANTHROPIC_ADMIN_KEY``) read via ``os.environ``. An empty/whitespace
    value yields ``None`` so an enabled-but-keyless config is a clean no-op rather
    than an attempted poll with an empty credential.
    """
    inline = admin_cfg.get("key")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()

    key_env = admin_cfg.get("key_env") or "ANTHROPIC_ADMIN_KEY"
    if not isinstance(key_env, str) or not key_env:
        return None

    env_value = os.environ.get(key_env)
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()

    return None


# --- Field helpers (by key, never position) ---------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping; anything else returns ``default``."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return default


def _first(obj: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among ``keys`` in ``obj``."""
    for key in keys:
        value = obj.get(key)
        if value is not None:
            return value
    return default


def _int(value: Any) -> int:
    """Coerce a token field to a non-negative int; missing/invalid -> 0."""
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def _float_or_none(value: Any) -> float | None:
    """Coerce a cost field to a float, or ``None`` if missing/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_ts(value: Any) -> str:
    """Return an ISO-8601 UTC timestamp string from a flexible input.

    Accepts an ISO string verbatim, an epoch number, or falls back to "now" when
    the payload provides nothing usable. The Admin API buckets usage by time, so
    most records carry a date/window start.
    """
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _dt.datetime.fromtimestamp(value, tz=_dt.timezone.utc).isoformat()
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


# --- event_uid --------------------------------------------------------------

def make_event_uid(
    bucket_key: str,
    model: str | None,
    project: str | None,
) -> str:
    """Build a stable dedupe hash so re-polling the same window is idempotent.

    Hashes ``source + bucket_key + model + project`` where ``bucket_key`` is the
    record's time-bucket identifier (e.g. its start timestamp). The Admin API is
    deterministic per (window, model, workspace), so the same poll yields the same
    uid and ``INSERT OR IGNORE`` (UNIQUE event_uid) prevents double-counting on a
    re-poll (SPEC.md §5.1, §11 dedupe).
    """
    parts = [SOURCE, bucket_key, model or "", project or ""]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# --- Payload parsing (schema-tolerant; assumptions documented) --------------

def _iter_usage_records(payload: Any) -> Iterator[Mapping[str, Any]]:
    """Yield flat usage records from a (loosely-assumed) Admin API payload.

    ASSUMPTIONS about the response shape (documented because the live schema is
    not verifiable here; everything is looked up by key and tolerated if absent):

    * The top level is an object that may hold the records under ``data`` or
      ``results`` (or be a bare list).
    * Each entry may be a flat usage record, OR a time-bucket object that nests
      its per-model breakdown under ``results``/``items``/``usage`` -- in which
      case the bucket's ``starting_at``/``start_time`` timestamp is propagated
      onto each nested record so the row keeps a sensible ``ts`` and ``bucket``.

    Any entry that is not a mapping (or yields no usable record) is simply not
    yielded; the caller counts skips. This never raises.
    """
    if isinstance(payload, Mapping):
        top = payload.get("data")
        if top is None:
            top = payload.get("results")
        if top is None:
            # A single bare record object.
            top = [payload]
    elif isinstance(payload, list):
        top = payload
    else:
        return

    if not isinstance(top, list):
        return

    for entry in top:
        if not isinstance(entry, Mapping):
            continue

        # Time-bucket envelope: a window with a nested per-model breakdown.
        nested = None
        for nest_key in ("results", "items", "usage", "breakdown"):
            candidate = entry.get(nest_key)
            if isinstance(candidate, list):
                nested = candidate
                break

        bucket_ts = _first(
            entry, "starting_at", "start_time", "start", "date", "timestamp", "ts"
        )

        if nested is not None:
            for sub in nested:
                if not isinstance(sub, Mapping):
                    continue
                # Propagate the bucket timestamp if the nested record lacks one.
                if bucket_ts is not None and not any(
                    sub.get(k) is not None
                    for k in ("starting_at", "start_time", "start", "date", "timestamp", "ts")
                ):
                    merged = dict(sub)
                    merged["starting_at"] = bucket_ts
                    yield merged
                else:
                    yield sub
        else:
            yield entry


def _record_to_event(
    record: Mapping[str, Any],
    pricing: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Map one Admin API usage record to a ``usage_events`` row, or ``None``.

    Returns ``None`` (caller counts a skip) when the record carries neither token
    counts nor a cost amount -- there is nothing to record. Never raises on bad
    input; every field is looked up by key and coerced defensively.

    Pricing (SPEC.md §11):
      * When token counts are present, cost is computed with the shared
        :func:`core.pricing.compute_cost` engine (same as every other collector).
      * When ONLY an aggregated cost is provided (Admin Cost API), that dollar
        amount is stored verbatim and the row is flagged in ``raw_meta`` --
        token counts are NEVER fabricated/re-tokenized.
    """
    # Model: by key, tolerate absence. Lower-resolution rollups may omit it.
    model = _first(record, "model", "model_id", "model_name")
    if not isinstance(model, str) or not model:
        model = None

    # Project / workspace label (SPEC.md §3: project = API-key label).
    project = _first(
        record,
        "workspace_id",
        "workspace",
        "project",
        "api_key_id",
        "api_key",
        "key_label",
        "service_tier",
    )
    if project is not None:
        project = str(project)

    ts = _normalize_ts(
        _first(record, "starting_at", "start_time", "start", "date", "timestamp", "ts")
    )

    input_tokens = _int(
        _first(record, "input_tokens", "uncached_input_tokens", "prompt_tokens")
    )
    output_tokens = _int(_first(record, "output_tokens", "completion_tokens"))
    cache_creation_tokens = _int(
        _first(
            record,
            "cache_creation_input_tokens",
            "cache_creation_tokens",
            "cache_write_tokens",
        )
    )
    cache_read_tokens = _int(
        _first(record, "cache_read_input_tokens", "cache_read_tokens")
    )

    has_tokens = (
        input_tokens
        or output_tokens
        or cache_creation_tokens
        or cache_read_tokens
    )

    reported_cost = _float_or_none(
        _first(record, "cost_usd", "cost", "amount", "total_cost")
    )

    # Nothing to record: no tokens and no cost.
    if not has_tokens and reported_cost is None:
        return None

    # is_batch: the Admin API may expose a service tier / batch flag.
    raw_batch = _first(record, "is_batch", "batch")
    if raw_batch is None:
        tier = _first(record, "service_tier", "tier")
        is_batch = 1 if isinstance(tier, str) and "batch" in tier.lower() else 0
    else:
        is_batch = 1 if raw_batch else 0

    bucket_key = ts
    unknown_model = not is_known_model(model, pricing)
    effective_model = model if model else pricing["fallback_model"]

    meta: dict[str, Any] = {"collector": SOURCE}
    if unknown_model:
        meta["unknown_model"] = True
        meta["reported_model"] = model

    event: dict[str, Any] = {
        "event_uid": make_event_uid(bucket_key, model, project),
        "ts": ts,
        "source": SOURCE,
        "project": project,
        "model": effective_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "is_batch": is_batch,
        "cache_ttl": None,
        "session_id": None,
    }

    if has_tokens:
        # Price from the reported token counts via the shared engine. The reported
        # model (not the substituted fallback) is passed so compute_cost itself
        # applies the fallback rates for an unknown model (SPEC.md §4.2).
        event["cost_usd"] = compute_cost(
            {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "cache_read_tokens": cache_read_tokens,
                "is_batch": bool(is_batch),
                "cache_ttl": None,
            },
            pricing,
        )
        if reported_cost is not None:
            # Keep the API's own figure for reconciliation/debugging, but trust the
            # token-derived cost for the stored value (consistent engine).
            meta["reported_cost_usd"] = reported_cost
    else:
        # Cost-only (aggregated) record: store the API's dollar amount verbatim
        # and flag it -- DO NOT fabricate token counts (SPEC.md §11).
        event["cost_usd"] = round(reported_cost, 6)
        meta["aggregated_cost"] = True

    event["raw_meta"] = json.dumps(meta)
    return event


# --- The actual poll (only reached when enabled + key) ----------------------

def _build_request(admin_cfg: Mapping[str, Any], admin_key: str):
    """Construct the urllib Request for the Admin Usage endpoint.

    Lazily imports ``urllib.request`` so importing this module never pulls in an
    HTTP client. The endpoint path is an ASSUMPTION (the Admin Usage & Cost API);
    a user can override the base via ``admin_api.base_url``. Auth uses the
    ``x-api-key`` + ``anthropic-version`` headers the Anthropic API expects.
    """
    import urllib.parse as _urlparse
    import urllib.request as _urlreq

    base = admin_cfg.get("base_url") or DEFAULT_BASE_URL
    base = str(base).rstrip("/")
    # Assumed Admin Usage report path; overridable via config for forward-compat.
    path = admin_cfg.get("usage_path") or "/v1/organizations/usage_report/messages"
    url = base + str(path)

    params = admin_cfg.get("query_params")
    if isinstance(params, Mapping) and params:
        url = url + "?" + _urlparse.urlencode(
            {str(k): str(v) for k, v in params.items()}
        )

    return _urlreq.Request(
        url,
        headers={
            "x-api-key": admin_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="GET",
    )


def _poll(admin_cfg: Mapping[str, Any], admin_key: str) -> Any | None:
    """Perform the outbound poll and return parsed JSON, or ``None`` on failure.

    Fail-soft (SPEC.md §11): any network error, non-200 status, timeout, or JSON
    parse failure is caught and turned into ``None``. This function never raises.
    """
    import urllib.request as _urlreq

    try:
        req = _build_request(admin_cfg, admin_key)
        # noqa: S310 -- user-configured Anthropic Admin endpoint, https by default.
        with _urlreq.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:  # noqa: S310
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            if status != 200:
                return None
            raw = resp.read()
    except Exception:  # noqa: BLE001 -- the poll is strictly best-effort.
        return None

    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:  # noqa: BLE001 -- malformed JSON -> no-op-with-warning.
        return None


# --- Top-level entry point --------------------------------------------------

def ingest_admin_api(
    conn: sqlite3.Connection,
    config: Mapping[str, Any] | None,
    pricing: Mapping[str, Any],
) -> dict[str, Any]:
    """Optionally poll the Anthropic Admin Usage/Cost API (SPEC.md §5.3).

    Returns ``{"rows_added": int, "records_skipped": int, "note"?: str}`` so the
    CLI can report rows added for the ``admin_api`` source like every other
    collector.

    GATING (exact order; the headline guarantee of this phase):

    1. ``admin_api.enabled`` falsey (DEFAULT) -> immediate zero-result no-op. No
       key read, no HTTP import, no network call, nothing raised.
    2. enabled but no resolvable key -> silent zero-result no-op (no network).
    3. enabled AND key present -> attempt the poll, fail-soft on any error.
    """
    admin_cfg = (config or {}).get("admin_api") if config else None
    if not isinstance(admin_cfg, Mapping):
        admin_cfg = {}

    # (1) Disabled is the default -> COMPLETELY INERT. Return before reading the
    # key or importing/using any HTTP client.
    if not admin_cfg.get("enabled"):
        return _result(note="admin_api disabled (default); no-op.")

    # (2) Enabled but no key -> silent no-op. Still no network call.
    admin_key = resolve_admin_key(admin_cfg)
    if not admin_key:
        return _result(note="admin_api enabled but no key found; no-op.")

    # (3) Enabled + key -> attempt the real poll (fail-soft).
    payload = _poll(admin_cfg, admin_key)
    if payload is None:
        # Network error / non-200 / timeout / malformed JSON: no-op-with-warning.
        return _result(note="admin_api poll failed or returned no data; no-op.")

    rows_added = 0
    records_skipped = 0
    for record in _iter_usage_records(payload):
        try:
            event = _record_to_event(record, pricing)
        except Exception:  # noqa: BLE001 -- one bad record never aborts the run.
            records_skipped += 1
            continue
        if event is None:
            records_skipped += 1
            continue
        try:
            if insert_usage_event(conn, event):
                rows_added += 1
        except Exception:  # noqa: BLE001 -- a single insert failure is fail-soft.
            records_skipped += 1
            continue

    conn.commit()
    return _result(rows_added=rows_added, records_skipped=records_skipped)
