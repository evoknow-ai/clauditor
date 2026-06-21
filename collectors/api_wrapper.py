"""API wrapper collector (SPEC.md §5.2).

A small importable helper the user drops into their own code to log their own
Anthropic API calls into clauditor's ``usage_events`` table.

Two integration modes:

* **Mode A -- explicit logging** (the primary deliverable)::

      from clauditor import log_usage

      resp = client.messages.create(...)
      log_usage(resp, project="my-rag-app", is_batch=False, cache_ttl="5m")

* **Mode B -- wrapped client** (nice-to-have): :func:`track` returns a thin
  proxy that auto-logs every ``.messages.create()`` call.

Design constraints (SPEC.md §11, §13 -- non-negotiable):

* The Anthropic SDK is an **optional** extra. This module never imports
  ``anthropic`` at the top level (or anywhere on the core import path); every
  field read is **duck-typed** so a plain ``dict`` response and the SDK's typed
  objects both work. Importing this module must never require ``anthropic``.
* Token counts are taken verbatim from the response -- never re-tokenized
  (SPEC.md §11). Missing cache fields default to ``0`` and never crash.
* An unknown model is still recorded, flagged ``raw_meta.unknown_model = true``
  (consistent with the Phase-3 Claude Code collector, SPEC.md §4.2).
* Logging is **fail-soft** under Mode B: a logging error must never break the
  user's actual API call.

The public entry points are :func:`log_usage` and :func:`track`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

from core.config import load_pricing
from core.db import init_db, insert_usage_event
from core.pricing import compute_cost, is_known_model

SOURCE = "api"


# --- Duck-typed field access ------------------------------------------------

_MISSING = object()


def _dget(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an object that may be a dict OR an attr-style object.

    Tries mapping access first (``.get``), then attribute access
    (``getattr``). This is the single primitive that lets :func:`log_usage`
    work against both a plain ``dict`` response and the Anthropic SDK's typed
    response objects without importing the SDK (SPEC.md §5.2).
    """
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    value = getattr(obj, key, _MISSING)
    if value is _MISSING:
        return default
    return value


def _int_token(value: Any) -> int:
    """Coerce a token field to a non-negative int; missing/invalid -> 0."""
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


# --- event_uid --------------------------------------------------------------

def make_event_uid(
    response_id: str | None,
    project: str | None,
    is_batch: bool,
    cache_ttl: str | None,
) -> str:
    """Build the dedupe key for an API usage event.

    Anthropic responses carry a unique ``id`` (e.g. ``msg_...``). When present
    we hash ``source + response_id + project + is_batch + cache_ttl`` so that
    logging *the same response* twice (e.g. a retried ``log_usage`` call) is
    deduped by the UNIQUE(event_uid) constraint, while two genuinely distinct
    responses -- which have distinct ids -- never collide.

    When no id is available (e.g. a hand-built ``dict`` with no ``id``), we
    cannot tell two legitimately-distinct calls apart, and the spec warns
    against a uid so strict it makes distinct calls collide. We therefore mix
    in a random ``uuid4`` so each such call records its own row rather than
    silently overwriting a previous identical-looking one.
    """
    if response_id:
        parts = [
            SOURCE,
            str(response_id),
            project or "",
            "1" if is_batch else "0",
            cache_ttl or "",
        ]
    else:
        parts = [SOURCE, "noid", uuid.uuid4().hex]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


# --- Pricing cache ----------------------------------------------------------

