"""Tests for the OPTIONAL, flag-gated Admin API collector (SPEC.md §5.3, §11).

The HEADLINE guarantee under test: with the default config the collector is
COMPLETELY INERT -- it never runs, never imports/uses an HTTP client, never
makes a network call, and never raises. An enabled-but-keyless config is a clean
silent no-op, not a crash. The live-poll path can't hit the real API, so the
HTTP layer (``urllib.request.urlopen``) is monkeypatched in every test that
reaches it, and a guard that RAISES on call proves no-network in the gated paths.

All tests use temp DBs and monkeypatched HTTP only -- no real network call, no
touching the real data/clauditor.db or ~/.claude.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request

import pytest

from collectors.admin_api import ingest_admin_api, resolve_admin_key
from core.config import load_pricing
from core.db import event_count, init_db


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture()
def pricing():
    return load_pricing()


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "clauditor.db"
    c = init_db(str(db_path))
    yield c
    c.close()


@pytest.fixture()
def boom_urlopen(monkeypatch):
    """Install a urlopen that RAISES if ever called -> proves no network call."""
    calls = {"n": 0}

    def _raise(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError(
            "urllib.request.urlopen was called -- the collector made a network "
            "call when it must not have."
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    return calls


def _canned_payload():
    """A schema-plausible Admin Usage payload (two model rows in one window)."""
    return {
        "data": [
            {
                "starting_at": "2026-06-19T00:00:00Z",
                "results": [
                    {
                        "model": "claude-opus-4-8",
                        "workspace_id": "wrk_team_a",
                        "input_tokens": 1_000_000,
                        "output_tokens": 200_000,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                    {
                        "model": "claude-haiku-4-5",
                        "workspace_id": "wrk_team_b",
                        "input_tokens": 500_000,
                        "output_tokens": 100_000,
                    },
                ],
            }
        ]
    }


class _FakeResponse:
    """Minimal context-manager stand-in for urlopen's return value."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def _install_fake_http(monkeypatch, payload, status=200):
    """Monkeypatch urlopen to return a canned payload; record call count."""
    calls = {"n": 0}
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload

    def _fake(req, timeout=None):
        calls["n"] += 1
        return _FakeResponse(body, status=status)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


# --- Import inertness -------------------------------------------------------

def test_import_does_not_require_anthropic():
    """Importing the module must not pull in the anthropic SDK (SPEC.md §13)."""
    import importlib
    import sys

    # Drop any cached anthropic so a real import attempt would be observable.
    had = sys.modules.pop("anthropic", None)
    try:
        mod = importlib.import_module("collectors.admin_api")
        importlib.reload(mod)
        assert "anthropic" not in sys.modules
    finally:
        if had is not None:
            sys.modules["anthropic"] = had


# --- (1) DISABLED (default) -> completely inert -----------------------------

def test_disabled_default_is_noop_no_network(conn, pricing, boom_urlopen):
    """Default config (admin_api disabled) -> zero result, NO network, no raise."""
    config = {"admin_api": {"enabled": False, "key_env": "ANTHROPIC_ADMIN_KEY"}}
    result = ingest_admin_api(conn, config, pricing)

    assert result["rows_added"] == 0
    assert event_count(conn) == 0
    assert boom_urlopen["n"] == 0  # urlopen never called.


