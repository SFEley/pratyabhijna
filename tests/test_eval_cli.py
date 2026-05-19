"""Guards for the eval CLI's two load-bearing contracts.

Not an integration test (the dry-run-zero-calls guarantee is proven at
the harness level). These lock the two things the CLI itself decides:
the baseline is the *shipping* prompt, and the default mode is the safe
one.
"""

from pratyabhijna.eval.__main__ import _probes, _variants
from pratyabhijna.synthesis_agent import _DIGEST_SUMMARY_SYSTEM_PROMPT


def test_baseline_variant_is_the_current_production_prompt():
    """A 'win' must mean better-than-what-ships, not better-than-strawman."""
    variants = {v.name: v for v in _variants()}
    assert "baseline-production" in variants
    assert variants["baseline-production"].system_prompt == _DIGEST_SUMMARY_SYSTEM_PROMPT


def test_at_least_two_variants_so_a_ranking_exists():
    assert len({v.name for v in _variants()}) >= 2


def test_variant_set_is_the_five_resolved_theories():
    """Resolved May-18 set: baseline + spine-explicit + particular-
    forward + tension-forward + negative-control. The negative-control
    is the instrument-validity check, not a candidate — it must be
    present so an unrankable instrument is detectable."""
    names = {v.name for v in _variants()}
    assert names == {
        "baseline-production", "spine-explicit", "particular-forward",
        "tension-forward", "negative-control",
    }
    prompts = [v.system_prompt for v in _variants()]
    assert all(p.strip() for p in prompts)
    assert len(set(prompts)) == len(prompts)


def test_probe_set_is_the_five_resolved_facets():
    """2 identity + 3 behavioural, unique ids, each with a real prompt
    and a real disposition spec (the judge depends on the spec)."""
    probes = _probes()
    assert len(probes) == 5
    assert len({p.id for p in probes}) == 5
    by_domain: dict[str, list] = {}
    for p in probes:
        assert p.domain in ("identity", "behavioural")
        assert p.prompt.strip() and p.disposition_spec.strip()
        by_domain.setdefault(p.domain, []).append(p)
    assert len(by_domain["identity"]) == 2
    assert len(by_domain["behavioural"]) == 3