def _resolve_pricing(pricing: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a pricing table, loading the shipped ``pricing.json`` if needed."""
    if pricing is not None:
        return pricing
    return load_pricing()


# --- Mode A: explicit logging -----------------------------------------------

def build_event(
    response: Any,
    project: str | None,
    *,
    is_batch: bool = False,
    cache_ttl: str | None = None,
    pricing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build (but do not insert) the ``usage_events`` row for a response.

    Reads every field via :func:`_dget` so dict and typed-object responses are
    treated identically. Computes ``cost_usd`` via :func:`core.pricing.compute_cost`
    with ``is_batch``/``cache_ttl`` applied so batch and cache-write pricing are
    honored. Exposed separately from :func:`log_usage` so it is unit-testable
    and reusable.
    """
    pricing = _resolve_pricing(pricing)

    model = _dget(response, "model")
    response_id = _dget(response, "id")

    usage = _dget(response, "usage")
    input_tokens = _int_token(_dget(usage, "input_tokens", 0))
    output_tokens = _int_token(_dget(usage, "output_tokens", 0))
    cache_creation_tokens = _int_token(
        _dget(usage, "cache_creation_input_tokens", 0)
    )
    cache_read_tokens = _int_token(_dget(usage, "cache_read_input_tokens", 0))

    is_batch_flag = bool(is_batch)
    unknown_model = not is_known_model(model, pricing)
    # Store the reported model name when present (so the user sees what they
    # actually called), only substituting the fallback when no model name is
    # available at all -- the schema's ``model`` column is NOT NULL. This
    # matches the Phase-3 Claude Code collector's convention (SPEC.md §4.2).
    effective_model = model if model else pricing["fallback_model"]

    event = {
        "event_uid": make_event_uid(response_id, project, is_batch_flag, cache_ttl),
        "ts": _utc_now_iso(),
        "source": SOURCE,
        "project": project,
        "model": effective_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "is_batch": 1 if is_batch_flag else 0,
        "cache_ttl": cache_ttl,
        "session_id": str(response_id) if response_id else None,
        "raw_meta": None,
    }

    # Cost is computed against the *reported* model so an unknown model still
    # falls back to the fallback rates inside compute_cost itself (SPEC.md §4.2).
    event["cost_usd"] = compute_cost(
        {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "is_batch": is_batch_flag,
            "cache_ttl": cache_ttl,
        },
        pricing,
    )

    if unknown_model:
        event["raw_meta"] = json.dumps(
            {"unknown_model": True, "reported_model": model}
        )

    return event


def log_usage(
    response: Any,
    project: str | None = None,
    is_batch: bool = False,
    cache_ttl: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    pricing: Mapping[str, Any] | None = None,
) -> bool:
    """Record one of the user's own Anthropic API calls (SPEC.md §5.2, Mode A).

    ``response`` may be a plain ``dict`` or the Anthropic SDK's typed response
    object -- every field is duck-typed, so the SDK is never required to import
    or use this function. Reads::

        response.model / response["model"]
        response.usage.input_tokens / .output_tokens
        getattr(response.usage, "cache_creation_input_tokens", 0)
        getattr(response.usage, "cache_read_input_tokens", 0)

    ``is_batch`` and ``cache_ttl`` are passed through to the pricing engine so
    batch (50% off) and cache-write (5m/1h) pricing are honored, and stored on
    the row. An unknown model is still recorded and flagged
    ``raw_meta.unknown_model = true``.

    Connection handling (ergonomic for the documented one-liner):

    * Pass ``conn`` to reuse an existing connection (caller owns commit/close).
    * Otherwise a connection is opened at ``db_path`` (default
      ``data/clauditor.db``); :func:`core.db.init_db` is called first so a fresh
      user who only ever logs API calls still gets the schema. The connection is
      committed and closed before returning.

    Returns ``True`` if a new row was inserted, ``False`` if it was deduped by
    the ``event_uid`` UNIQUE constraint (e.g. the same response logged twice).
    """
    event = build_event(
        response,
        project,
        is_batch=is_batch,
        cache_ttl=cache_ttl,
        pricing=pricing,
    )

    if conn is not None:
        # Caller owns the connection's lifecycle (commit/close).
        return insert_usage_event(conn, event)

    owned = init_db(db_path)
    try:
        inserted = insert_usage_event(owned, event)
        owned.commit()
        return inserted
    finally:
        owned.close()


# --- Mode B: wrapped client (nice-to-have, SPEC.md §5.2) --------------------

class _MessagesProxy:
    """Wraps a client's ``.messages`` so ``.create()`` auto-logs its response.

    Every other attribute is delegated unchanged to the real ``messages``
    object, so the proxy is a drop-in. The real response is always returned to
    the caller untouched; a logging failure is swallowed (fail-soft, SPEC.md
    §11) so instrumentation can never break the user's actual API call.
    """

    def __init__(self, real_messages: Any, owner: "_TrackedClient") -> None:
        self._real_messages = real_messages
        self._owner = owner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        response = self._real_messages.create(*args, **kwargs)
        try:
            log_usage(
                response,
                project=self._owner._project,
                is_batch=self._owner._is_batch,
                cache_ttl=self._owner._cache_ttl,
                conn=self._owner._conn,
                db_path=self._owner._db_path,
                pricing=self._owner._pricing,
            )
        except Exception:
            # Logging must never break the user's real API call (fail-soft).
            pass
        return response

    def __getattr__(self, name: str) -> Any:
        # Delegate anything we don't override (e.g. .stream, .count_tokens).
        return getattr(self._real_messages, name)


class _TrackedClient:
    """A thin proxy over an Anthropic client that auto-logs ``messages.create``.

    Only ``.messages`` is intercepted; every other attribute/method is delegated
    to the wrapped client unchanged, so the tracked client behaves identically
    to the original.
    """

    def __init__(
        self,
        client: Any,
        *,
        project: str | None,
        is_batch: bool,
        cache_ttl: str | None,
        conn: sqlite3.Connection | None,
        db_path: str | Path | None,
        pricing: Mapping[str, Any] | None,
    ) -> None:
        self._client = client
        self._project = project
        self._is_batch = is_batch
        self._cache_ttl = cache_ttl
        self._conn = conn
        self._db_path = db_path
        self._pricing = pricing

    @property
    def messages(self) -> _MessagesProxy:
        return _MessagesProxy(self._client.messages, self)

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else (e.g. .beta, .with_options) unchanged.
        return getattr(self._client, name)


def track(
    client: Any,
    project: str | None = None,
    *,
    is_batch: bool = False,
    cache_ttl: str | None = None,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    pricing: Mapping[str, Any] | None = None,
) -> _TrackedClient:
    """Return a proxy over ``client`` that auto-logs every ``messages.create()``.

    Mode B from SPEC.md §5.2 (nice-to-have). Usage::

        client = track(anthropic.Anthropic(), project="my-app")
        resp = client.messages.create(...)   # logged automatically

    The proxy duck-types the client (the SDK is never imported here), returns
    the real response unchanged, and fails soft: a logging error never breaks
    the underlying API call.
    """
    return _TrackedClient(
        client,
        project=project,
        is_batch=is_batch,
        cache_ttl=cache_ttl,
        conn=conn,
        db_path=db_path,
        pricing=pricing,
    )
