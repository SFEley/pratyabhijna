"""Tests for the eval-harness cost guardrail (dry-run estimator + cap).

The structural bound that turns "could the eval exceed $30" from a hope
into a guarantee: every planned call is priced before any live spend,
and a running tally aborts hard at the ceiling.
"""

import pytest

from pratyabhijna.eval.cost import (
    PlannedCall,
    SpendCeilingExceeded,
    SpendTracker,
    estimate_cost,
)


def test_uncached_sonnet_call_priced_at_tier_rates():
    # Sonnet tier: $3/M input, $15/M output.
    plan = [PlannedCall(model="sonnet", input_tokens=10_000, output_tokens=1_000)]
    est = estimate_cost(plan)
    # 10000/1e6*3 + 1000/1e6*15 = 0.03 + 0.015
    assert est.total_usd == pytest.approx(0.045)


# Contract guards over the spec-complete guardrail (test 1 RED-driven).


def test_cached_prefix_priced_at_cache_read_rate():
    """The whole reason caching matters: the evaluator's 16k identity
    prefix, repeated every call, billed at 10% after the first."""
    fresh = PlannedCall(model="opus", input_tokens=16_000, output_tokens=500)
    cached = PlannedCall(
        model="opus", input_tokens=16_000, output_tokens=500,
        cached_input_tokens=15_000,
    )
    # 15k of the 16k now at 10% of $15/M instead of full rate.
    assert cached.cost_usd() < fresh.cost_usd()
    saved = fresh.cost_usd() - cached.cost_usd()
    assert saved == pytest.approx(15_000 / 1e6 * 15.0 * 0.9)


def test_cache_write_priced_above_input():
    primer = PlannedCall(
        model="sonnet", input_tokens=10_000, output_tokens=0,
        cache_write_tokens=10_000,
    )
    plain = PlannedCall(model="sonnet", input_tokens=10_000, output_tokens=0)
    assert primer.cost_usd() == pytest.approx(plain.cost_usd() * 1.25)


def test_spend_tracker_aborts_before_the_call_that_would_cross():
    t = SpendTracker(ceiling_usd=30.0)
    t.record(29.50)
    with pytest.raises(SpendCeilingExceeded, match="aborting before the call"):
        t.check(1.00)  # would reach 30.50 > 30 — must not proceed
    # Spend unchanged; the call was never made.
    assert t.spent_usd == pytest.approx(29.50)
    assert t.remaining_usd == pytest.approx(0.50)


def test_spend_tracker_allows_calls_within_ceiling():
    t = SpendTracker(ceiling_usd=30.0)
    t.check(10.0)
    t.record(10.0)
    t.check(5.0)
    t.record(5.0)
    assert t.spent_usd == pytest.approx(15.0)


def test_unknown_model_is_explicit_error():
    with pytest.raises(ValueError, match="Unknown model"):
        PlannedCall(model="gpt-4", input_tokens=1, output_tokens=1).cost_usd()


def test_overlapping_cache_tokens_rejected():
    bad = PlannedCall(
        model="opus", input_tokens=1_000, output_tokens=10,
        cached_input_tokens=800, cache_write_tokens=800,
    )
    with pytest.raises(ValueError, match="exceed input_tokens"):
        bad.cost_usd()


def test_estimate_reports_per_call_breakdown():
    plan = [
        PlannedCall(model="sonnet", input_tokens=1000, output_tokens=100,
                    label="compose-A"),
        PlannedCall(model="opus", input_tokens=2000, output_tokens=200,
                    label="rank-opus"),
    ]
    est = estimate_cost(plan)
    labels = [lbl for lbl, _ in est.per_call]
    assert labels == ["compose-A", "rank-opus"]
    assert est.total_usd == pytest.approx(sum(c for _, c in est.per_call))
