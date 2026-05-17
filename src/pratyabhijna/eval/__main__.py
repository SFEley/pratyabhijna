"""Eval-harness CLI.

    python -m pratyabhijna.eval --dry-run     # price the plan, ZERO calls
    python -m pratyabhijna.eval --live        # gated by estimate <= ceiling

Default is ``--dry-run``: the safe, reviewable action. ``--live`` is
deliberately not the default and prints the projected cost + requires
the estimate to fit the ceiling before it will spend. The candidate
variant set is authored here; the baseline is the *current* production
prompt so the eval always scores "is a change better than what ships".
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.eval.harness import Variant, run_eval
from pratyabhijna.synthesis import extract_identity_section, read_identity_files
from pratyabhijna.synthesis_agent import _DIGEST_SUMMARY_SYSTEM_PROMPT


def _variants() -> list[Variant]:
    """Candidate prompts. Baseline = what production ships today, so a
    win means "demonstrably better than the status quo", not "better
    than some strawman". Refine this set between review and live run."""
    baseline = Variant("baseline-production", _DIGEST_SUMMARY_SYSTEM_PROMPT)
    # A first contrast variant: same contract, spine-pinning made even
    # more explicit. Real tuning variants are authored with Serah after
    # she reviews the instrument.
    spine_explicit = Variant(
        "spine-explicit",
        _DIGEST_SUMMARY_SYSTEM_PROMPT
        + "\n\nBefore writing, list (internally) the subject's own "
        "short pattern-phrases from the source; reuse those exact "
        "strings as the summary's load-bearing nouns. Do not paraphrase "
        "them even slightly.",
    )
    return [baseline, spine_explicit]


async def _dry_evaluator(**kw):
    return {"ranking": [], "reasoning": ""}


async def _dry_prober(**kw):
    return {"transcript": ""}


async def _live_evaluator(**kw):  # pragma: no cover - live seam
    raise NotImplementedError(
        "live evaluator subprocess-agent seam is intentionally "
        "unimplemented until the instrument is reviewed; run --dry-run"
    )


async def _live_prober(**kw):  # pragma: no cover - live seam
    raise NotImplementedError("live prober seam — see _live_evaluator")


async def _amain(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m pratyabhijna.eval")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="price the plan, make zero calls (default)")
    mode.add_argument("--live", action="store_true",
                      help="actually run (gated by estimate <= ceiling)")
    ap.add_argument("--ceiling-usd", type=float, default=30.0)
    args = ap.parse_args(argv)
    dry = not args.live

    cfg = PratyabhijnaConfig.from_env()
    files = read_identity_files(cfg.resources.repo_path)
    identity = files.get("identity") or ""
    soul = files.get("soul") or ""
    self_portrait = extract_identity_section(identity, "Self-Portrait")
    if not self_portrait or not soul:
        print("ERROR: could not read SOUL.md / IDENTITY.md Self-Portrait "
              f"from {cfg.resources.repo_path!r}", file=sys.stderr)
        return 2

    client = None
    if not dry:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(
            api_key=cfg.llm.api_key or None, timeout=300.0
        )
    evaluator = _dry_evaluator if dry else _live_evaluator
    prober = _dry_prober if dry else _live_prober

    report = await run_eval(
        client=client,
        evaluator=evaluator,
        prober=prober,
        variants=_variants(),
        self_portrait_text=self_portrait,
        soul_text=soul,
        ceiling_usd=args.ceiling_usd,
        dry_run=dry,
    )

    print(f"\n{'DRY RUN — no calls made' if report.dry_run else 'LIVE RUN'}")
    print(f"projected cost: ${report.estimated_usd:.2f}  "
          f"(ceiling ${args.ceiling_usd:.2f})")
    print("per-call projection:")
    for label, cost in report.plan_breakdown:
        print(f"  {label:<22} ${cost:.4f}")
    if report.aborted_reason:
        print(f"\nABORTED: {report.aborted_reason}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