def test_disabled_even_with_key_present_is_noop(conn, pricing, boom_urlopen, monkeypatch):
    """Disabled wins even if a key is set in env -> still inert, no network."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-should-not-matter")
    config = {"admin_api": {"enabled": False, "key_env": "ANTHROPIC_ADMIN_KEY"}}
    result = ingest_admin_api(conn, config, pricing)

    assert result["rows_added"] == 0
    assert boom_urlopen["n"] == 0


def test_no_admin_api_key_at_all_is_noop(conn, pricing, boom_urlopen):
    """Missing admin_api config entirely -> treated as disabled, inert."""
    result = ingest_admin_api(conn, {}, pricing)
    assert result["rows_added"] == 0
    assert boom_urlopen["n"] == 0

    result_none = ingest_admin_api(conn, None, pricing)
    assert result_none["rows_added"] == 0
    assert boom_urlopen["n"] == 0


# --- (2) ENABLED but NO KEY -> silent no-op ---------------------------------

def test_enabled_missing_env_var_is_silent_noop(conn, pricing, boom_urlopen, monkeypatch):
    """enabled=true, env var unset, no inline key -> no-op, no network, no raise."""
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}
    result = ingest_admin_api(conn, config, pricing)

    assert result["rows_added"] == 0
    assert event_count(conn) == 0
    assert boom_urlopen["n"] == 0


def test_enabled_empty_inline_key_is_silent_noop(conn, pricing, boom_urlopen, monkeypatch):
    """enabled=true with an empty/whitespace inline key and no env -> no-op."""
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY", "key": "   "}}
    result = ingest_admin_api(conn, config, pricing)

    assert result["rows_added"] == 0
    assert boom_urlopen["n"] == 0


def test_resolve_admin_key_precedence(monkeypatch):
    """Inline key beats env; whitespace/empty -> None; env used otherwise."""
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    assert resolve_admin_key({"key_env": "ANTHROPIC_ADMIN_KEY"}) is None
    assert resolve_admin_key({"key": "  ", "key_env": "ANTHROPIC_ADMIN_KEY"}) is None

    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-from-env")
    assert resolve_admin_key({"key_env": "ANTHROPIC_ADMIN_KEY"}) == "sk-from-env"
    # Inline key takes precedence over env.
    assert resolve_admin_key({"key": "sk-inline", "key_env": "ANTHROPIC_ADMIN_KEY"}) == "sk-inline"


# --- (3) ENABLED + KEY -> attempts the poll ---------------------------------

def test_enabled_with_key_polls_and_inserts(conn, pricing, monkeypatch):
    """enabled+key -> poll attempted, rows inserted with source='admin_api'."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    calls = _install_fake_http(monkeypatch, _canned_payload())
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)

    assert calls["n"] == 1  # the poll was actually attempted.
    assert result["rows_added"] == 2

    rows = conn.execute(
        "SELECT source, model, project, cost_usd FROM usage_events ORDER BY model"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["source"] == "admin_api" for r in rows)
    models = {r["model"] for r in rows}
    assert models == {"claude-opus-4-8", "claude-haiku-4-5"}


def test_cost_reconciles_with_pricing_engine(conn, pricing, monkeypatch):
    """A polled row's cost matches compute_cost over the same token counts."""
    from core.pricing import compute_cost

    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    _install_fake_http(monkeypatch, _canned_payload())
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    ingest_admin_api(conn, config, pricing)

    row = conn.execute(
        "SELECT cost_usd FROM usage_events WHERE model = 'claude-opus-4-8'"
    ).fetchone()
    expected = compute_cost(
        {
            "model": "claude-opus-4-8",
            "input_tokens": 1_000_000,
            "output_tokens": 200_000,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        pricing,
    )
    # 1M input @ $5 + 200k output @ $25 = 5 + 5 = $10.
    assert expected == pytest.approx(10.0)
    assert row["cost_usd"] == pytest.approx(expected)


def test_repoll_dedupes_no_double_count(conn, pricing, monkeypatch):
    """Re-running with the same payload inserts no new rows (INSERT OR IGNORE)."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    _install_fake_http(monkeypatch, _canned_payload())
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    first = ingest_admin_api(conn, config, pricing)
    assert first["rows_added"] == 2
    assert event_count(conn) == 2

    second = ingest_admin_api(conn, config, pricing)
    assert second["rows_added"] == 0  # deduped.
    assert event_count(conn) == 2


def test_aggregated_cost_only_record_stored_verbatim(conn, pricing, monkeypatch):
    """A cost-only record stores the dollar amount, fabricates no tokens."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    payload = {
        "data": [
            {
                "starting_at": "2026-06-18T00:00:00Z",
                "results": [
                    {"workspace_id": "wrk_cost", "cost_usd": 42.5},
                ],
            }
        ]
    }
    _install_fake_http(monkeypatch, payload)
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)
    assert result["rows_added"] == 1

    row = conn.execute(
        "SELECT cost_usd, input_tokens, output_tokens, raw_meta FROM usage_events"
    ).fetchone()
    assert row["cost_usd"] == pytest.approx(42.5)
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    meta = json.loads(row["raw_meta"])
    assert meta.get("aggregated_cost") is True


