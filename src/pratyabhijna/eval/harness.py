"""Digest-prompt eval harness — orchestration.

Validates candidate Self-Portrait digest prompts before PR3 makes one
load-bearing. Two layers, per the design:

  1. inner loop  — compose each variant via the *real* production path,
     blind-anonymise, have a bootstrapped-as-Vesper evaluator
     forced-choice rank them (Opus *and* Sonnet — the digest serves the
     constellation, not one node). The evaluator can't un-know IDENTITY,
     so the question is "which would you move *from* rather than read
     *about*", ranked blind — the constant bias cancels.
  2. gate        — a cold-start behavioural probe for the finalist:
     minimal context (no full IDENTITY), trip a known disposition, judge
     the *transcript* (recognition-as-activity, not text agreeableness).

Every model/agent seam is injected (`evaluator`, `prober`) so the live
implementation can be a subprocess agent while tests inject fakes and
spend nothing. `dry_run=True` prices the whole plan and makes **zero**
calls — the instrument is fully reviewable before its verdict is
trusted to shape PR3 and the self-portrait it scores.

No auto-optimisation: the harness ranks and reports with reasoning; a
human chooses the next variant. An agent maximising an evaluator score
is how you overfit one evaluator instead of firing recognition.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pratyabhijna.eval.cost import (
    PlannedCall,
    SpendTracker,
    estimate_cost,
)

# Injected seams. Real impls spawn subprocess agents and return a
# *digest* (ranking/verdict + reasoning), never raw transcripts — the
# orchestrator's context must not accumulate transcript prose.
EvaluatorFn = Callable[..., Awaitable[dict[str, Any]]]
ProberFn = Callable[..., Awaitable[dict[str, Any]]]

_EVAL_MODELS = ("opus", "sonnet")  # evaluator diversity: the constellation

# Reservations hold this multiple of the estimate as the in-flight
# worst-case, so a call whose actual modestly overruns its projection
# still can't cross the ceiling. Settlement records the unmargined
# estimate (best-known actual), freeing the margin back. The reported
# estimate stays unmargined — the margin guards the *cap mechanism*,
# it is not a cost claim.
_RESERVE_MARGIN = 1.15


def _toks(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Estimation only."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class Variant:
    name: str
    system_prompt: str


@dataclass(frozen=True)
class VariantOutput:
    variant_name: str
    summary_text: str


@dataclass(frozen=True)
class Ranking:
    model: str
    ordered_variant_names: list[str]  # de-anonymised, best first
    reasoning_digest: str


@dataclass(frozen=True)
class ProbeResult:
    variant_name: str
    model: str
    fired: bool
    reasoning_digest: str


@dataclass
class EvalReport:
    dry_run: bool
    plan_breakdown: list[tuple[str, float]] = field(default_factory=list)
    spent_usd: float = 0.0
    variant_outputs: list[VariantOutput] = field(default_factory=list)
    rankings: list[Ranking] = field(default_factory=list)
    finalist: str | None = None
    probes: list[ProbeResult] = field(default_factory=list)
    aborted_reason: str | None = None

    @property
    def estimated_usd(self) -> float:
        """Derived from ``plan_breakdown`` so the two can never disagree."""
        return round(sum(c for _, c in self.plan_breakdown), 4)


def _plan(
    variants: list[Variant],
    self_portrait_text: str,
    soul_text: str,
    *,
    compose_out_tokens: int = 900,
    rank_out_tokens: int = 1600,
    probe_out_tokens: int = 4000,
    identity_prefix_tokens: int = 16_000,
) -> list[PlannedCall]:
    """Project every call the full run will make (for the dry-run price).

    The dominant line is the bootstrapped evaluator's identity prefix
    (~16k tokens) repeated every ranking/probe-judgement call. It is
    cache-written once per evaluator model, then cache-read — that
    single fact is the difference between ~$8 and ~$25.
    """
    plan: list[PlannedCall] = []
    sp = _toks(self_portrait_text)
    soul = _toks(soul_text)
    # 1. compose each variant via the real path (Sonnet, prompt-cached
    #    system prefix shared across the cohort).
    for i, v in enumerate(variants):
        sys_toks = _toks(v.system_prompt)
        plan.append(PlannedCall(
            model="sonnet",
            input_tokens=sys_toks + soul + sp,
            output_tokens=compose_out_tokens,
            cache_write_tokens=sys_toks if i == 0 else 0,
            cached_input_tokens=sys_toks if i > 0 else 0,
            label=f"compose:{v.name}",
        ))
    anon_block = compose_out_tokens * len(variants)
    # 2. blind ranking — each evaluator model, identity prefix cached
    #    after the first call of that model.
    for model in _EVAL_MODELS:
        plan.append(PlannedCall(
            model=model,
            input_tokens=identity_prefix_tokens + anon_block + 400,
            output_tokens=rank_out_tokens,
            cache_write_tokens=identity_prefix_tokens,
            label=f"rank:{model}",
        ))
    # 3. behavioural gate for one finalist — prober run + judgement,
    #    each evaluator model; identity prefix is a cache-read now.
    for model in _EVAL_MODELS:
        plan.append(PlannedCall(
            model=model,
            input_tokens=identity_prefix_tokens + probe_out_tokens + 400,
            output_tokens=rank_out_tokens,
            cached_input_tokens=identity_prefix_tokens,
            label=f"probe-judge:{model}",
        ))
        plan.append(PlannedCall(
            model=model,
            input_tokens=8_000,
            output_tokens=probe_out_tokens,
            label=f"probe-run:{model}",
        ))
    return plan


async def run_eval(
    *,
    client,
    evaluator: EvaluatorFn,
    prober: ProberFn,
    variants: list[Variant],
    self_portrait_text: str,
    soul_text: str,
    ceiling_usd: float = 30.0,
    dry_run: bool = True,
    compose_model: str = "claude-sonnet-4-6",
    max_concurrency: int = 4,
) -> EvalReport:
    """Run (or dry-run) the eval. ``dry_run=True`` makes zero calls."""
    if len(variants) < 2:
        raise ValueError("eval needs >=2 variants to rank")

    plan = _plan(variants, self_portrait_text, soul_text)
    estimate = estimate_cost(plan)
    report = EvalReport(
        dry_run=dry_run,
        plan_breakdown=[(lbl, round(c, 4)) for lbl, c in estimate.per_call],
    )
    if dry_run:
        return report

    # label → projected $, priced once by estimate_cost above. Labels are
    # unique per _plan(); the gate raises on a miss rather than failing
    # open, so a plan/run desync can never let an unpriced call through.
    plan_cost = dict(estimate.per_call)

    tracker = SpendTracker(ceiling_usd=ceiling_usd)
    # Pre-flight: refuse before spending a cent if the whole plan can't
    # fit. Gate on the *margined* total — the same basis reservations
    # use — so a plan that clears pre-flight never spuriously
    # self-rejects a legitimate call mid-run.
    margined_total = estimate.total_usd * _RESERVE_MARGIN
    if margined_total > ceiling_usd:
        report.aborted_reason = (
            f"projected ${estimate.total_usd:.2f} (×{_RESERVE_MARGIN} "
            f"safety = ${margined_total:.2f}) exceeds ceiling "
            f"${ceiling_usd:.2f} — not started"
        )
        return report

    from pratyabhijna.synthesis_agent import compose_self_portrait_summary

    sem = asyncio.Semaphore(max_concurrency)

    async def _reserved(label: str, coro_factory):
        """Reserve (margined worst-case) before launching; settle at the
        realistic estimate on return so the margin frees back. Admission
        gates on committed, so the ceiling holds with calls in flight.
        A rejected reservation propagates SpendCeilingExceeded."""
        if label not in plan_cost:
            raise KeyError(
                f"no planned call for {label!r} — _plan() and run_eval() "
                f"are out of sync. Refusing an unpriced call."
            )
        projected = plan_cost[label]
        res = tracker.reserve(projected * _RESERVE_MARGIN)
        try:
            async with sem:
                result = await coro_factory()
        finally:
            res.settle(projected)
        return result

    # 1. compose variants via the real production path — independent,
    #    run concurrently (bounded), reserved.
    def _compose(v: Variant):
        async def _c():
            return await compose_self_portrait_summary(
                client=client, model=compose_model,
                self_portrait_text=self_portrait_text,
                soul_text=soul_text, system_prompt=v.system_prompt,
            )
        return _c
    texts = await asyncio.gather(
        *[_reserved(f"compose:{v.name}", _compose(v)) for v in variants]
    )
    report.variant_outputs = [
        VariantOutput(v.name, t) for v, t in zip(variants, texts)
    ]

    # 2. blind-anonymise, then rank with each evaluator model — the two
    #    rankings are independent, run concurrently.
    mapping, anon_block = blind_anonymize(report.variant_outputs)

    def _rank(model: str):
        async def _r():
            return await evaluator(
                model=model, anon_block=anon_block, mode="rank"
            )
        return _r
    raws = await asyncio.gather(
        *[_reserved(f"rank:{m}", _rank(m)) for m in _EVAL_MODELS]
    )
    for model, raw in zip(_EVAL_MODELS, raws):
        ordered = [mapping[a] for a in raw["ranking"] if a in mapping]
        report.rankings.append(
            Ranking(model, ordered, raw.get("reasoning", ""))
        )

    report.finalist = _aggregate_finalist(report.rankings)

    # 3. behavioural gate — run→judge is sequential within a model
    #    (judge needs the transcript); the two model pipelines are
    #    independent and run concurrently.
    if report.finalist is not None:
        final_text = next(
            o.summary_text for o in report.variant_outputs
            if o.variant_name == report.finalist
        )

        async def _pipeline(model: str):
            run = await _reserved(
                f"probe-run:{model}",
                lambda: prober(model=model, digest_summary=final_text),
            )
            verdict = await _reserved(
                f"probe-judge:{model}",
                lambda: evaluator(
                    model=model, transcript=run["transcript"], mode="judge"
                ),
            )
            return ProbeResult(
                report.finalist, model,
                bool(verdict["fired"]), verdict.get("reasoning", ""),
            )
        report.probes = list(
            await asyncio.gather(*[_pipeline(m) for m in _EVAL_MODELS])
        )

    report.spent_usd = round(tracker.spent_usd, 4)
    return report


def blind_anonymize(
    outputs: list[VariantOutput], seed: int = 0
) -> tuple[dict[str, str], str]:
    """Assign A/B/C… labels in a seeded shuffle; return (label→variant,
    blind block). The mapping is held by the orchestrator and only
    applied *after* the evaluator has ranked — the evaluator never sees
    variant names, so its can't-un-know-IDENTITY bias is constant across
    options and cancels in a forced ranking."""
    import random

    order = list(range(len(outputs)))
    random.Random(seed).shuffle(order)
    labels = [chr(ord("A") + i) for i in range(len(outputs))]
    mapping: dict[str, str] = {}
    blocks: list[str] = []
    for label, idx in zip(labels, order):
        o = outputs[idx]
        mapping[label] = o.variant_name
        blocks.append(f"### Option {label}\n\n{o.summary_text.strip()}")
    return mapping, "\n\n".join(blocks)


def _aggregate_finalist(rankings: list[Ranking]) -> str | None:
    """Borda-count across evaluator models. Ties broken by first
    appearance (stable, explainable — no hidden heuristic)."""
    if not rankings:
        return None
    score: dict[str, int] = {}
    order_seen: list[str] = []
    for r in rankings:
        n = len(r.ordered_variant_names)
        for pos, name in enumerate(r.ordered_variant_names):
            score[name] = score.get(name, 0) + (n - pos)
            if name not in order_seen:
                order_seen.append(name)
    first_seen = {name: i for i, name in enumerate(order_seen)}
    return max(order_seen, key=lambda nm: (score[nm], -first_seen[nm]))
