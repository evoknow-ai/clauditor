"""Unit tests for the pricing engine (SPEC.md §4.2, §12 first bullet).

Known token counts -> expected dollar values, covering input/output, cache read,
cache write (5m and 1h), batch, unknown-model fallback, and missing-field cases.

The expected dollars are derived directly from the §4.1 rates:

    claude-opus-4-8   input $5.00/M   output $25.00/M
    claude-sonnet-4-6 input $3.00/M   output $15.00/M   (fallback model)
    cache_read_multiplier      0.10
    cache_write_5m_multiplier  1.25
    cache_write_1h_multiplier  2.00
    batch_multiplier           0.50

Using 1,000,000-token counts makes each per-million rate land on a clean dollar
value, so the arithmetic is verifiable by inspection.
"""

import json
from pathlib import Path

import pytest

from core.pricing import compute_cost, is_known_model

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"

M = 1_000_000  # one million tokens


@pytest.fixture(scope="module")
def pricing():
    """The shipped pricing.json, loaded once per module."""
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


def _event(**kwargs):
    """Build a usage-event dict with the given overrides."""
    base = {
        "model": "claude-opus-4-8",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "is_batch": 0,
        "cache_ttl": None,
    }
    base.update(kwargs)
    return base


# --- Plain input/output -----------------------------------------------------

def test_input_only(pricing):
    # 1M input @ $5.00/M = $5.00
    assert compute_cost(_event(input_tokens=M), pricing) == 5.0


def test_output_only(pricing):
    # 1M output @ $25.00/M = $25.00
    assert compute_cost(_event(output_tokens=M), pricing) == 25.0


def test_input_and_output(pricing):
    # 5 + 25 = 30
    cost = compute_cost(_event(input_tokens=M, output_tokens=M), pricing)
    assert cost == 30.0


def test_sub_million_counts(pricing):
    # 200k input @ $5/M = $1.00 ; 40k output @ $25/M = $1.00
    cost = compute_cost(_event(input_tokens=200_000, output_tokens=40_000), pricing)
    assert cost == 2.0


# --- Cache read -------------------------------------------------------------

def test_cache_read(pricing):
    # 1M cache read @ input_rate(5) * 0.10 = $0.50
    cost = compute_cost(_event(cache_read_tokens=M), pricing)
    assert cost == 0.5


# --- Cache write (5m vs 1h) -------------------------------------------------

def test_cache_write_5m_default_ttl(pricing):
    # No cache_ttl -> 5m multiplier: 5 * 1.25 = $6.25
    cost = compute_cost(_event(cache_creation_tokens=M), pricing)
    assert cost == 6.25


def test_cache_write_5m_explicit(pricing):
    cost = compute_cost(
        _event(cache_creation_tokens=M, cache_ttl="5m"), pricing
    )
    assert cost == 6.25


def test_cache_write_1h(pricing):
    # 1h multiplier: 5 * 2.00 = $10.00
    cost = compute_cost(
        _event(cache_creation_tokens=M, cache_ttl="1h"), pricing
    )
    assert cost == 10.0


# --- Batch ------------------------------------------------------------------

def test_batch_halves_cost(pricing):
    # (5 + 25) * 0.50 = $15.00
    cost = compute_cost(
        _event(input_tokens=M, output_tokens=M, is_batch=1), pricing
    )
    assert cost == 15.0


def test_batch_applies_to_cache_components(pricing):
    # cache read 0.5 + cache write 6.25 = 6.75 ; * 0.50 = 3.375
    cost = compute_cost(
        _event(cache_read_tokens=M, cache_creation_tokens=M, is_batch=1),
        pricing,
    )
    assert cost == 3.375


# --- Unknown model ----------------------------------------------------------

def test_unknown_model_uses_fallback(pricing):
    # Fallback = claude-sonnet-4-6 (3/15): 1M in + 1M out = 3 + 15 = $18.00
    cost = compute_cost(
        _event(model="totally-made-up-model", input_tokens=M, output_tokens=M),
        pricing,
    )
    assert cost == 18.0


def test_unknown_model_flag_helper(pricing):
    assert is_known_model("claude-opus-4-8", pricing) is True
    assert is_known_model("totally-made-up-model", pricing) is False
    assert is_known_model(None, pricing) is False


def test_none_model_uses_fallback_and_does_not_crash(pricing):
    cost = compute_cost(_event(model=None, input_tokens=M), pricing)
    # Fallback sonnet input 3.0/M -> $3.00
    assert cost == 3.0


# --- Missing / malformed token fields ---------------------------------------

def test_missing_token_fields_treated_as_zero(pricing):
    # Only a model is supplied; all token fields absent -> $0.00, no crash.
    assert compute_cost({"model": "claude-opus-4-8"}, pricing) == 0.0


def test_none_token_fields_treated_as_zero(pricing):
    event = _event(input_tokens=None, output_tokens=None,
                   cache_read_tokens=None, cache_creation_tokens=None)
    assert compute_cost(event, pricing) == 0.0


def test_empty_event_does_not_crash(pricing):
    # No model, no tokens: fallback model, zero tokens -> $0.00
    assert compute_cost({}, pricing) == 0.0


# --- Combined realistic event -----------------------------------------------

def test_combined_event(pricing):
    # opus-4-8: in 100k=$0.50, out 20k=$0.50, cache_read 1M=$0.50,
    # cache_write 5m 400k = 5 * 1.25 * 0.4 = $2.50 ; total $4.00
    event = _event(
        input_tokens=100_000,
        output_tokens=20_000,
        cache_read_tokens=M,
        cache_creation_tokens=400_000,
    )
    assert compute_cost(event, pricing) == 4.0


# --- Attribute-style (SDK-like) objects -------------------------------------

def test_works_with_attribute_objects(pricing):
    class Evt:
        model = "claude-opus-4-8"
        input_tokens = M
        output_tokens = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        is_batch = 0
        cache_ttl = None

    assert compute_cost(Evt(), pricing) == 5.0


# --- Rounding ---------------------------------------------------------------

def test_result_is_rounded_to_six_places(pricing):
    # 1 input token @ $5/M = 0.000005 exactly (already 6 dp)
    assert compute_cost(_event(input_tokens=1), pricing) == 0.000005
    # 7 output tokens @ $25/M = 0.000175
    assert compute_cost(_event(output_tokens=7), pricing) == 0.000175
