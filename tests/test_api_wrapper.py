"""Tests for the API wrapper collector (SPEC.md §5.2, §12).

Covers ``log_usage`` against both plain ``dict`` responses and typed-object
stand-ins, missing cache fields defaulting to 0, batch/cache_ttl pricing,
unknown-model flagging, dedupe, and the §13 requirement that importing the
module does not pull in the optional ``anthropic`` SDK.

All tests use an explicit temp DB (via ``conn=`` or ``db_path=``) so the real
``data/clauditor.db`` is never touched.
"""

from __future__ import annotations

import json
import sys

import pytest

from collectors.api_wrapper import build_event, log_usage, make_event_uid, track
from core.config import load_pricing
from core.db import event_count, init_db
from core.pricing import compute_cost


# --- Fixtures / helpers -----------------------------------------------------

@pytest.fixture()
def pricing():
    return load_pricing()


@pytest.fixture()
def conn(tmp_path):
    """A fresh temp-DB connection (never the real data/clauditor.db)."""
    c = init_db(tmp_path / "test.db")
    try:
        yield c
    finally:
        c.close()


class _Usage:
    """Typed-object stand-in for an SDK ``response.usage``."""

    def __init__(self, input_tokens, output_tokens, **extra):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        for k, v in extra.items():
            setattr(self, k, v)


class _Response:
    """Typed-object stand-in for an SDK message response (attr access only)."""

    def __init__(self, model, usage, id=None):
        self.model = model
        self.usage = usage
        if id is not None:
            self.id = id


def _dict_response(model, *, input_tokens, output_tokens, id="msg_dict", **cache):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    usage.update(cache)
    return {"id": id, "model": model, "usage": usage}


def _fetch_one(conn):
    return conn.execute(
        "SELECT * FROM usage_events ORDER BY id DESC LIMIT 1"
    ).fetchone()


# --- Mode A: dict responses -------------------------------------------------

def test_log_usage_dict_basic(conn, pricing):
    resp = _dict_response(
        "claude-opus-4-8", input_tokens=1000, output_tokens=500, id="msg_a"
    )
    inserted = log_usage(resp, project="my-rag-app", conn=conn)
    assert inserted is True
    assert event_count(conn) == 1

    row = _fetch_one(conn)
    assert row["source"] == "api"
    assert row["project"] == "my-rag-app"
    assert row["model"] == "claude-opus-4-8"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["cache_creation_tokens"] == 0
    assert row["cache_read_tokens"] == 0
    assert row["is_batch"] == 0
    assert row["cache_ttl"] is None
    assert row["session_id"] == "msg_a"

    # Cost reconciles with a direct compute_cost call (SPEC.md §4.2).
    expected = compute_cost(
        {
            "model": "claude-opus-4-8",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "is_batch": False,
            "cache_ttl": None,
        },
        pricing,
    )
    assert row["cost_usd"] == expected
    assert expected > 0


def test_log_usage_dict_with_cache_fields(conn, pricing):
    resp = _dict_response(
        "claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=800,
        id="msg_cache",
        cache_creation_input_tokens=1500,
        cache_read_input_tokens=4000,
    )
    log_usage(resp, project="support-bot", cache_ttl="5m", conn=conn)

    row = _fetch_one(conn)
    assert row["cache_creation_tokens"] == 1500
    assert row["cache_read_tokens"] == 4000
    assert row["cache_ttl"] == "5m"

    expected = compute_cost(
        {
            "model": "claude-sonnet-4-6",
            "input_tokens": 2000,
            "output_tokens": 800,
            "cache_creation_tokens": 1500,
            "cache_read_tokens": 4000,
            "is_batch": False,
            "cache_ttl": "5m",
        },
        pricing,
    )
    assert row["cost_usd"] == expected


# --- Mode A: typed-object responses -----------------------------------------

def test_log_usage_typed_object(conn, pricing):
    resp = _Response(
        "claude-opus-4-8",
        _Usage(1000, 500),
        id="msg_typed",
    )
    inserted = log_usage(resp, project="typed-app", conn=conn)
    assert inserted is True

    row = _fetch_one(conn)
    assert row["model"] == "claude-opus-4-8"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    # Identical result to the dict form with the same tokens.
    expected = compute_cost(
        {
            "model": "claude-opus-4-8",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "is_batch": False,
            "cache_ttl": None,
        },
        pricing,
    )
    assert row["cost_usd"] == expected


def test_log_usage_typed_object_with_cache_attrs(conn):
    resp = _Response(
        "claude-sonnet-4-6",
        _Usage(
            2000,
            800,
            cache_creation_input_tokens=1500,
            cache_read_input_tokens=4000,
        ),
        id="msg_typed_cache",
    )
    log_usage(resp, project="typed-cache", cache_ttl="1h", conn=conn)
    row = _fetch_one(conn)
    assert row["cache_creation_tokens"] == 1500
    assert row["cache_read_tokens"] == 4000
    assert row["cache_ttl"] == "1h"


