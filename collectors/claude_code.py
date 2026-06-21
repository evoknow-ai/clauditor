"""Claude Code collector (SPEC.md §5.1).

Parses Claude Code session logs at
``~/.claude/projects/<project-dir>/<session-uuid>.jsonl``. Each ``.jsonl`` file
is one session; each line is a JSON object. Only assistant-message lines carry a
``usage`` object, so most lines are skipped.

Design constraints (SPEC.md §11, non-negotiable):

* ``~/.claude`` is treated as **read-only** -- files are opened ``"r"`` and never
  written to.
* Field extraction is **by key, not position**: the exact schema varies by
  Claude Code version. Lines that don't parse, or that lack a usage object, are
  **skipped and counted**, never fatal (fail-soft).
* Token counts are taken verbatim from the log -- never re-tokenized.
* Dedupe is rigorous: ``event_uid`` is a stable hash of
  ``source + file_path + line_number + message_id`` and rows are inserted via the
  ``INSERT OR IGNORE`` path in :func:`core.db.insert_usage_event`, so re-ingesting
  is safe.
* Incremental: per-file byte ``last_offset`` is stored in ``ingest_state``; the
  file is ``seek()``-ed to it and only new bytes are read. If a file's mtime is
  unchanged since the last successful ingest, the file is skipped entirely.

The public entry point is :func:`ingest_claude_code`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from core.db import insert_usage_event
from core.pricing import compute_cost, is_known_model

SOURCE = "claude_code"


# --- Path resolution --------------------------------------------------------

def resolve_projects_root(config: Mapping[str, Any] | None) -> Path:
    """Return the ``projects`` directory to scan.

    Honors ``config["claude_code_path"]`` when set (it may point either at the
    ``~/.claude`` root or directly at a ``projects`` dir); otherwise defaults to
    ``Path.home() / ".claude" / "projects"`` (SPEC.md §5.1, §8).
    """
    override = None
    if config is not None:
        override = config.get("claude_code_path")

    if override:
        base = Path(override).expanduser()
        # Accept either a `.claude` root or a `projects` dir directly.
        if base.name == "projects":
            return base
        return base / "projects"

    return Path.home() / ".claude" / "projects"


def decode_project_name(project_dir_name: str) -> str:
    """Turn Claude Code's path-encoded directory name into a readable project.

    Claude Code encodes a project's absolute path into a single directory name by
    replacing path separators (and dots) with ``-`` -- e.g.
    ``-Users-kabir-projects-clauditor``. We can't perfectly reverse that
    (the original name may itself contain a hyphen), but the meaningful label is
    the final path segment, so we return the trailing token after the last ``-``.

    Falls back to the raw directory name if decoding yields nothing useful.
    """
    if not project_dir_name:
        return project_dir_name

    # Strip a leading separator-marker, then split on '-'. The last non-empty
    # token is the closest thing to the project/repo directory name.
    stripped = project_dir_name.lstrip("-")
    if not stripped:
        return project_dir_name

    tokens = [t for t in stripped.split("-") if t]
    if not tokens:
        return project_dir_name
    return tokens[-1]


# --- Field extraction (by key, never position) ------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict (the log lines are parsed JSON dicts)."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
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


def _extract_usage(obj: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the usage object, trying ``message.usage`` then top-level ``usage``."""
    message = _get(obj, "message")
    if isinstance(message, Mapping):
        usage = message.get("usage")
        if isinstance(usage, Mapping):
            return usage
    usage = _get(obj, "usage")
    if isinstance(usage, Mapping):
        return usage
    return None


def _extract_model(obj: Mapping[str, Any]) -> str | None:
    """Return the model, trying ``message.model`` then top-level ``model``."""
    message = _get(obj, "message")
    if isinstance(message, Mapping):
        model = message.get("model")
        if isinstance(model, str) and model:
            return model
    model = _get(obj, "model")
    if isinstance(model, str) and model:
        return model
    return None


