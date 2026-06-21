"""End-to-end tests for the CLI ingest path (SPEC.md §9, §11, §12, §14 item 4).

These drive ``cli.main(["ingest", ...])`` against a temporary
``claude_code_path`` fixture dir so the real ~/.claude is never touched. The DB,
config, and pricing all point at tmp paths via the CLI's global overrides.

Key acceptance check (SPEC.md §11 dedupe / §12): a second ``ingest`` over the
same data adds zero new rows.
"""

import json
from pathlib import Path

import pytest

import cli
from core.db import event_count, get_connection

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"


# --- A couple of valid assistant-usage lines (real schema shapes) -----------

LINE_MESSAGE_USAGE = json.dumps(
    {
        "type": "assistant",
        "timestamp": "2026-06-20T10:00:00+00:00",
        "message": {
            "id": "msg_aaa",
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 4000,
            },
        },
    }
)

LINE_TOP_LEVEL_USAGE = json.dumps(
    {
        "ts": "2026-06-20T10:05:00+00:00",
        "model": "claude-sonnet-4-6",
        "id": "msg_bbb",
        "usage": {"input_tokens": 300, "output_tokens": 50},
    }
)

# A non-usage line that must be skipped, proving fail-soft through the CLI.
LINE_NO_USAGE = json.dumps({"type": "user", "message": {"content": "hi"}})


@pytest.fixture
def env(tmp_path):
    """Build a tmp claude_code_path, a config pointing at it, and a tmp DB.

    Returns the argv-tail of global overrides plus the db path so a test can do
    ``cli.main(["ingest", *env.overrides])``.
    """
    claude_root = tmp_path / "claude"
    projects = claude_root / "projects" / "-Users-x-projects-demo"
    projects.mkdir(parents=True)
    session = projects / "sess1.jsonl"
    session.write_text(
        "\n".join([LINE_MESSAGE_USAGE, LINE_NO_USAGE, LINE_TOP_LEVEL_USAGE]) + "\n",
        encoding="utf-8",
    )

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"claude_code_path": str(claude_root)}), encoding="utf-8"
    )

    db_path = tmp_path / "clauditor.db"

    class Env:
        overrides = [
            "--config", str(config_path),
            "--pricing", str(PRICING_PATH),
            "--db", str(db_path),
        ]

    Env.db_path = db_path
    Env.session = session
    return Env


def _count(db_path: Path) -> int:
    conn = get_connection(db_path)
    try:
        return event_count(conn)
    finally:
        conn.close()


# --- ingest end-to-end ------------------------------------------------------

def test_ingest_creates_db_and_inserts_rows(env, capsys):
    rc = cli.main(["ingest", *env.overrides])
    assert rc == 0

    # DB created and the two usage lines persisted (the user line was skipped).
    assert env.db_path.exists()
    assert _count(env.db_path) == 2

    out = capsys.readouterr().out
    assert "claude_code: 2 rows added" in out


def test_second_ingest_adds_zero_new_rows(env, capsys):
    """Real-run dedupe: re-ingesting the same data adds nothing (SPEC.md §11)."""
    assert cli.main(["ingest", *env.overrides]) == 0
    assert _count(env.db_path) == 2
    capsys.readouterr()  # discard first run's output.

    assert cli.main(["ingest", *env.overrides]) == 0
    assert _count(env.db_path) == 2  # unchanged

    out = capsys.readouterr().out
    assert "claude_code: 0 rows added" in out


def test_ingest_reports_per_source_and_details(env, capsys):
    cli.main(["ingest", *env.overrides])
    out = capsys.readouterr().out
    # Per-source line includes the cheap extras (files scanned / lines skipped).
    assert "files scanned" in out
    assert "lines skipped" in out


def test_collector_failure_is_fail_soft(env, capsys, monkeypatch):
    """A collector raising must be skipped, not crash the CLI (SPEC.md §11)."""

    def boom(conn, config, pricing):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(
        cli, "_build_collector_registry", lambda: [("claude_code", boom)]
    )

    rc = cli.main(["ingest", *env.overrides])
    assert rc == 0  # CLI still exits cleanly.
    err = capsys.readouterr().err
    assert "claude_code: skipped" in err


# --- not-yet-implemented commands -------------------------------------------

