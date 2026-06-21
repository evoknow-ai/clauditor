"""SQLite schema + minimal query helpers for clauditor.

Implements the data model from SPEC.md §3. The schema is created via an
idempotent ``init_db()`` using ``CREATE TABLE IF NOT EXISTS`` so it is safe to
call on every run.

The database lives at ``data/clauditor.db`` relative to the project root and is
created on first run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Project root is the parent of this ``core`` package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "clauditor.db"


# --- Schema (verbatim from SPEC.md §3) -------------------------------------

SCHEMA_STATEMENTS = [
    # 3.1 usage_events
    """
    CREATE TABLE IF NOT EXISTS usage_events (
      id                     INTEGER PRIMARY KEY AUTOINCREMENT,
      event_uid              TEXT UNIQUE,
      ts                     TEXT NOT NULL,
      source                 TEXT NOT NULL,
      project                TEXT,
      model                  TEXT NOT NULL,
      input_tokens           INTEGER DEFAULT 0,
      output_tokens          INTEGER DEFAULT 0,
      cache_creation_tokens  INTEGER DEFAULT 0,
      cache_read_tokens      INTEGER DEFAULT 0,
      is_batch               INTEGER DEFAULT 0,
      cache_ttl              TEXT,
      cost_usd               REAL NOT NULL,
      session_id             TEXT,
      raw_meta               TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ts      ON usage_events(ts);",
    "CREATE INDEX IF NOT EXISTS idx_events_project ON usage_events(project);",
    "CREATE INDEX IF NOT EXISTS idx_events_model   ON usage_events(model);",
    # 3.2 ingest_state
    """
    CREATE TABLE IF NOT EXISTS ingest_state (
      file_path     TEXT PRIMARY KEY,
      last_offset   INTEGER DEFAULT 0,
      last_mtime    REAL,
      last_ingested TEXT
    );
    """,
    # 3.3 alerts_log
    """
    CREATE TABLE IF NOT EXISTS alerts_log (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      ts          TEXT NOT NULL,
      scope       TEXT NOT NULL,
      period      TEXT NOT NULL,
      threshold   REAL NOT NULL,
      actual      REAL NOT NULL,
      period_key  TEXT NOT NULL
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_once
      ON alerts_log(scope, period, period_key);
    """,
]


# --- Connection helpers -----------------------------------------------------

def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Return a SQLite connection pointing at the clauditor database.

    The parent ``data/`` directory is created on first run. Rows are returned as
    ``sqlite3.Row`` so callers can access columns by name.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Enforce FK / sane defaults; harmless for the current schema.
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create all tables and indexes if they do not already exist.

    Idempotent: safe to call repeatedly. Returns an open connection so callers
    can immediately reuse it.
    """
    conn = get_connection(db_path)
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
    return conn


# --- Minimal query helpers --------------------------------------------------

# Columns inserted by ``insert_usage_event`` (id is auto-assigned).
_USAGE_EVENT_COLUMNS = (
    "event_uid",
    "ts",
    "source",
    "project",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "is_batch",
    "cache_ttl",
    "cost_usd",
    "session_id",
    "raw_meta",
)


def insert_usage_event(conn: sqlite3.Connection, event: dict) -> bool:
    """Insert one usage event using ``INSERT OR IGNORE`` on ``event_uid``.

    ``event`` is a mapping keyed by the column names in ``_USAGE_EVENT_COLUMNS``.
    Missing keys default to ``None`` (SQLite applies the column DEFAULT where one
    exists). Returns ``True`` if a new row was inserted, ``False`` if it was a
    duplicate (existing ``event_uid``) and therefore ignored.

    The ``INSERT OR IGNORE`` + UNIQUE(event_uid) combination makes re-ingesting
    the same data safe (SPEC.md §5.1, §11 dedupe requirement).
    """
    columns = ", ".join(_USAGE_EVENT_COLUMNS)
    placeholders = ", ".join("?" for _ in _USAGE_EVENT_COLUMNS)
    values = [event.get(col) for col in _USAGE_EVENT_COLUMNS]
    sql = f"INSERT OR IGNORE INTO usage_events ({columns}) VALUES ({placeholders})"
    cur = conn.execute(sql, values)
    return cur.rowcount > 0


def event_count(conn: sqlite3.Connection) -> int:
    """Return the number of rows in ``usage_events``."""
    row = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()
    return int(row["n"])