def _extract_message_id(obj: Mapping[str, Any]) -> str | None:
    """Return a message id if present (``message.id`` then top-level ``id``)."""
    message = _get(obj, "message")
    if isinstance(message, Mapping):
        mid = message.get("id")
        if mid:
            return str(mid)
    mid = _get(obj, "id")
    if mid:
        return str(mid)
    return None


def _normalize_timestamp(raw: Any, mtime_fallback: float) -> str:
    """Return an ISO-8601 UTC timestamp string.

    Tries the log's value (``timestamp`` / ``ts``) first; if absent or
    unparseable, falls back to the file mtime (SPEC.md §5.1 table).
    """
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _dt.datetime.fromtimestamp(raw, tz=_dt.timezone.utc).isoformat()
    return _dt.datetime.fromtimestamp(mtime_fallback, tz=_dt.timezone.utc).isoformat()


def make_event_uid(file_path: str, line_number: int, message_id: str | None) -> str:
    """Build the stable dedupe hash (SPEC.md §5.1).

    Hash over ``source + file_path + line_number + message_id (if present)``.
    The line number anchors the row even when no message id is available.
    """
    parts = [SOURCE, file_path, str(line_number)]
    if message_id:
        parts.append(message_id)
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest


def parse_line(
    raw_line: str,
    *,
    file_path: str,
    line_number: int,
    session_id: str,
    project: str,
    mtime_fallback: float,
    pricing: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Parse one ``.jsonl`` line into a usage-event row dict, or ``None`` to skip.

    Returns ``None`` (caller counts it as skipped) when the line is blank, not
    valid JSON, or carries no usage object. Never raises on bad input.
    """
    text = raw_line.strip()
    if not text:
        return None

    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(obj, Mapping):
        return None

    usage = _extract_usage(obj)
    if usage is None:
        return None

    model = _extract_model(obj)
    message_id = _extract_message_id(obj)
    ts = _normalize_timestamp(
        _get(obj, "timestamp", _get(obj, "ts")), mtime_fallback
    )

    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_creation_tokens = _int(usage.get("cache_creation_input_tokens"))
    cache_read_tokens = _int(usage.get("cache_read_input_tokens"))

    # Model is required by the schema (NOT NULL). If a usage object exists but the
    # model is missing, fall back to the pricing fallback model and flag it.
    unknown_model = not is_known_model(model, pricing)
    effective_model = model if model else pricing["fallback_model"]

    event = {
        "event_uid": make_event_uid(file_path, line_number, message_id),
        "ts": ts,
        "source": SOURCE,
        "project": project,
        "model": effective_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "is_batch": 0,  # Claude Code usage is interactive (SPEC.md §5.1).
        "cache_ttl": None,
        "session_id": session_id,
        "raw_meta": None,
    }

    # Cost is computed against the *reported* model so an unknown model still uses
    # the fallback rates via compute_cost itself (SPEC.md §4.2).
    event["cost_usd"] = compute_cost(
        {**event, "model": model}, pricing
    )

    if unknown_model:
        event["raw_meta"] = json.dumps(
            {"unknown_model": True, "reported_model": model}
        )

    return event


# --- File-level ingest ------------------------------------------------------

def _read_ingest_state(conn: sqlite3.Connection, file_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT last_offset, last_mtime, last_ingested FROM ingest_state "
        "WHERE file_path = ?",
        (file_path,),
    ).fetchone()


def _write_ingest_state(
    conn: sqlite3.Connection,
    file_path: str,
    last_offset: int,
    last_mtime: float,
    last_ingested: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_state (file_path, last_offset, last_mtime, last_ingested)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            last_offset   = excluded.last_offset,
            last_mtime    = excluded.last_mtime,
            last_ingested = excluded.last_ingested
        """,
        (file_path, last_offset, last_mtime, last_ingested),
    )


