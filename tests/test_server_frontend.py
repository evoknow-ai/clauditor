"""Phase 6 server tests (SPEC.md §7, §10, §11; build-order item 6).

Covers two things added in Phase 6:

* Static SPA serving -- ``GET /`` returns the dashboard HTML and the static
  assets (``main.js``, ``styles.css``) are reachable, while ``/api/*`` keeps
  working exactly as before.
* Missing-DB hardening (CARRIED-FORWARD ITEM 1) -- pointing the app at a
  non-existent DB file must make the read endpoints return 200 well-formed zeros
  (NOT a 500/traceback), and ``/api/health`` report a sane degraded shape.

Run:
  uv run --with pytest --with fastapi --with httpx --with uvicorn pytest tests/ -q
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.app import WEB_DIR, build_app

PRICING_UPDATED = "2026-06-20"


# --- Static frontend serving (SPEC.md §7, §10) ------------------------------

def test_index_served_at_root_is_html():
    """GET / returns the SPA shell as HTML (200)."""
    client = TestClient(build_app(pricing_updated=PRICING_UPDATED))
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "<!DOCTYPE html>" in body
    # Sanity: it is the clauditor dashboard, loading its own assets.
    assert "/main.js" in body
    assert "/styles.css" in body


def test_static_assets_reachable():
    """main.js and styles.css are served as static files."""
    client = TestClient(build_app(pricing_updated=PRICING_UPDATED))

    js = client.get("/main.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]

    css = client.get("/styles.css")
    assert css.status_code == 200
    assert "css" in css.headers["content-type"]


def test_web_assets_exist_on_disk():
    """The shipped web/ directory has the three SPA files (servable build)."""
    for name in ("index.html", "main.js", "styles.css"):
        assert (WEB_DIR / name).is_file(), f"missing web asset: {name}"


def test_api_still_works_alongside_static(tmp_path):
    """Mounting the static site does not shadow /api/* (SPEC.md §7)."""
    from core.db import init_db

    db_path = tmp_path / "clauditor.db"
    init_db(db_path).close()
    client = TestClient(build_app(db_path=db_path, pricing_updated=PRICING_UPDATED))

    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.get("/api/summary")
    assert r.status_code == 200
    assert r.json()["call_count"] == 0


# --- Missing-DB hardening (CARRIED-FORWARD ITEM 1) --------------------------

def _missing_db_client(tmp_path) -> TestClient:
    """An app pointed at a DB path that does not exist on disk."""
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()
    return TestClient(build_app(db_path=missing, pricing_updated=PRICING_UPDATED))


def test_summary_missing_db_returns_zeros_not_500(tmp_path):
    client = _missing_db_client(tmp_path)
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["call_count"] == 0
    assert body["total_spend_usd"] == 0.0
    assert body["total_tokens"] == 0
    assert body["cache_efficiency"] == 0.0
    assert body["tokens"] == {
        "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0,
    }


def test_timeseries_missing_db_returns_empty_series_not_500(tmp_path):
    client = _missing_db_client(tmp_path)
    r = client.get("/api/timeseries")
    assert r.status_code == 200
    body = r.json()
    assert body["series"] == []
    assert body["granularity"] == "day"


def test_breakdown_missing_db_returns_empty_groups_not_500(tmp_path):
    client = _missing_db_client(tmp_path)
    r = client.get("/api/breakdown", params={"by": "model"})
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == []
    assert body["by"] == "model"


def test_health_missing_db_is_degraded_with_sane_shape(tmp_path):
    client = _missing_db_client(tmp_path)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"status", "db_path", "event_count", "pricing_updated"}
    assert body["status"] == "degraded"
    assert body["event_count"] == 0
    assert body["pricing_updated"] == PRICING_UPDATED


def test_missing_db_still_4xx_on_bad_input(tmp_path):
    """Bad input is still a clean 400, even with a missing DB (no 500)."""
    client = _missing_db_client(tmp_path)
    r = client.get("/api/summary", params={"from": "not-a-date"})
    assert r.status_code == 400
    assert "error" in r.json()


# --- Loading-skeleton lifecycle (SPEC.md §10 progressive render) -------------
#
# Visual defect: the chart-panel "Loading..." placeholder stayed painted on top
# of the rendered bars. Root cause: `.placeholder { display: flex }` in the CSS
# outranked the UA `[hidden] { display: none }` rule, so `placeholder.hidden =
# true` in the render code had no visual effect. These tests assert the fix --
# the CSS now hides `.placeholder[hidden]`, and every render path (success,
# empty, error) clears the placeholder per-panel -- so the reviewer can run an
# automated check of the loading-element lifecycle without a JS/DOM harness.

def _css_text() -> str:
    return (WEB_DIR / "styles.css").read_text(encoding="utf-8")


def _js_text() -> str:
    return (WEB_DIR / "main.js").read_text(encoding="utf-8")


def test_css_hides_placeholder_when_hidden_attr_set():
    """The CSS must let the `hidden` attribute actually hide the placeholder.

    Without an explicit `.placeholder[hidden] { display: none }`, the
    `.placeholder { display: flex }` rule keeps the skeleton painted over the
    chart even after `placeholder.hidden = true`.
    """
    css = _css_text()
    # Some `.placeholder[hidden]` selector must set display:none.
    assert "[hidden]" in css
    import re
    m = re.search(r"\.placeholder\[hidden\]\s*\{([^}]*)\}", css)
    assert m is not None, ".placeholder[hidden] rule is missing"
    rule = m.group(1).replace(" ", "")
    assert "display:none" in rule


def test_render_paths_clear_chart_placeholders():
    """Each chart panel clears its own skeleton in success/empty/error paths.

    We assert that both `loadTimeseries` and `loadBreakdown`:
      * re-arm the loading state at the start of a (re)fetch, and
      * hide the placeholder once data lands and in the catch (error) path.
    """
    js = _js_text()
    for fn in ("loadTimeseries", "loadBreakdown"):
        start = js.index("function " + fn)
        # Slice to the next top-level `function ` declaration so we only inspect
        # this render function's body.
        body = js[start:]
        next_fn = body.index("\n  function ", 1)
        body = body[:next_fn]
        # Re-arms loading on (re)fetch.
        assert "placeholder.hidden = false" in body, f"{fn} never re-arms loading"
        # Clears loading once data lands / on error. There must be at least the
        # success-path clear and the catch-path clear (>= 2 occurrences).
        assert body.count("placeholder.hidden = true") >= 2, (
            f"{fn} does not clear its placeholder in both success and error paths"
        )
        # Error path must show a message, not leave the spinner / be silent.
        assert "empty.hidden = false" in body, f"{fn} error/empty path shows no note"
