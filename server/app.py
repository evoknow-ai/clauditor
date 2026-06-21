"""FastAPI application factory + local server launcher (SPEC.md §7, §11).

The app serves the static single-page dashboard from ``/`` (the ``web/``
directory) and the read-only JSON endpoints under ``/api/*`` (see
:mod:`server.routes`).

Binding policy (NON-NEGOTIABLE, SPEC.md §7 & §11): the server binds to
``127.0.0.1`` only. :func:`run_server` hardcodes the uvicorn host and ignores
any caller-supplied host, so the dashboard can never be exposed off-host.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.db import DEFAULT_DB_PATH
from server.routes import router

# Hardcoded loopback bind address -- the only host the server may listen on.
# Intentionally not a parameter (SPEC.md §7/§11: "bind to 127.0.0.1 only").
LOCALHOST = "127.0.0.1"

# The frontend lives in ``web/`` at the repo root (sibling of this ``server``
# package). Resolved by path so it is found regardless of the process CWD
# (SPEC.md §7: "robust to where the process is launched from").
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def build_app(
    *,
    db_path: str | Path | None = None,
    pricing: Mapping[str, Any] | None = None,
    pricing_updated: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> FastAPI:
    """Construct the FastAPI app and attach request-scoped configuration.

    ``db_path`` is where the read-only endpoints look for ``usage_events``
    (defaults to ``data/clauditor.db``). ``pricing_updated`` (or the ``updated``
    field of ``pricing``) is surfaced by ``/api/health`` so a stale pricing file
    is visible (SPEC.md §4.3, §7).

    ``config`` and ``pricing`` (the full dicts) are attached to ``app.state`` so
    the analyzer-backed read endpoints (``/api/suggestions``, ``/api/alerts``;
    SPEC.md §6, §7) can read budgets, the lookback window, and pricing rates.
    Both fall back to the on-disk defaults when not supplied, so the app still
    boots (and those endpoints still return well-formed empties) without them.
    """
    resolved_db = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    if pricing_updated is None and pricing is not None:
        pricing_updated = pricing.get("updated")

    # Resolve config/pricing for the analyzer endpoints, falling back to the
    # on-disk defaults. Loading must never crash app construction, so a config
    # error degrades to in-code defaults (the endpoints then return empties).
    resolved_config = config
    resolved_pricing = pricing
    if resolved_config is None or resolved_pricing is None:
        try:
            from core.config import load_config, load_pricing

            if resolved_config is None:
                resolved_config = load_config()
            if resolved_pricing is None:
                resolved_pricing = load_pricing()
        except Exception:  # noqa: BLE001 -- analyzer endpoints degrade to empty.
            pass

    app = FastAPI(
        title="clauditor",
        description=(
            "Local-first dashboard for Claude token usage and dollar spend."
        ),
        version="0.1.0",
    )

    # Per-app config consumed by the route handlers via ``request.app.state``.
    app.state.db_path = str(resolved_db)
    app.state.pricing_updated = pricing_updated
    app.state.config = resolved_config
    app.state.pricing = resolved_pricing

    # API routes are registered BEFORE the static mount so ``/api/*`` always
    # wins over the catch-all static handler (SPEC.md §7: JSON from /api/*,
    # static frontend from /).
    app.include_router(router)
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve ``web/index.html`` at ``/`` plus its static assets.

    Uses an explicit ``/`` route for the SPA shell and a ``StaticFiles`` mount
    for the asset files (``main.js``, ``styles.css``). If the ``web/`` directory
    is missing (e.g. a partial install) the app still boots and ``/`` returns a
    clean JSON message rather than crashing on startup.
    """
    index_file = WEB_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def index() -> Any:  # noqa: ANN401 -- FastAPI response.
        if index_file.is_file():
            return FileResponse(index_file)
        return JSONResponse(
            status_code=503,
            content={
                "error": "frontend assets not found",
                "expected_dir": str(WEB_DIR),
            },
        )

    if WEB_DIR.is_dir():
        # ``html=False``: serve the static asset files only. The SPA shell is
        # served by the explicit ``/`` route above; ``/api/*`` is matched first.
        app.mount(
            "/",
            StaticFiles(directory=str(WEB_DIR), html=False),
            name="web",
        )


def run_server(
    *,
    db_path: str | Path | None = None,
    pricing: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    port: int = 4747,
) -> None:
    """Start the dashboard with uvicorn, bound to ``127.0.0.1`` ONLY.

    The host is hardcoded to :data:`LOCALHOST` and is deliberately not exposed as
    a parameter, so there is no code path that can bind the server to a
    non-loopback interface (SPEC.md §7, §11). Default port ``4747`` (overridable
    from config).
    """
    import uvicorn  # imported lazily so importing the app never requires it.

    pricing_updated = pricing.get("updated") if pricing is not None else None
    app = build_app(
        db_path=db_path,
        pricing=pricing,
        pricing_updated=pricing_updated,
        config=config,
    )

    uvicorn.run(app, host=LOCALHOST, port=int(port), log_level="info")