def test_refresh_pricing_message_is_optional_not_phase(capsys):
    """refresh-pricing is OPTIONAL (§4.3): no phase tag, flagged optional."""
    rc = cli.main(["refresh-pricing"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "refresh-pricing" in err
    assert "optional" in err
    assert "4.3" in err
    # It must NOT carry a build-order phase tag anymore.
    assert "Phase 4" not in err


def test_no_command_prints_help(capsys):
    rc = cli.main([])
    assert rc == 2


# --- suggest / status (read-only terminal reports, SPEC.md §9) --------------

import datetime as _dt  # noqa: E402

from core.db import init_db, insert_usage_event  # noqa: E402
from core.pricing import compute_cost  # noqa: E402


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


def _seed_event(conn, pricing, **kw):
    event = {
        "event_uid": kw["uid"],
        "ts": kw["ts"],
        "source": kw.get("source", "claude_code"),
        "project": kw.get("project", "demo"),
        "model": kw.get("model", "claude-opus-4-8"),
        "input_tokens": kw.get("input_tokens", 0),
        "output_tokens": kw.get("output_tokens", 0),
        "cache_creation_tokens": kw.get("cache_creation_tokens", 0),
        "cache_read_tokens": kw.get("cache_read_tokens", 0),
        "is_batch": kw.get("is_batch", 0),
        "cache_ttl": kw.get("cache_ttl"),
        "session_id": "s",
        "raw_meta": None,
    }
    event["cost_usd"] = compute_cost(event, pricing)
    insert_usage_event(conn, event)
    return event


@pytest.fixture
def report_env(tmp_path):
    """A temp DB with a planted downgrade pattern + a budget-crossing.

    Returns argv overrides (config/pricing/db all temp) so the CLI's read-only
    suggest/status commands run entirely against tmp paths -- the real
    data/clauditor.db and config.json are never touched (SPEC.md §11, §12).
    """
    from core.config import load_pricing

    pricing = load_pricing(PRICING_PATH)
    db_path = tmp_path / "clauditor.db"
    conn = init_db(db_path)

    now = _dt.datetime.now(tz=_dt.timezone.utc)

    # Downgrade pattern: 150 Opus calls in 'etl', tiny output, identical inputs
    # (zero input variance) a few days ago -> Rule 1 fires (savings_suggestions).
    recent = now - _dt.timedelta(days=3)
    for i in range(150):
        _seed_event(
            conn, pricing,
            uid=f"dg-{i}", ts=_iso(recent),
            project="etl", model="claude-opus-4-8",
            input_tokens=1000, output_tokens=50,
        )

    # Budget crossing: enough spend TODAY (UTC) to blow the $1 global daily
    # budget set in the temp config below -> status shows >=100% (RED).
    today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    for i in range(30):
        _seed_event(
            conn, pricing,
            uid=f"bd-{i}", ts=_iso(today_noon),
            project="etl", model="claude-opus-4-8",
            input_tokens=100000, output_tokens=10000,
        )
    conn.commit()
    conn.close()

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "lookback_days": 30,
                "budgets": {"global": {"daily": 1}, "projects": {}},
            }
        ),
        encoding="utf-8",
    )

    class Env:
        overrides = [
            "--config", str(config_path),
            "--pricing", str(PRICING_PATH),
            "--db", str(db_path),
        ]

    Env.db_path = db_path
    return Env


def test_suggest_prints_savings(report_env, capsys):
    rc = cli.main(["suggest", *report_env.overrides])
    assert rc == 0
    out = capsys.readouterr().out
    # The planted Opus downgrade pattern produces a suggestion with a $ figure.
    assert "Savings suggestions" in out
    assert "Downgrade" in out
    assert "/mo" in out
    assert "$" in out
    assert "Confidence" in out


