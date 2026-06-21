"""Loads ``config.json`` and ``pricing.json`` (SPEC.md §8 and §4.1).

Defaults are defined in code (:data:`DEFAULT_CONFIG`); any key missing from the
on-disk ``config.json`` falls back to the default. Both files are validated on
load. On malformed config a clear error is printed and the process exits with a
non-zero status (SPEC.md §8).
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_PRICING_PATH = PROJECT_ROOT / "pricing.json"


# --- Defaults (SPEC.md §8) --------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 4747,
    "claude_code_path": None,
    "currency": "USD",
    "lookback_days": 30,
    "ingest_on_serve": True,
    "budgets": {
        "global": {"daily": None, "weekly": None, "monthly": 200},
        # The default per-project budgets map is EMPTY on purpose. Because the
        # user's config is DEEP-merged onto these defaults, any example project
        # left here would be merged into EVERY user's live config as a phantom
        # budget (surfacing in `status`, /api/alerts, and the budget gauges)
        # even when they never configured it. The structure of a per-project
        # budget is documented in README.md and config.example.json instead.
        "projects": {},
    },
    "alert_fractions": [0.8, 1.0],
    "alert_webhook_url": None,
    "desktop_notifications": True,
    # OPTIONAL, org-wide Admin Usage/Cost collector (SPEC.md §5.3). DEFAULT OFF.
    # ``key`` (inline admin key) and ``base_url`` (endpoint override) default to
    # null so the collector stays a complete no-op unless the user opts in. These
    # extra keys are additive -- existing ``enabled``/``key_env`` are unchanged.
    "admin_api": {
        "enabled": False,
        "key_env": "ANTHROPIC_ADMIN_KEY",
        "key": None,
        "base_url": None,
    },
}


class ConfigError(Exception):
    """Raised when a config or pricing file is malformed or invalid."""


# --- Helpers ----------------------------------------------------------------

def _deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overrides`` onto a copy of ``defaults``.

    Missing keys in ``overrides`` fall back to ``defaults``. Nested dicts are
    merged key-by-key; non-dict values (and lists) are replaced wholesale.
    """
    result = deepcopy(defaults)
    for key, value in overrides.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON object file, raising ConfigError on any problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"File not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Expected a JSON object at top level in {path}")
    return data


# --- Validation -------------------------------------------------------------

def _validate_config(config: dict[str, Any]) -> None:
    """Validate a merged config dict, raising ConfigError on problems."""
    port = config.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError(f"'port' must be an integer in 1..65535, got {port!r}")

    lookback = config.get("lookback_days")
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback <= 0:
        raise ConfigError(
            f"'lookback_days' must be a positive integer, got {lookback!r}"
        )

    fractions = config.get("alert_fractions")
    if not isinstance(fractions, list) or not fractions:
        raise ConfigError("'alert_fractions' must be a non-empty list of numbers")
    for frac in fractions:
        if isinstance(frac, bool) or not isinstance(frac, (int, float)) or frac <= 0:
            raise ConfigError(
                f"'alert_fractions' entries must be positive numbers, got {frac!r}"
            )

    budgets = config.get("budgets")
    if not isinstance(budgets, dict):
        raise ConfigError("'budgets' must be an object")

    glob = budgets.get("global", {})
    if not isinstance(glob, dict):
        raise ConfigError("'budgets.global' must be an object")
    _validate_budget_periods("budgets.global", glob)

    projects = budgets.get("projects", {})
    if not isinstance(projects, dict):
        raise ConfigError("'budgets.projects' must be an object")
    for name, periods in projects.items():
        if not isinstance(periods, dict):
            raise ConfigError(f"'budgets.projects.{name}' must be an object")
        _validate_budget_periods(f"budgets.projects.{name}", periods)

    admin = config.get("admin_api")
    if not isinstance(admin, dict):
        raise ConfigError("'admin_api' must be an object")
    if not isinstance(admin.get("enabled"), bool):
        raise ConfigError("'admin_api.enabled' must be a boolean")


def _validate_budget_periods(label: str, periods: dict[str, Any]) -> None:
    """A budget period value must be null or a positive number."""
    for period, value in periods.items():
        if period not in ("daily", "weekly", "monthly"):
            raise ConfigError(
                f"{label}: unknown budget period {period!r} "
                "(expected 'daily', 'weekly', or 'monthly')"
            )
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(
                f"{label}.{period} must be null or a positive number, got {value!r}"
            )


def _validate_pricing(pricing: dict[str, Any]) -> None:
    """Validate the pricing file structure (SPEC.md §4.1)."""
    models = pricing.get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigError("pricing 'models' must be a non-empty object")
    for name, rates in models.items():
        if not isinstance(rates, dict):
            raise ConfigError(f"pricing model {name!r} must be an object")
        for required in ("input", "output"):
            value = rates.get(required)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(
                    f"pricing model {name!r} missing/invalid '{required}' rate"
                )

    modifiers = pricing.get("modifiers")
    if not isinstance(modifiers, dict):
        raise ConfigError("pricing 'modifiers' must be an object")
    for required in (
        "cache_read_multiplier",
        "cache_write_5m_multiplier",
        "cache_write_1h_multiplier",
        "batch_multiplier",
    ):
        value = modifiers.get(required)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"pricing modifier '{required}' missing or invalid")

    fallback = pricing.get("fallback_model")
    if not isinstance(fallback, str) or fallback not in models:
        raise ConfigError(
            "pricing 'fallback_model' must name a model present in 'models'"
        )


# --- Public API -------------------------------------------------------------

def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load, merge with defaults, and validate ``config.json``.

    If the file is absent, the in-code defaults are used. Raises
    :class:`ConfigError` on malformed/invalid content.
    """
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if cfg_path.exists():
        raw = _read_json(cfg_path)
    else:
        raw = {}
    merged = _deep_merge(DEFAULT_CONFIG, raw)
    _validate_config(merged)
    return merged


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate ``pricing.json``.

    Unlike config, pricing has no in-code default table (rates must be shipped
    explicitly), so a missing file is an error. Raises :class:`ConfigError` on
    any problem.
    """
    pricing_path = Path(path) if path is not None else DEFAULT_PRICING_PATH
    data = _read_json(pricing_path)
    _validate_pricing(data)
    return data


def load_all(
    config_path: str | Path | None = None,
    pricing_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load both config and pricing; on error print clearly and exit non-zero.

    This is the entrypoint CLI/server code should use at startup (SPEC.md §8:
    "print a clear error and exit non-zero on malformed config").
    """
    try:
        config = load_config(config_path)
        pricing = load_pricing(pricing_path)
    except ConfigError as exc:
        print(f"clauditor: configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    return config, pricing
