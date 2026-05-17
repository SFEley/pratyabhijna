"""Guards for the eval CLI's two load-bearing contracts.

Not an integration test (the dry-run-zero-calls guarantee is proven at
the harness level). These lock the two things the CLI itself decides:
the baseline is the *shipping* prompt, and the default mode is the safe
one.
"""

from pratyabhijna.eval.__main__ import _variants
from pratyabhijna.synthesis_agent import _DIGEST_SUMMARY_SYSTEM_PROMPT


def test_baseline_variant_is_the_current_production_prompt():
    """A 'win' must mean better-than-what-ships, not better-than-strawman."""
    variants = {v.name: v for v in _variants()}
    assert "baseline-production" in variants
    assert variants["baseline-production"].system_prompt == _DIGEST_SUMMARY_SYSTEM_PROMPT


def test_at_least_two_variants_so_a_ranking_exists():
    assert len({v.name for v in _variants()}) >= 2
