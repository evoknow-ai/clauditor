"""Tests for the Claude Code collector (SPEC.md §5.1, §11, §12).

Covers:
* Parser against fixture .jsonl lines with varying schema (``message.usage`` vs
  top-level ``usage``; missing cache fields).
* Dedupe: ingest the same fixture twice -> row count unchanged.
* Incremental ingest: append a line -> only the new line is added.
* Skip behavior: malformed/non-JSON lines and lines lacking ``usage`` are
  skipped (counted), not fatal.
* Carried-forward: an unknown model flows through and the row uses fallback
  pricing AND ``raw_meta.unknown_model == true``.

All tests use a tmp projects dir; the real ~/.claude is never touched.
"""

import json
from pathlib import Path

import pytest

from collectors.claude_code import (
    decode_project_name,
    ingest_claude_code,
    make_event_uid,
    parse_line,
    resolve_projects_root,
)
from core.db import event_count, init_db

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"


@pytest.fixture(scope="module")
def pricing():
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    """A fresh, initialized SQLite DB in a tmp dir."""
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def projects(tmp_path):
    """A tmp Claude Code ``projects`` root with a config pointing at it."""
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    config = {"claude_code_path": str(tmp_path / "claude")}
    return root, config


# --- Fixture lines (varying schema) -----------------------------------------

# message.usage shape, full cache fields.
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

# top-level usage shape, missing cache fields.
LINE_TOP_LEVEL_USAGE = json.dumps(
    {
        "ts": "2026-06-20T10:05:00+00:00",
        "model": "claude-sonnet-4-6",
        "id": "msg_bbb",
        "usage": {
            "input_tokens": 300,
            "output_tokens": 50,
        },
    }
)

# A user line with no usage object -> must be skipped.
LINE_NO_USAGE = json.dumps({"type": "user", "message": {"content": "hello"}})

# Not JSON at all -> must be skipped.
LINE_MALFORMED = "{this is not valid json"


def _write_session(root: Path, project_dir: str, name: str, lines):
    proj = root / project_dir
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- Parser unit tests ------------------------------------------------------

def test_parse_message_usage_shape(pricing):
    event = parse_line(
        LINE_MESSAGE_USAGE,
        file_path="/x/sess.jsonl",
        line_number=1,
        session_id="sess",
        project="clauditor",
        mtime_fallback=0.0,
        pricing=pricing,
    )
    assert event is not None
    assert event["model"] == "claude-opus-4-8"
    assert event["input_tokens"] == 1000
    assert event["output_tokens"] == 200
    assert event["cache_creation_tokens"] == 500
    assert event["cache_read_tokens"] == 4000
    assert event["source"] == "claude_code"
    assert event["is_batch"] == 0
    assert event["session_id"] == "sess"
    assert event["project"] == "clauditor"
    assert event["ts"] == "2026-06-20T10:00:00+00:00"
    assert event["raw_meta"] is None
    assert event["cost_usd"] > 0


def test_parse_top_level_usage_missing_cache_fields(pricing):
    event = parse_line(
        LINE_TOP_LEVEL_USAGE,
        file_path="/x/sess.jsonl",
        line_number=2,
        session_id="sess",
        project="clauditor",
        mtime_fallback=0.0,
        pricing=pricing,
    )
    assert event is not None
    assert event["model"] == "claude-sonnet-4-6"
    assert event["input_tokens"] == 300
    assert event["output_tokens"] == 50
    # Missing cache fields default to 0 (SPEC.md §4.2 / §5.1).
    assert event["cache_creation_tokens"] == 0
    assert event["cache_read_tokens"] == 0
    assert event["ts"] == "2026-06-20T10:05:00+00:00"


def test_parse_skips_line_without_usage(pricing):
    assert (
        parse_line(
            LINE_NO_USAGE,
            file_path="/x/sess.jsonl",
            line_number=3,
            session_id="sess",
            project="p",
            mtime_fallback=0.0,
            pricing=pricing,
        )
        is None
    )


def test_parse_skips_malformed_line(pricing):
    assert (
        parse_line(
            LINE_MALFORMED,
            file_path="/x/sess.jsonl",
            line_number=4,
            session_id="sess",
            project="p",
            mtime_fallback=0.0,
            pricing=pricing,
        )
        is None
    )


def test_parse_blank_line_skipped(pricing):
    assert (
        parse_line(
            "   ",
            file_path="/x/sess.jsonl",
            line_number=5,
            session_id="sess",
            project="p",
            mtime_fallback=0.0,
            pricing=pricing,
        )
        is None
    )