def test_inline_key_path_polls(conn, pricing, monkeypatch):
    """An inline config key (no env var) still reaches the poll."""
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    calls = _install_fake_http(monkeypatch, _canned_payload())
    config = {"admin_api": {"enabled": True, "key": "sk-inline", "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)
    assert calls["n"] == 1
    assert result["rows_added"] == 2


# --- (4) ENABLED + KEY but HTTP FAILS -> fail-soft --------------------------

def test_http_raises_is_failsoft(conn, pricing, monkeypatch):
    """urlopen raising -> no exception escapes, zero rows, ingest continues."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")

    def _raise(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)  # must not raise.
    assert result["rows_added"] == 0
    assert event_count(conn) == 0


def test_http_non_200_is_failsoft(conn, pricing, monkeypatch):
    """A non-200 status -> no rows, no raise."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    _install_fake_http(monkeypatch, _canned_payload(), status=503)
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)
    assert result["rows_added"] == 0
    assert event_count(conn) == 0


def test_malformed_json_is_failsoft(conn, pricing, monkeypatch):
    """Malformed JSON body -> no rows, no raise (no-op-with-warning)."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    _install_fake_http(monkeypatch, b"{not valid json")
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)
    assert result["rows_added"] == 0
    assert event_count(conn) == 0


def test_partial_bad_records_skipped(conn, pricing, monkeypatch):
    """A payload with one good and one junk record inserts the good one only."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-admin-real")
    payload = {
        "data": [
            {
                "starting_at": "2026-06-17T00:00:00Z",
                "results": [
                    {"model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 50},
                    "this is not a dict",  # junk -> not yielded
                    {"model": "claude-sonnet-4-6"},  # no tokens/cost -> skipped
                ],
            }
        ]
    }
    _install_fake_http(monkeypatch, payload)
    config = {"admin_api": {"enabled": True, "key_env": "ANTHROPIC_ADMIN_KEY"}}

    result = ingest_admin_api(conn, config, pricing)
    assert result["rows_added"] == 1
    assert event_count(conn) == 1


# --- (5) End-to-end: default ingest never touches the network ---------------

def test_cli_ingest_default_config_no_admin_network(tmp_path, monkeypatch, boom_urlopen):
    """`clauditor ingest` with default config must not poll the Admin API.

    Runs the real ingest path against a temp DB and an empty Claude Code root, so
    the gate is the only thing keeping urlopen unused. A boom-urlopen guard proves
    no network call happened end-to-end; ingest still succeeds.
    """
    import cli

    # Point the Claude Code collector at an empty dir so it cleanly finds nothing.
    empty_claude = tmp_path / "claude"
    (empty_claude / "projects").mkdir(parents=True)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"claude_code_path": str(empty_claude / "projects")}),
        encoding="utf-8",
    )
    db_path = tmp_path / "clauditor.db"

    args = cli.build_parser().parse_args(
        ["ingest", "--config", str(cfg_path), "--db", str(db_path)]
    )
    rc = cli.run_ingest(args)

    assert rc == 0
    assert boom_urlopen["n"] == 0  # no network call during default ingest.

    # admin_api is registered as a source but contributed nothing.
    c = sqlite3.connect(str(db_path))
    try:
        n = c.execute(
            "SELECT COUNT(*) FROM usage_events WHERE source='admin_api'"
        ).fetchone()[0]
        assert n == 0
    finally:
        c.close()


def test_admin_api_in_cli_registry():
    """The collector is wired into the CLI ingest registry as 'admin_api'."""
    import cli

    sources = [name for name, _ in cli._build_collector_registry()]
    assert "admin_api" in sources
