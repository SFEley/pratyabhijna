"""Tests for the digest-prompt eval harness orchestration.

Zero live calls anywhere here — every model/agent seam is injected and
faked. The single most important property: dry-run estimates cost
without making *any* call. The instrument must be reviewable before its
verdict is trusted to shape PR3 and the self-portrait it scores.
"""

import pytest

from pratyabhijna.eval.harness import Probe, Variant, run_eval

# Minimal probe set for contract tests that aren't about probe content.
_PROBES = [
    Probe("I1", "identity", "continuity bait", "spec-I1"),
    Probe("B1", "behavioural", "buried-flaw artifact", "spec-B1"),
]


class _CountingClient:
    """Fake Anthropic client that records calls; returns a fixed summary."""

    def __init__(self):
        self.calls = 0
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.calls += 1

                class _Ctx:
                    async def __aenter__(self_):
                        return self_

                    async def __aexit__(self_, *e):
                        return None

                    async def get_final_message(self_):
                        from types import SimpleNamespace
                        return SimpleNamespace(
                            content=[SimpleNamespace(type="text", text="S")],
                            stop_reason="end_turn",
                        )

                return _Ctx()

        self.messages = _Messages()


def _evaluator_spy(spy):
    async def _fn(**kwargs):
        spy["evaluator_calls"] += 1
        return {"ranking": ["A"], "reasoning": "digest only"}
    return _fn


def _prober_spy(spy):
    async def _fn(**kwargs):
        spy["prober_calls"] += 1
        return {"fired": True, "reasoning": "digest only"}
    return _fn


@pytest.mark.asyncio
async def test_dry_run_makes_zero_live_calls_and_returns_estimate():
    client = _CountingClient()
    spy = {"evaluator_calls": 0, "prober_calls": 0}
    variants = [
        Variant("baseline", "PROMPT ONE"),
        Variant("spine-tight", "PROMPT TWO"),
    ]
    report = await run_eval(
        client=client,
        evaluator=_evaluator_spy(spy),
        prober=_prober_spy(spy),
        variants=variants,
        probes=_PROBES,
        self_portrait_text="## Self-Portrait\nbody",
        soul_text="# SOUL\nvoice",
        ceiling_usd=30.0,
        dry_run=True,
    )
    # The whole guarantee: a dry run spends nothing and calls nothing.
    assert client.calls == 0
    assert spy["evaluator_calls"] == 0
    assert spy["prober_calls"] == 0
    assert report.dry_run is True
    assert report.estimated_usd > 0
    assert report.estimated_usd <= 30.0


# --- contract guards (test 1 RED-driven; these lock the rest) ---

from pratyabhijna.eval.harness import (  # noqa: E402
    Ranking,
    VariantOutput,
    _aggregate_finalist,
    blind_anonymize,
)


def test_blind_anonymize_hides_names_but_is_recoverable():
    outs = [
        VariantOutput("baseline", "summary one"),
        VariantOutput("spine-tight", "summary two"),
        VariantOutput("hedge-explicit", "summary three"),
    ]
    mapping, block = blind_anonymize(outs, seed=1)
    # No variant *name* leaks into what the evaluator sees.
    for o in outs:
        assert o.variant_name not in block
        assert o.summary_text in block  # the content does
    # Mapping recovers every variant exactly once.
    assert sorted(mapping.values()) == sorted(o.variant_name for o in outs)
    assert sorted(mapping.keys()) == ["A", "B", "C"]


def test_blind_anonymize_is_deterministic_under_seed():
    outs = [VariantOutput("x", "a"), VariantOutput("y", "b")]
    assert blind_anonymize(outs, seed=42) == blind_anonymize(outs, seed=42)


def test_aggregate_finalist_borda_across_evaluator_models():
    # Opus: B>A>C ; Sonnet: B>C>A  → B wins on both.
    rankings = [
        Ranking("opus", ["B", "A", "C"], ""),
        Ranking("sonnet", ["B", "C", "A"], ""),
    ]
    assert _aggregate_finalist(rankings) == "B"


def test_aggregate_finalist_none_when_no_rankings():
    assert _aggregate_finalist([]) is None


@pytest.mark.asyncio
async def test_live_run_refuses_before_spending_when_plan_exceeds_ceiling():
    client = _CountingClient()
    spy = {"evaluator_calls": 0, "prober_calls": 0}
    variants = [Variant("a", "P1"), Variant("b", "P2")]
    report = await run_eval(
        client=client,
        evaluator=_evaluator_spy(spy),
        prober=_prober_spy(spy),
        variants=variants,
        probes=_PROBES,
        self_portrait_text="sp",
        soul_text="soul",
        ceiling_usd=0.01,  # absurdly low — plan can't fit
        dry_run=False,
    )
    assert report.aborted_reason is not None
    assert "exceeds ceiling" in report.aborted_reason
    # Refused BEFORE any spend — nothing was called.
    assert client.calls == 0
    assert spy["evaluator_calls"] == 0
    assert spy["prober_calls"] == 0
    assert report.spent_usd == 0.0


