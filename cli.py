"""clauditor command-line entrypoint (SPEC.md §9, build order item 4).

Phase 4 scope: the ``ingest`` subcommand is fully functional end-to-end --
it loads config + pricing, initializes the SQLite DB idempotently, runs every
enabled collector once, and prints rows added per source (SPEC.md §9).

``serve`` (§7), ``suggest`` (§6.2), and ``status`` (§6.1) are also implemented:
``suggest`` and ``status`` open the DB read-only and print analyzer output to
the terminal without starting a server or firing any alert. ``reset`` (Phase 9)
wipes ONLY the resolved clauditor DB file after a typed confirmation (§9).
``refresh-pricing`` (optional, §4.3) remains registered as a clean
"not yet implemented" stub so the CLI surface stays stable.

Safety (SPEC.md §11): this module makes no network calls and binds nothing.
``~/.claude`` is only ever read (the collector opens files read-only). A
collector-level skip or failure is caught so a single bad source never crashes
the whole ingest run. ``reset`` only ever touches the one resolved DB path --
never ``~/.claude``, config.json, pricing.json, or any source file.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any, Callable, Mapping

from core.config import load_all
from core.db import event_count, init_db

# Phase number each not-yet-built command lands in, per SPEC.md §14 build order.
# Surfaced in the "not yet implemented" message so the boundary is explicit.
# ``refresh-pricing`` is intentionally absent: it is OPTIONAL (§4.3) with no
# assigned build-order step, so it gets a bespoke message instead of a phase tag.
# (Now empty: ``reset`` is implemented; ``refresh-pricing`` uses _PENDING_MESSAGE.)
_PENDING_PHASE: dict[str, int] = {}

# Commands with no build-order phase get a custom pending message (keyed by name).
_PENDING_MESSAGE = {
    "refresh-pricing": (
        "clauditor refresh-pricing: optional, not implemented (see SPEC §4.3)."
    ),
}


# --- Collector registry -----------------------------------------------------

# Each entry maps a source name -> a callable
# ``(conn, config, pricing) -> {rows_added, files_scanned?, lines_skipped?}``.
# Structured as a list so additional sources (api, admin_api) drop in later
# without reworking the ingest command (SPEC.md §9: "rows added per source").
def _build_collector_registry() -> list[tuple[str, Callable[..., Mapping[str, Any]]]]:
    from collectors.admin_api import ingest_admin_api
    from collectors.claude_code import ingest_claude_code

    registry: list[tuple[str, Callable[..., Mapping[str, Any]]]] = [
        ("claude_code", ingest_claude_code),
        # OPTIONAL, flag-gated (SPEC.md §5.3). The collector itself respects the
        # gate: with the default config (admin_api.enabled=false) it is a
        # complete no-op -- it reads no key, imports no HTTP client, and makes no
        # network call -- so adding it here cannot break or slow the default
        # ingest path. When disabled it contributes 0 rows.
        ("admin_api", ingest_admin_api),
    ]
    return registry


# --- ingest -----------------------------------------------------------------

def run_ingest(args: argparse.Namespace) -> int:
    """Run all enabled collectors once and print rows added per source.

    Returns a process exit code (0 on success). Loads config + pricing,
    initializes the DB (idempotent), then runs each registered collector,
    isolating failures so one bad source cannot abort the whole run
    (SPEC.md §11 fail-soft).
    """
    config, pricing = load_all(args.config, args.pricing)

    conn = init_db(args.db)
    try:
        before = event_count(conn)

        print("Ingesting usage events...")
        total_added = 0
        for source, collector in _build_collector_registry():
            try:
                result = collector(conn, config, pricing)
            except Exception as exc:  # noqa: BLE001 -- fail-soft per source.
                # A collector-level failure is reported and skipped; it must not
                # crash the CLI or block other sources (SPEC.md §11).
                print(
                    f"  {source}: skipped (error: {exc})",
                    file=sys.stderr,
                )
                continue

            rows_added = int(result.get("rows_added", 0))
            total_added += rows_added
            print(f"  {source}: {rows_added} rows added{_detail_suffix(result)}")

        after = event_count(conn)
        print(
            f"Done. {total_added} new rows "
            f"(database now holds {after} events; was {before})."
        )

        # Budget alerts: evaluate + persist crossings now that fresh data has
        # landed (SPEC.md §6.1, §14 item 8). Fail-soft -- an analyzer error must
        # never break ingest.
        _run_alert_step(conn, config)
    finally:
        conn.close()

    return 0


def _run_alert_step(conn: sqlite3.Connection, config: Mapping[str, Any]) -> None:
    """Evaluate + persist budget alerts after ingest (fail-soft, SPEC.md §6.1).

    Wraps :func:`core.analyzer.analyze_and_fire` so any analyzer/delivery error
    is reported but never aborts the ingest run (SPEC.md §11 fail-soft). Newly
    fired crossings are summarised; already-fired ones are silently skipped by
    the ``alerts_log`` UNIQUE(scope, period, period_key) de-duplication.
    """
    try:
        from core.analyzer import analyze_and_fire

        fired = analyze_and_fire(conn, config)
        if fired:
            print(f"Budget alerts: {len(fired)} new alert(s) fired.")
    except Exception as exc:  # noqa: BLE001 -- analyzer must never break ingest.
        print(f"Budget alert step skipped (error: {exc})", file=sys.stderr)


def _detail_suffix(result: Mapping[str, Any]) -> str:
    """Render the cheap-to-surface extras (files scanned / lines skipped)."""
    bits = []
    if "files_scanned" in result:
        bits.append(f"{int(result['files_scanned'])} files scanned")
    if "lines_skipped" in result:
        bits.append(f"{int(result['lines_skipped'])} lines skipped")
    if not bits:
        return ""
    return " (" + ", ".join(bits) + ")"


# --- serve ------------------------------------------------------------------

def run_serve(args: argparse.Namespace) -> int:
    """Start the local dashboard server (SPEC.md §7, §9, build-order item 6).

    Loads config + pricing, ensures the DB schema exists, optionally runs an
    ingest pass first (when ``ingest_on_serve`` is set), then prints the local
    URL, auto-opens the browser, and starts uvicorn bound to 127.0.0.1 ONLY (the
    host is hardcoded inside ``server.app.run_server`` -- there is no flag to bind
    elsewhere, SPEC.md §11).
    """
    config, pricing = load_all(args.config, args.pricing)

    # Ensure the schema exists so /api/* never hits a missing table on a fresh
    # machine. init_db is idempotent.
    conn = init_db(args.db)

    # Ingest-on-serve (SPEC.md §8/§9): run the collectors once before launching
    # so the dashboard opens on fresh data. Reuses the Phase 4 ingest path and
    # stays fail-soft -- a collector error is reported but never blocks serve.
    if config.get("ingest_on_serve"):
        try:
            print("Running ingest before serve (ingest_on_serve=true)...")
            total = 0
            for source, collector in _build_collector_registry():
                try:
                    result = collector(conn, config, pricing)
                except Exception as exc:  # noqa: BLE001 -- fail-soft per source.
                    print(f"  {source}: skipped (error: {exc})", file=sys.stderr)
                    continue
                added = int(result.get("rows_added", 0))
                total += added
                print(f"  {source}: {added} rows added{_detail_suffix(result)}")
            print(f"Ingest done. {total} new rows.")
            # Persist budget alerts on fresh data before serving (§6.1). Fail-soft.
            _run_alert_step(conn, config)
        finally:
            pass
    conn.close()

    from core.db import DEFAULT_DB_PATH
    from server.app import LOCALHOST, run_server

    db_path = args.db if args.db is not None else str(DEFAULT_DB_PATH)
    port = int(config["port"])
    url = f"http://localhost:{port}"

    print(f"clauditor serving on {url}  (binding {LOCALHOST}; Ctrl-C to stop)")

    # Auto-open the browser (SPEC.md §9, §14 item 6). uvicorn.run blocks, so the
    # open is scheduled on a short-delay background thread -- it fires just after
    # the server starts accepting connections and never hangs the launcher. The
    # stdlib ``webbrowser`` makes no network call of its own.
    _schedule_browser_open(url)

    run_server(db_path=db_path, pricing=pricing, config=config, port=port)
    return 0


def _schedule_browser_open(url: str, *, delay: float = 1.0) -> None:
    """Open ``url`` in the default browser shortly after serve starts.

    Runs on a daemon thread with a small delay so the uvicorn server is listening
    by the time the browser hits ``localhost``. Failures are swallowed -- a
    headless machine (no browser) must not crash ``serve``.
    """
    import threading
    import webbrowser

    def _open() -> None:
        import time

        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 -- opening a browser is best-effort.
            pass

    threading.Thread(target=_open, name="clauditor-open-browser", daemon=True).start()


# --- suggest / status (read-only terminal reports, SPEC.md §9) --------------

def _resolve_db_path(args: argparse.Namespace) -> str:
    """Resolve the DB path the same way ingest/serve do (honors --db override)."""
    from core.db import DEFAULT_DB_PATH

    return args.db if args.db is not None else str(DEFAULT_DB_PATH)


def _open_db_readonly(db_path: str) -> sqlite3.Connection | None:
    """Open the DB read-only, or return None if it is missing/unopenable.

    Mirrors the server's read-only contract (SPEC.md §11): these terminal
    commands never mutate usage data. A fresh machine with no database yet is
    handled fail-soft -- the caller prints an empty/no-data report rather than
    crashing.
    """
    from pathlib import Path

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def run_suggest(args: argparse.Namespace) -> int:
    """Print savings suggestions to the terminal -- no server (SPEC.md §9, §6.2).

    Loads config + pricing, opens the DB read-only, runs the analyzer's savings
    rules over the configured lookback window, and prints each suggestion's
    title, dollar estimate, confidence, and detail. Fail-soft: a missing DB or
    no qualifying pattern prints a clear "no suggestions" line and exits 0.
    """
    config, pricing = load_all(args.config, args.pricing)
    db_path = _resolve_db_path(args)

    conn = _open_db_readonly(db_path)
    if conn is None:
        print("No data yet -- run 'clauditor ingest' first. No suggestions.")
        return 0

    try:
        from core.analyzer import savings_suggestions

        suggestions = savings_suggestions(conn, config, pricing)
    finally:
        conn.close()

    if not suggestions:
        print("No savings suggestions -- usage looks efficient over the window.")
        return 0

    lookback = int(config.get("lookback_days", 30) or 30)
    print(f"Savings suggestions (last {lookback} days):\n")
    for i, s in enumerate(suggestions, start=1):
        savings = float(s.get("estimated_monthly_savings_usd", 0.0))
        confidence = s.get("confidence", "")
        print(f"{i}. {s.get('title', '(untitled)')}")
        print(
            f"   Estimated savings: ${savings:.2f}/mo"
            f"   Confidence: {confidence}"
        )
        print(f"   {s.get('detail', '')}\n")
    return 0


def run_status(args: argparse.Namespace) -> int:
    """Print current-period spend vs budgets (SPEC.md §9, §6.1).

    Loads config + pricing, opens the DB read-only, and uses the analyzer's
    READ-ONLY ``budget_status`` to report each configured budget's value,
    current-period spend, and fraction used (with an amber/red flag at
    80%/100%). This is a pure read -- it NEVER fires alerts or triggers any
    optional delivery (SPEC.md §11). Exits 0.
    """
    config, _pricing = load_all(args.config, args.pricing)
    db_path = _resolve_db_path(args)

    conn = _open_db_readonly(db_path)
    if conn is None:
        print("No data yet -- run 'clauditor ingest' first. No budget status.")
        return 0

    try:
        from core.analyzer import budget_status

        statuses = budget_status(conn, config)
    finally:
        conn.close()

    if not statuses:
        print("No budgets configured. Set 'budgets' in config.json (SPEC §8).")
        return 0

    currency = config.get("currency", "USD")
    print(f"Current-period spend vs budgets ({currency}):\n")
    for s in statuses:
        fraction = s.get("fraction_used")
        level = s.get("level", "ok")
        flag = {"red": " [RED]", "amber": " [AMBER]"}.get(level, "")
        pct = f"{fraction * 100:.0f}%" if fraction is not None else "n/a"
        print(
            f"  {s['scope']} / {s['period']} ({s['period_key']}): "
            f"spend ${s['spend']:.2f} of ${s['budget']:.2f} "
            f"({pct} used){flag}"
        )
    return 0


# --- reset (destructive: wipe ONLY the resolved DB, SPEC.md §9, §11) --------

# The exact word the user must type to confirm a destructive wipe (SPEC.md §9).
RESET_CONFIRM_TOKEN = "reset"


def run_reset(args: argparse.Namespace) -> int:
    """Wipe ONLY the resolved clauditor DB file after a typed confirmation.

    SPEC.md §9: ``reset`` wipes ``data/clauditor.db`` after a typed confirmation.
    SPEC.md §11: this is the ONLY command that deletes anything, and it deletes
    EXACTLY the one DB path resolved via :func:`_resolve_db_path` -- the same
    mechanism ingest/serve/suggest/status use (honors ``--db``; defaults to
    ``data/clauditor.db``). It NEVER touches ``~/.claude``, config.json,
    pricing.json, or any source file.

    Confirmation:
      * Default (interactive): the user must type the exact token
        ``RESET_CONFIRM_TOKEN`` ("reset") on stdin. Anything else -- a wrong
        word, an empty line, or EOF/Ctrl-D -- DECLINES the wipe.
      * ``--yes`` / ``--force``: an explicit, opt-in non-interactive override
        that skips the prompt (for automation/tests). Without it, the typed
        confirmation is always required.

    Exit codes:
      * 0 on a successful wipe (file removed) OR on nothing-to-reset (no DB).
      * 0 on a deliberate decline -- a user choosing not to wipe is NOT an
        error; we print "aborted, nothing was changed" and leave the DB intact.
      * non-zero (1) only if the deletion itself fails (e.g. permissions), with
        a clear error and no partial corruption of unrelated files.
    """
    from pathlib import Path

    db_path = _resolve_db_path(args)
    target = Path(db_path)

    if not getattr(args, "yes", False):
        if not _confirm_reset(db_path):
            print("Reset aborted, nothing was changed.")
            return 0

    # Nothing-to-reset: a fresh machine with no DB yet is handled gracefully.
    if not target.exists():
        print(f"Nothing to reset -- no database at {db_path}.")
        return 0

    try:
        target.unlink()
    except OSError as exc:
        # Fail-soft: a permissions/IO error is reported clearly and exits
        # non-zero. We removed exactly one file, so nothing unrelated is touched.
        print(
            f"Reset failed: could not remove {db_path} ({exc}).",
            file=sys.stderr,
        )
        return 1

    print(
        f"Reset complete -- removed {db_path}. "
        "A fresh, empty database is recreated on the next ingest/serve."
    )
    return 0


def _confirm_reset(db_path: str) -> bool:
    """Prompt on stdin and return True only if the user types the exact token.

    Shows the resolved DB path that will be wiped so the user sees the target
    (SPEC.md §9). An empty line, a non-matching word, or EOF/Ctrl-D all return
    False (decline). Comparison is case-insensitive on the stripped input.
    """
    prompt = (
        f"This will permanently delete the clauditor database at:\n"
        f"  {db_path}\n"
        f"Nothing under ~/.claude or your config/pricing files is touched.\n"
        f"Type '{RESET_CONFIRM_TOKEN}' to confirm: "
    )
    try:
        answer = input(prompt)
    except EOFError:
        # Ctrl-D / closed stdin: treat as a decline, never as confirmation.
        print()  # newline so the abort message starts on its own line.
        return False
    return answer.strip().lower() == RESET_CONFIRM_TOKEN


# --- not-yet-implemented commands -------------------------------------------

def run_pending(args: argparse.Namespace) -> int:
    """Handler for commands registered but not built in this phase.

    Prints a clear message naming the build-order phase and exits cleanly with a
    non-zero code so scripts can detect that nothing happened.
    """
    command = args.command
    custom = _PENDING_MESSAGE.get(command)
    if custom is not None:
        print(custom, file=sys.stderr)
        return 2

    phase = _PENDING_PHASE.get(command)
    where = f" (Phase {phase})" if phase is not None else ""
    print(
        f"clauditor {command}: not yet implemented{where}.",
        file=sys.stderr,
    )
    return 2


# --- argparse wiring --------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse CLI matching the command table in SPEC.md §9."""
    # Path overrides so tests (and power users) can point at non-default
    # config / pricing / database files without touching the real ones. Hung off
    # a shared parent parser so they are accepted both before AND after the
    # subcommand (e.g. ``clauditor ingest --db /tmp/x.db``).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to config.json (default: project-root config.json).",
    )
    common.add_argument(
        "--pricing",
        default=None,
        metavar="PATH",
        help="Path to pricing.json (default: project-root pricing.json).",
    )
    common.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Path to the SQLite database (default: data/clauditor.db).",
    )

    parser = argparse.ArgumentParser(
        prog="clauditor",
        description=(
            "Local-first dashboard for Claude token usage and dollar spend."
        ),
        parents=[common],
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # Fully implemented this phase.
    p_ingest = subparsers.add_parser(
        "ingest",
        parents=[common],
        help="Run all enabled collectors once; print rows added per source.",
    )
    p_ingest.set_defaults(handler=run_ingest)

    # Implemented this phase: the local dashboard server (§7, build-order item 5).
    p_serve = subparsers.add_parser(
        "serve",
        parents=[common],
        help="Start the local dashboard server on 127.0.0.1 (§7).",
    )
    p_serve.set_defaults(handler=run_serve)

    # Read-only terminal reports (SPEC.md §9), powered by the analyzer.
    p_suggest = subparsers.add_parser(
        "suggest",
        parents=[common],
        help="Print savings suggestions to the terminal (§6.2).",
    )
    p_suggest.set_defaults(handler=run_suggest)

    p_status = subparsers.add_parser(
        "status",
        parents=[common],
        help="Print current-period spend vs budgets (§6.1).",
    )
    p_status.set_defaults(handler=run_status)

    # Destructive: wipe ONLY the resolved DB after a typed confirmation (§9).
    p_reset = subparsers.add_parser(
        "reset",
        parents=[common],
        help="Wipe the clauditor database after a typed confirmation (§9).",
    )
    p_reset.add_argument(
        "--yes",
        "--force",
        dest="yes",
        action="store_true",
        help=(
            "Skip the interactive typed confirmation (explicit opt-in for "
            "automation). Without it, you must type 'reset' to confirm."
        ),
    )
    p_reset.set_defaults(handler=run_reset)

    # Registered for a stable surface; optional, not implemented (§4.3).
    pending_help = {
        "refresh-pricing": "Update pricing.json (optional, §4.3).",
    }
    for name, help_text in pending_help.items():
        sub = subparsers.add_parser(name, parents=[common], help=help_text)
        sub.set_defaults(handler=run_pending)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Console-script entrypoint. Parse args, dispatch, return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        # No subcommand given: show usage and signal misuse.
        parser.print_help(sys.stderr)
        return 2

    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