def test_dict_and_object_identical(conn):
    """Same tokens via dict and typed object produce the same cost."""
    d = build_event(
        _dict_response("claude-opus-4-8", input_tokens=1234, output_tokens=567, id="x")
    , project="p")
    o = build_event(
        _Response("claude-opus-4-8", _Usage(1234, 567), id="x"), project="p"
    )
    assert d["cost_usd"] == o["cost_usd"]
    assert d["input_tokens"] == o["input_tokens"] == 1234
    assert d["output_tokens"] == o["output_tokens"] == 567


# --- Missing cache fields default to 0 (no crash), both forms ---------------

def test_missing_cache_fields_default_zero_dict(conn):
    # No cache_* keys at all.
    resp = _dict_response("claude-opus-4-8", input_tokens=10, output_tokens=5, id="md")
    log_usage(resp, project="p", conn=conn)
    row = _fetch_one(conn)
    assert row["cache_creation_tokens"] == 0
    assert row["cache_read_tokens"] == 0


def test_missing_cache_fields_default_zero_object(conn):
    # Usage object with no cache attributes -> getattr default 0, no crash.
    resp = _Response("claude-opus-4-8", _Usage(10, 5), id="mo")
    log_usage(resp, project="p", conn=conn)
    row = _fetch_one(conn)
    assert row["cache_creation_tokens"] == 0
    assert row["cache_read_tokens"] == 0


# --- Batch + cache_ttl reflected in row and cost ----------------------------

def test_is_batch_reflected_in_row_and_cost(conn, pricing):
    resp = _dict_response(
        "claude-opus-4-8", input_tokens=1000, output_tokens=500, id="batch1"
    )
    log_usage(resp, project="nightly", is_batch=True, conn=conn)
    row = _fetch_one(conn)
    assert row["is_batch"] == 1

    non_batch = compute_cost(
        {"model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 500,
         "cache_creation_tokens": 0, "cache_read_tokens": 0,
         "is_batch": False, "cache_ttl": None},
        pricing,
    )
    # Batch is 50% off (SPEC.md §4.1 batch_multiplier = 0.5).
    assert row["cost_usd"] == pytest.approx(non_batch * 0.5)


@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_cache_ttl_reflected_in_row_and_cost(conn, pricing, ttl):
    resp = _dict_response(
        "claude-opus-4-8",
        input_tokens=1000,
        output_tokens=500,
        id=f"ttl_{ttl}",
        cache_creation_input_tokens=2000,
    )
    log_usage(resp, project="cachy", cache_ttl=ttl, conn=conn)
    row = _fetch_one(conn)
    assert row["cache_ttl"] == ttl

    expected = compute_cost(
        {"model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 500,
         "cache_creation_tokens": 2000, "cache_read_tokens": 0,
         "is_batch": False, "cache_ttl": ttl},
        pricing,
    )
    assert row["cost_usd"] == expected
    # 1h cache write costs strictly more than 5m for the same write tokens.


def test_5m_cheaper_than_1h_for_cache_write(conn):
    base = dict(model="claude-opus-4-8", input_tokens=0, output_tokens=0,
                cache_read_input_tokens=0, cache_creation_input_tokens=5000)
    e5 = build_event(
        {"id": "c5", "model": base["model"],
         "usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_creation_input_tokens": 5000}},
        project="p", cache_ttl="5m",
    )
    e1 = build_event(
        {"id": "c1", "model": base["model"],
         "usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_creation_input_tokens": 5000}},
        project="p", cache_ttl="1h",
    )
    assert e1["cost_usd"] > e5["cost_usd"]


# --- Unknown model ----------------------------------------------------------

def test_unknown_model_recorded_and_flagged(conn):
    resp = _dict_response(
        "claude-made-up-9", input_tokens=1000, output_tokens=500, id="unk1"
    )
    inserted = log_usage(resp, project="p", conn=conn)
    assert inserted is True
    row = _fetch_one(conn)
    # Reported model name is preserved (Phase-3 collector convention); the
    # unknown-model flag lives in raw_meta and the cost uses fallback rates.
    assert row["model"] == "claude-made-up-9"
    meta = json.loads(row["raw_meta"])
    assert meta["unknown_model"] is True
    assert meta["reported_model"] == "claude-made-up-9"
    assert row["cost_usd"] > 0


def test_missing_model_uses_fallback(conn):
    # No model name at all -> NOT NULL column gets the fallback model.
    resp = {"id": "noml", "usage": {"input_tokens": 10, "output_tokens": 5}}
    log_usage(resp, project="p", conn=conn)
    row = _fetch_one(conn)
    assert row["model"] == "claude-sonnet-4-6"
    meta = json.loads(row["raw_meta"])
    assert meta["unknown_model"] is True