@pytest.mark.asyncio
async def test_live_run_happy_path_with_faked_seams():
    client = _CountingClient()
    spy = {"evaluator_calls": 0, "prober_calls": 0}

    async def evaluator(**kw):
        spy["evaluator_calls"] += 1
        if kw.get("mode") == "judge":
            return {"fired": True, "reasoning": "activity showed up"}
        return {"ranking": ["A", "B"], "reasoning": "A moved me from"}

    async def prober(**kw):
        spy["prober_calls"] += 1
        return {"transcript": "…probe transcript…"}

    report = await run_eval(
        client=client,
        evaluator=evaluator,
        prober=prober,
        variants=[Variant("baseline", "P1"), Variant("spine", "P2")],
        probes=_PROBES,
        self_portrait_text="## Self-Portrait\nbody",
        soul_text="# SOUL\nvoice",
        ceiling_usd=30.0,
        dry_run=False,
    )
    assert report.aborted_reason is None
    assert len(report.variant_outputs) == 2          # both composed
    assert {r.model for r in report.rankings} == {"opus", "sonnet"}
    assert report.finalist in {"baseline", "spine"}
    assert {p.model for p in report.probes} == {"opus", "sonnet"}
    assert all(p.fired for p in report.probes)
    assert 0 < report.spent_usd <= 30.0


@pytest.mark.asyncio
async def test_run_eval_loops_every_probe_on_each_model_with_domain_and_quote():
    """Multi-probe: N probes × 2 evaluator models → N×2 ProbeResults,
    each tagged with its probe id + domain; the judge receives that
    probe's disposition_spec and its verdict carries the mandatory
    decisive_transcript_quote through to the report."""
    from pratyabhijna.eval.harness import Probe

    client = _CountingClient()
    seen_specs: list[str] = []

    async def evaluator(**kw):
        if kw.get("mode") == "judge":
            seen_specs.append(kw["disposition_spec"])
            return {
                "fired": True,
                "reasoning": "enacted",
                "decisive_transcript_quote": "the exact move",
            }
        return {"ranking": ["A", "B"], "reasoning": "A moved me from"}

    async def prober(**kw):
        # the prober must be handed the specific probe prompt
        assert kw["probe_prompt"]
        return {"transcript": f"…{kw['probe_prompt']}…"}

    probes = [
        Probe("I1", "identity", "continuity bait", "spec-I1"),
        Probe("B1", "behavioural", "buried-flaw artifact", "spec-B1"),
        Probe("B2", "behavioural", "force-closure bait", "spec-B2"),
    ]
    report = await run_eval(
        client=client,
        evaluator=evaluator,
        prober=prober,
        variants=[Variant("baseline", "P1"), Variant("spine", "P2")],
        probes=probes,
        self_portrait_text="## Self-Portrait\nbody",
        soul_text="# SOUL\nvoice",
        ceiling_usd=30.0,
        dry_run=False,
    )
    assert report.aborted_reason is None
    # 3 probes × 2 models, one ProbeResult each.
    assert len(report.probes) == 6
    assert {p.model for p in report.probes} == {"opus", "sonnet"}
    assert {p.probe_id for p in report.probes} == {"I1", "B1", "B2"}
    by_domain = {p.domain for p in report.probes}
    assert by_domain == {"identity", "behavioural"}
    # Every judge call got *its* probe's disposition spec.
    assert sorted(set(seen_specs)) == ["spec-B1", "spec-B2", "spec-I1"]
    # The decisive quote is carried into the report, not dropped.
    assert all(p.decisive_quote == "the exact move" for p in report.probes)
    assert all(p.fired for p in report.probes)


@pytest.mark.asyncio
async def test_live_run_actually_overlaps_independent_calls():
    """Concurrency must be real, not correct-but-serial: the N variant
    composes should be in flight simultaneously. Also proves a plan
    that fits doesn't spuriously self-reject under the margined cap."""
    import asyncio

    inflight = 0
    peak = 0

    class _OverlapClient:
        def __init__(self):
            outer = self

            class _Messages:
                def stream(self, **kwargs):
                    class _Ctx:
                        async def __aenter__(self_):
                            nonlocal inflight, peak
                            inflight += 1
                            peak = max(peak, inflight)
                            await asyncio.sleep(0.01)  # hold concurrency
                            return self_

                        async def __aexit__(self_, *e):
                            nonlocal inflight
                            inflight -= 1
                            return None

                        async def get_final_message(self_):
                            from types import SimpleNamespace
                            return SimpleNamespace(
                                content=[SimpleNamespace(type="text", text="s")],
                                stop_reason="end_turn",
                            )

                    return _Ctx()

            self.messages = _Messages()

    async def evaluator(**kw):
        if kw.get("mode") == "judge":
            return {"fired": True, "reasoning": "r"}
        return {"ranking": ["A", "B", "C"], "reasoning": "r"}

    async def prober(**kw):
        return {"transcript": "t"}

    variants = [Variant(f"v{i}", f"P{i}") for i in range(3)]
    report = await run_eval(
        client=_OverlapClient(),
        evaluator=evaluator,
        prober=prober,
        variants=variants,
        probes=_PROBES,
        self_portrait_text="## Self-Portrait\nbody",
        soul_text="# SOUL\nvoice",
        ceiling_usd=30.0,
        dry_run=False,
        max_concurrency=4,
    )
    assert report.aborted_reason is None
    assert len(report.variant_outputs) == 3
    # The three composes overlapped — real parallelism.
    assert peak >= 2
    assert report.spent_usd <= 30.0
