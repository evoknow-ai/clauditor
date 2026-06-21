"""clauditor HTTP server package (SPEC.md §7).

Exposes a FastAPI app that serves read-only JSON aggregates from the
``usage_events`` SQLite table. The server binds to ``127.0.0.1`` only -- never
``0.0.0.0`` -- because clauditor is a local tool (SPEC.md §7, §11).
"""

from __future__ import annotations

from server.app import build_app, run_server

__all__ = ["build_app", "run_server"]