# --- Dedupe -----------------------------------------------------------------

def test_same_response_id_deduped(conn):
    resp = _dict_response(
        "claude-opus-4-8", input_tokens=1, output_tokens=1, id="dup_me"
    )
    assert log_usage(resp, project="p", conn=conn) is True
    assert log_usage(resp, project="p", conn=conn) is False
    assert event_count(conn) == 1


def test_distinct_response_ids_not_deduped(conn):
    r1 = _dict_response("claude-opus-4-8", input_tokens=1, output_tokens=1, id="a")
    r2 = _dict_response("claude-opus-4-8", input_tokens=1, output_tokens=1, id="b")
    assert log_usage(r1, project="p", conn=conn) is True
    assert log_usage(r2, project="p", conn=conn) is True
    assert event_count(conn) == 2


def test_no_id_responses_each_get_a_row(conn):
    # Without an id we can't tell distinct calls apart; each must record a row
    # (uid mixes in a uuid4) rather than overwriting a prior identical-looking one.
    r = {"model": "claude-opus-4-8",
         "usage": {"input_tokens": 1, "output_tokens": 1}}
    assert log_usage(r, project="p", conn=conn) is True
    assert log_usage(r, project="p", conn=conn) is True
    assert event_count(conn) == 2


def test_make_event_uid_stable_with_id():
    a = make_event_uid("msg_1", "proj", False, None)
    b = make_event_uid("msg_1", "proj", False, None)
    assert a == b
    c = make_event_uid("msg_2", "proj", False, None)
    assert a != c


# --- DB-path default path (no explicit conn) --------------------------------

def test_log_usage_db_path_creates_schema(tmp_path):
    db = tmp_path / "fresh.db"
    resp = _dict_response("claude-opus-4-8", input_tokens=5, output_tokens=5, id="p1")
    inserted = log_usage(resp, project="p", db_path=db)
    assert inserted is True
    # init_db ran for a fresh user who only logs API calls.
    c = init_db(db)
    try:
        assert event_count(c) == 1
    finally:
        c.close()


# --- Mode B: track() --------------------------------------------------------

class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.created = 0

    def create(self, **kwargs):
        self.created += 1
        return self._response

    def other_method(self):
        return "delegated"


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)
        self.attr = "client-attr"


def test_track_auto_logs_and_returns_real_response(conn):
    resp = _dict_response(
        "claude-opus-4-8", input_tokens=100, output_tokens=50, id="tracked"
    )
    client = _FakeClient(resp)
    tracked = track(client, project="tracked-app", conn=conn)

    returned = tracked.messages.create(model="claude-opus-4-8", messages=[])
    # Real response returned unchanged.
    assert returned is resp
    # And a row was logged.
    assert event_count(conn) == 1
    row = _fetch_one(conn)
    assert row["project"] == "tracked-app"
    assert row["source"] == "api"


def test_track_delegates_unknown_attrs(conn):
    resp = _dict_response("claude-opus-4-8", input_tokens=1, output_tokens=1, id="d")
    client = _FakeClient(resp)
    tracked = track(client, project="p", conn=conn)
    assert tracked.attr == "client-attr"
    assert tracked.messages.other_method() == "delegated"


def test_track_fail_soft_on_logging_error(conn):
    # A response missing usage entirely would normally still log (defaults 0),
    # but force a logging failure by passing a response that explodes on access.
    class _Boom:
        @property
        def model(self):
            raise RuntimeError("boom")

    client = _FakeClient(_Boom())
    tracked = track(client, project="p", conn=conn)
    # The user's real API call must still succeed despite the logging error.
    returned = tracked.messages.create()
    assert isinstance(returned, _Boom)
    assert event_count(conn) == 0


# --- §13: anthropic SDK stays optional --------------------------------------

def test_import_does_not_require_anthropic():
    # Importing the wrapper (and the clauditor re-export) must not import the
    # optional SDK (SPEC.md §13). It is not installed in the test env at all.
    import collectors.api_wrapper  # noqa: F401
    from clauditor import log_usage as _lu  # noqa: F401

    assert "anthropic" not in sys.modules


def test_no_toplevel_anthropic_import_in_source():
    import collectors.api_wrapper as mod

    src = open(mod.__file__, encoding="utf-8").read()
    # No bare top-level `import anthropic` / `from anthropic import ...`.
    for line in src.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import anthropic")
        assert not stripped.startswith("from anthropic")


def test_clauditor_reexport_works():
    import clauditor

    assert hasattr(clauditor, "log_usage")
    assert clauditor.log_usage is log_usage
    assert hasattr(clauditor, "track")