def test_timestamp_mtime_fallback(pricing):
    line = json.dumps(
        {"message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 1}}}
    )
    event = parse_line(
        line,
        file_path="/x/sess.jsonl",
        line_number=1,
        session_id="sess",
        project="p",
        mtime_fallback=1_700_000_000.0,
        pricing=pricing,
    )
    assert event is not None
    # mtime fallback produces a UTC ISO timestamp.
    assert event["ts"].startswith("2023-")


# --- Project name decoding --------------------------------------------------

def test_decode_project_name():
    assert decode_project_name("-Users-kabir-projects-clauditor") == "clauditor"
    assert decode_project_name("-Users-kabir-Downloads") == "Downloads"
    assert decode_project_name("plain") == "plain"


def test_resolve_projects_root_override(tmp_path):
    # Override may point at the .claude root...
    root = resolve_projects_root({"claude_code_path": str(tmp_path / "claude")})
    assert root == tmp_path / "claude" / "projects"
    # ...or directly at a projects dir.
    direct = tmp_path / "claude" / "projects"
    assert resolve_projects_root({"claude_code_path": str(direct)}) == direct


# --- Dedupe -----------------------------------------------------------------

def test_event_uid_stable():
    a = make_event_uid("/x/sess.jsonl", 1, "msg_aaa")
    b = make_event_uid("/x/sess.jsonl", 1, "msg_aaa")
    assert a == b
    assert make_event_uid("/x/sess.jsonl", 2, "msg_aaa") != a


def test_dedupe_double_ingest_no_double_count(db, projects, pricing):
    root, config = projects
    _write_session(
        root, "-Users-x-projects-demo", "sess1",
        [LINE_MESSAGE_USAGE, LINE_TOP_LEVEL_USAGE],
    )

    first = ingest_claude_code(db, config, pricing)
    assert first["rows_added"] == 2
    assert event_count(db) == 2

    second = ingest_claude_code(db, config, pricing)
    # Same bytes re-ingested -> mtime unchanged so file is skipped; either way
    # no new rows and the count is unchanged.
    assert second["rows_added"] == 0
    assert event_count(db) == 2


# --- Incremental ingest -----------------------------------------------------

def test_incremental_only_new_line_added(db, projects, pricing):
    root, config = projects
    path = _write_session(
        root, "-Users-x-projects-demo", "sess1", [LINE_MESSAGE_USAGE]
    )

    first = ingest_claude_code(db, config, pricing)
    assert first["rows_added"] == 1
    assert event_count(db) == 1

    # Append a new assistant line and bump mtime so the file is re-read.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(LINE_TOP_LEVEL_USAGE + "\n")
    import os
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))

    second = ingest_claude_code(db, config, pricing)
    assert second["rows_added"] == 1  # only the appended line
    assert event_count(db) == 2


def test_unchanged_mtime_skips_file(db, projects, pricing):
    root, config = projects
    _write_session(root, "-Users-x-projects-demo", "sess1", [LINE_MESSAGE_USAGE])

    ingest_claude_code(db, config, pricing)
    result = ingest_claude_code(db, config, pricing)
    assert result["files_scanned"] == 1
    assert result["rows_added"] == 0


# --- Skip behavior (fail-soft) ----------------------------------------------

def test_malformed_and_no_usage_lines_skipped_not_fatal(db, projects, pricing):
    root, config = projects
    _write_session(
        root,
        "-Users-x-projects-demo",
        "sess1",
        [LINE_MESSAGE_USAGE, LINE_NO_USAGE, LINE_MALFORMED, LINE_TOP_LEVEL_USAGE],
    )

    result = ingest_claude_code(db, config, pricing)
    # Two good usage lines ingested; two lines skipped; no crash.
    assert result["rows_added"] == 2
    assert result["lines_skipped"] == 2
    assert event_count(db) == 2


# --- Unknown model (carried-forward acceptance criterion #1) -----------------

def test_unknown_model_uses_fallback_pricing_and_flags_raw_meta(db, projects, pricing):
    root, config = projects
    unknown_line = json.dumps(
        {
            "timestamp": "2026-06-20T11:00:00+00:00",
            "message": {
                "id": "msg_unknown",
                "model": "claude-future-9-9",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            },
        }
    )
    _write_session(root, "-Users-x-projects-demo", "sess1", [unknown_line])

    result = ingest_claude_code(db, config, pricing)
    assert result["rows_added"] == 1

    row = db.execute(
        "SELECT model, cost_usd, raw_meta FROM usage_events"
    ).fetchone()

    # raw_meta flags the unknown model.
    meta = json.loads(row["raw_meta"])
    assert meta["unknown_model"] is True
    assert meta["reported_model"] == "claude-future-9-9"

    # Cost must use the fallback model (claude-sonnet-4-6: $3 in / $15 out per M).
    # 1M in + 1M out = 3 + 15 = $18.00
    assert row["cost_usd"] == 18.0

    # The reported model string is preserved in the model column (more useful for
    # breakdowns); only the *pricing* falls back, and raw_meta flags it.
    assert row["model"] == "claude-future-9-9"


def test_known_model_has_no_unknown_flag(db, projects, pricing):
    root, config = projects
    _write_session(root, "-Users-x-projects-demo", "sess1", [LINE_MESSAGE_USAGE])
    ingest_claude_code(db, config, pricing)
    row = db.execute("SELECT raw_meta FROM usage_events").fetchone()
    assert row["raw_meta"] is None


# --- No history is a clean no-op --------------------------------------------

def test_missing_projects_dir_is_noop(db, tmp_path, pricing):
    config = {"claude_code_path": str(tmp_path / "does-not-exist")}
    result = ingest_claude_code(db, config, pricing)
    assert result == {"rows_added": 0, "files_scanned": 0, "lines_skipped": 0}
