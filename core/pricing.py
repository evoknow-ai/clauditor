"""Pricing engine: tokens -> dollars (SPEC.md §4).

All model rates in ``pricing.json`` are expressed **per million tokens**, so the
final cost divides the weighted token sum by 1_000_000.

This module never crashes ingestion: an unknown model falls back to
``pricing["fallback_model"]`` (and the caller is told via the return of
:func:`compute_cost` so it can flag ``raw_meta.unknown_model = true``), and any
missing token field is treated as 0.
"""

from __future__ import annotations

from typing import Any, Mapping


def _get(event: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an event that may be a dict or an attr-style object.

    Duck-typed so the same engine works for collector dicts and SDK-style
    objects (SPEC.md §5.2).
    """
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _int_token(event: Any, key: str) -> int:
    """Return an integer token count, treating missing/None/invalid as 0."""
    value = _get(event, key, 0)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def compute_cost(event: Any, pricing: Mapping[str, Any]) -> float:
    """Compute the USD cost of a single usage event.

    Follows the pseudocode in SPEC.md §4.2 exactly:

    * Unknown model -> use ``pricing["fallback_model"]`` rates.
    * Missing token fields -> treated as 0.
    * ``cache_ttl == "1h"`` uses the 1h cache-write multiplier, otherwise the
      5m multiplier is used.
    * Batch events are multiplied by the batch multiplier last.

    ``event`` may be a dict or an attribute-style object. Returns the cost
    rounded to 6 decimal places.
    """
    models = pricing["models"]
    model = _get(event, "model")

    rates = models.get(model) if model is not None else None
    if rates is None:
        rates = models[pricing["fallback_model"]]

    in_rate = rates["input"]
    out_rate = rates["output"]
    mod = pricing["modifiers"]

    cache_ttl = _get(event, "cache_ttl")
    cache_write_mult = (
        mod["cache_write_1h_multiplier"]
        if cache_ttl == "1h"
        else mod["cache_write_5m_multiplier"]
    )

    input_tokens = _int_token(event, "input_tokens")
    output_tokens = _int_token(event, "output_tokens")
    cache_read_tokens = _int_token(event, "cache_read_tokens")
    cache_creation_tokens = _int_token(event, "cache_creation_tokens")

    cost = (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * in_rate * mod["cache_read_multiplier"]
        + cache_creation_tokens * in_rate * cache_write_mult
    ) / 1_000_000

    if _get(event, "is_batch"):
        cost *= mod["batch_multiplier"]

    return round(cost, 6)


def is_known_model(model: str | None, pricing: Mapping[str, Any]) -> bool:
    """Return True if ``model`` has explicit rates in ``pricing``.

    Lets callers decide whether to set ``raw_meta.unknown_model = true`` after
    pricing an event (SPEC.md §4.2 edge case).
    """
    if model is None:
        return False
    return model in pricing["models"]