def test_suggest_no_data_is_clean(tmp_path, capsys):
    """A fresh (empty) DB yields a clear no-suggestions line, exit 0."""
    db_path = tmp_path / "empty.db"
    init_db(db_path).close()
    rc = cli.main(
        [
            "suggest",
            "--pricing", str(PRICING_PATH),
            "--db", str(db_path),
            "--config", str(tmp_path / "missing-config.json"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No savings suggestions" in out


def test_status_prints_budget_crossing(report_env, capsys):
    rc = cli.main(["status", *report_env.overrides])
    assert rc == 0
    out = capsys.readouterr().out
    assert "spend vs budgets" in out
    # Global daily budget of $1.00 is shown and blown -> RED at >=100%.
    assert "global / daily" in out
    assert "of $1.00" in out
    assert "[RED]" in out


def test_status_no_budgets_is_clean(tmp_path, capsys):
    """No configured budgets -> a clear line, exit 0 (no alerts fired)."""
    db_path = tmp_path / "clauditor.db"
    init_db(db_path).close()
    config_path = tmp_path / "config.json"
    # Null out the global budget so budget_status yields nothing. The default
    # per-project budgets map is now EMPTY (no phantom 'etl-pipeline' is merged
    # in), so leaving 'projects' unset is enough -- nothing to null out there.
    config_path.write_text(
        json.dumps(
            {
                "budgets": {
                    "global": {"daily": None, "weekly": None, "monthly": None},
                    "projects": {},
                }
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "status",
            "--pricing", str(PRICING_PATH),
            "--db", str(db_path),
            "--config", str(config_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No budgets configured" in out


# --- reset (destructive: wipe ONLY the resolved DB, SPEC.md §9, §11) ---------

def _make_db(db_path: Path) -> None:
    """Create a real (empty-schema) SQLite DB at db_path via init_db."""
    init_db(db_path).close()
    assert db_path.exists()


def test_reset_confirm_via_yes_flag_wipes_db(tmp_path, capsys):
    """--yes skips the prompt and removes exactly the resolved --db file."""
    db_path = tmp_path / "clauditor.db"
    _make_db(db_path)

    rc = cli.main(["reset", "--db", str(db_path), "--yes"])
    assert rc == 0
    # The DB file is gone (recreated empty on next ingest/serve).
    assert not db_path.exists()
    out = capsys.readouterr().out
    assert "Reset complete" in out
    assert str(db_path) in out


def test_reset_confirm_via_typed_token_wipes_db(tmp_path, capsys, monkeypatch):
    """Typing the exact token 'reset' on stdin confirms and wipes the DB."""
    db_path = tmp_path / "clauditor.db"
    _make_db(db_path)

    monkeypatch.setattr("builtins.input", lambda _prompt="": "reset")
    rc = cli.main(["reset", "--db", str(db_path)])
    assert rc == 0
    assert not db_path.exists()
    assert "Reset complete" in capsys.readouterr().out


def test_reset_decline_nonmatching_keeps_db_intact(tmp_path, capsys, monkeypatch):
    """A non-matching response aborts: DB stays byte-for-byte intact, exit 0."""
    db_path = tmp_path / "clauditor.db"
    _make_db(db_path)
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime

    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")
    rc = cli.main(["reset", "--db", str(db_path)])
    assert rc == 0  # a deliberate decline is not an error.
    out = capsys.readouterr().out
    assert "aborted" in out.lower()
    assert "nothing was changed" in out.lower()

    # DB is untouched: still present, same bytes, same mtime.
    assert db_path.exists()
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime == before_mtime


def test_reset_decline_on_empty_input(tmp_path, capsys, monkeypatch):
    """An empty line declines (does not confirm); DB intact, exit 0."""
    db_path = tmp_path / "clauditor.db"
    _make_db(db_path)

    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    rc = cli.main(["reset", "--db", str(db_path)])
    assert rc == 0
    assert db_path.exists()
    assert "aborted" in capsys.readouterr().out.lower()


def test_reset_decline_on_eof(tmp_path, capsys, monkeypatch):
    """EOF / Ctrl-D (closed stdin) declines, never confirms; DB intact."""
    db_path = tmp_path / "clauditor.db"
    _make_db(db_path)

    def _eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    rc = cli.main(["reset", "--db", str(db_path)])
    assert rc == 0
    assert db_path.exists()
    assert "aborted" in capsys.readouterr().out.lower()


def test_reset_nothing_to_reset_when_db_missing(tmp_path, capsys):
    """No DB file present -> graceful 'nothing to reset', exit 0, no error."""
    db_path = tmp_path / "absent.db"
    assert not db_path.exists()

    rc = cli.main(["reset", "--db", str(db_path), "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to reset" in out


def test_reset_targets_only_resolved_db_path(tmp_path, monkeypatch):
    """SAFETY (§11): reset removes ONLY the resolved --db file.

    Sibling stand-ins for config.json / pricing.json / a fake ~/.claude file in
    the same temp dir must survive a confirmed wipe untouched, and no path under
    a fake ~/.claude may ever be referenced by the command.
    """
    db_path = tmp_path / "clauditor.db"
    _make_db(db_path)

    # Sibling files that must NOT be touched.
    config_sib = tmp_path / "config.json"
    pricing_sib = tmp_path / "pricing.json"
    config_sib.write_text('{"port": 4747}', encoding="utf-8")
    pricing_sib.write_text('{"updated": "2026-06-20"}', encoding="utf-8")

    fake_claude = tmp_path / "dot-claude"
    (fake_claude / "projects").mkdir(parents=True)
    claude_file = fake_claude / "projects" / "sess.jsonl"
    claude_file.write_text('{"usage": {}}', encoding="utf-8")

    # Force Path.home() to the temp tree so any accidental ~/.claude touch would
    # land here (and be caught) rather than the real home.
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    rc = cli.main(["reset", "--db", str(db_path), "--yes"])
    assert rc == 0

    # Only the DB is gone; every sibling / fake-claude file survives intact.
    assert not db_path.exists()
    assert config_sib.read_text(encoding="utf-8") == '{"port": 4747}'
    assert pricing_sib.read_text(encoding="utf-8") == '{"updated": "2026-06-20"}'
    assert claude_file.read_text(encoding="utf-8") == '{"usage": {}}'
