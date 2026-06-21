"""Config load + merge tests (SPEC.md §8, §11).

Phase-9 focus: the per-project budgets default must be EMPTY so deep-merging the
user's config onto the in-code defaults never injects a phantom project budget
(no ``etl-pipeline``) into anyone's live config. We assert:

* A user config defining ONLY its own project budget yields a merged
  ``budgets.projects`` whose keys are EXACTLY that user's project -- no
  phantom/example project is merged in.
* The default (no user ``projects``) yields an EMPTY projects map.
* The scalar/global defaults still deep-merge correctly (port, global budget
  periods, alert_fractions) -- _deep_merge is unchanged, only the example
  project was removed.

All tests use TEMP config files; the real config.json is never touched.

Run:
  uv run --with pytest --with fastapi --with httpx --with uvicorn pytest tests/ -q
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import DEFAULT_CONFIG, load_config


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# No phantom project (the Phase-9 fix)
# ---------------------------------------------------------------------------

def test_default_projects_map_is_empty_in_code():
    """The in-code DEFAULT_CONFIG must ship an EMPTY per-project budgets map."""
    assert DEFAULT_CONFIG["budgets"]["projects"] == {}


def test_user_project_budget_does_not_inherit_phantom(tmp_path):
    """A user defining only their own project budget gets EXACTLY that one.

    Regression guard: previously the default ``etl-pipeline`` budget was
    deep-merged onto every user config. With the default projects map empty, the
    merged ``budgets.projects`` keys must equal exactly the user's keys.
    """
    cfg_path = _write(
        tmp_path,
        {"budgets": {"projects": {"my-app": {"monthly": 10}}}},
    )
    merged = load_config(cfg_path)

    assert set(merged["budgets"]["projects"].keys()) == {"my-app"}
    assert "etl-pipeline" not in merged["budgets"]["projects"]
    assert merged["budgets"]["projects"]["my-app"] == {"monthly": 10}


def test_no_user_projects_yields_empty_projects(tmp_path):
    """A user config that omits ``projects`` ends up with an EMPTY projects map."""
    cfg_path = _write(tmp_path, {"budgets": {"global": {"monthly": 100}}})
    merged = load_config(cfg_path)

    assert merged["budgets"]["projects"] == {}


def test_absent_config_file_yields_empty_projects(tmp_path):
    """When no config file exists at all, defaults apply -> empty projects map."""
    merged = load_config(tmp_path / "does-not-exist.json")
    assert merged["budgets"]["projects"] == {}


# ---------------------------------------------------------------------------
# Deep-merge of scalar/global defaults is unchanged
# ---------------------------------------------------------------------------

def test_scalar_and_global_defaults_still_merge(tmp_path):
    """Removing the example project did NOT change the merge for other keys."""
    cfg_path = _write(
        tmp_path,
        {"budgets": {"global": {"monthly": 500}}},
    )
    merged = load_config(cfg_path)

    # User override applied to the one period they set...
    assert merged["budgets"]["global"]["monthly"] == 500
    # ...while the other global periods fall back to the defaults (null).
    assert merged["budgets"]["global"]["daily"] is None
    assert merged["budgets"]["global"]["weekly"] is None
    # Unspecified top-level keys come from defaults.
    assert merged["port"] == 4747
    assert merged["alert_fractions"] == [0.8, 1.0]
    assert merged["lookback_days"] == 30


def test_two_user_projects_preserved_exactly(tmp_path):
    """Multiple user project budgets survive merge with no extra keys."""
    cfg_path = _write(
        tmp_path,
        {
            "budgets": {
                "projects": {
                    "alpha": {"monthly": 20},
                    "beta": {"daily": 1, "weekly": 5},
                }
            }
        },
    )
    merged = load_config(cfg_path)
    assert set(merged["budgets"]["projects"].keys()) == {"alpha", "beta"}