def ingest_file(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    *,
    pricing: Mapping[str, Any],
) -> dict[str, int]:
    """Ingest a single ``.jsonl`` session file incrementally.

    Returns ``{"rows_added", "lines_skipped", "skipped_file"}`` where
    ``skipped_file`` is ``1`` when the file's mtime was unchanged since the last
    successful ingest (whole file skipped) and ``0`` otherwise.
    """
    file_path = str(jsonl_path)
    try:
        stat = jsonl_path.stat()
    except OSError:
        return {"rows_added": 0, "lines_skipped": 0, "skipped_file": 1}

    mtime = stat.st_mtime
    session_id = jsonl_path.stem
    project = decode_project_name(jsonl_path.parent.name)

    state = _read_ingest_state(conn, file_path)
    start_offset = 0
    if state is not None:
        last_mtime = state["last_mtime"]
        # mtime unchanged since last ingest -> nothing new, skip the file.
        if last_mtime is not None and mtime <= last_mtime:
            return {"rows_added": 0, "lines_skipped": 0, "skipped_file": 1}
        start_offset = int(state["last_offset"] or 0)

    # If the file shrank (rotated/truncated), re-read from the top.
    if start_offset > stat.st_size:
        start_offset = 0

    rows_added = 0
    lines_skipped = 0
    new_offset = start_offset

    # READ-ONLY: open the user's ~/.claude file in read mode only (SPEC.md §11).
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start_offset)
            # Anchor each row to its absolute line number within the file so the
            # event_uid is stable across incremental runs. Count lines preceding
            # the offset without re-parsing them.
            line_number = _count_lines_before(file_path, start_offset)
            for raw_line in fh:
                line_number += 1
                event = parse_line(
                    raw_line,
                    file_path=file_path,
                    line_number=line_number,
                    session_id=session_id,
                    project=project,
                    mtime_fallback=mtime,
                    pricing=pricing,
                )
                if event is None:
                    lines_skipped += 1
                    continue
                if insert_usage_event(conn, event):
                    rows_added += 1
            new_offset = fh.tell()
    except OSError:
        # Could not read the file at all; treat as a no-op skip, don't abort the
        # whole ingest (fail-soft, SPEC.md §11).
        return {"rows_added": 0, "lines_skipped": 0, "skipped_file": 1}

    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    _write_ingest_state(conn, file_path, new_offset, mtime, now)

    return {
        "rows_added": rows_added,
        "lines_skipped": lines_skipped,
        "skipped_file": 0,
    }


def _count_lines_before(file_path: str, offset: int) -> int:
    """Count newline-terminated lines in ``file_path`` up to ``offset`` bytes.

    Used so a row's ``line_number`` (and therefore its ``event_uid``) stays stable
    across incremental runs that resume from a byte offset. Counts in binary to
    stay byte-accurate against the ``seek`` offset.
    """
    if offset <= 0:
        return 0
    count = 0
    remaining = offset
    chunk_size = 65536
    with open(file_path, "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(chunk_size, remaining))
            if not chunk:
                break
            count += chunk.count(b"\n")
            remaining -= len(chunk)
    return count


# --- Top-level entry point --------------------------------------------------

def ingest_claude_code(
    conn: sqlite3.Connection,
    config: Mapping[str, Any] | None,
    pricing: Mapping[str, Any],
) -> dict[str, int]:
    """Scan all Claude Code session logs and ingest new usage events.

    Returns a summary dict::

        {"rows_added": int, "files_scanned": int, "lines_skipped": int}

    ``rows_added`` is the number of *new* (non-duplicate) rows inserted, so the
    Phase-4 CLI can report it. The whole run is committed once at the end; a bad
    line or unreadable file is skipped, never fatal (SPEC.md §11 fail-soft).
    """
    projects_root = resolve_projects_root(config)

    rows_added = 0
    files_scanned = 0
    lines_skipped = 0

    if not projects_root.exists() or not projects_root.is_dir():
        # No Claude Code history on this machine: a clean no-op.
        return {
            "rows_added": 0,
            "files_scanned": 0,
            "lines_skipped": 0,
        }

    for jsonl_path in sorted(projects_root.glob("*/*.jsonl")):
        if not jsonl_path.is_file():
            continue
        files_scanned += 1
        result = ingest_file(conn, jsonl_path, pricing=pricing)
        rows_added += result["rows_added"]
        lines_skipped += result["lines_skipped"]

    conn.commit()

    return {
        "rows_added": rows_added,
        "files_scanned": files_scanned,
        "lines_skipped": lines_skipped,
    }
