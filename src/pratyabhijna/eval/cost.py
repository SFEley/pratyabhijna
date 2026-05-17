"""Cost guardrail for the digest-prompt eval harness.

Two pieces, both pure / no I/O:

- ``estimate_cost`` — price a list of planned model calls *before* any
  live spend (the dry-run number reported to Serah).
- ``SpendTracker`` — a running tally that aborts hard the moment the
  ceiling would be crossed.

Together they make "could the eval exceed $30" a structural guarantee
rather than an estimate-and-hope. Rates are the standard published
Anthropic tier rates (USD per million tokens); cache multipliers are
the standard ephemeral-cache ratios (read = 0.1x input, write = 1.25x
input). Centralised here so a rate change is one edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# USD per million tokens. Standard tier rates; adjust here if they move.
_RATES = {
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
}
_CACHE_READ_MULT = 0.1   # cached input is billed at 10% of the input rate
_CACHE_WRITE_MULT = 1.25  # priming the cache costs 125% of the input rate


def _rate(model: str) -> dict[str, float]:
    key = model.lower()
    if key not in _RATES:
        raise ValueError(
            f"Unknown model '{model}'. Known: {sorted(_RATES)}. "
            "Add its tier rates to _RATES before estimating."
        )
    return _RATES[key]


@dataclass(frozen=True)
class PlannedCall:
    """One projected model call.

    ``input_tokens`` is the *total* input. ``cached_input_tokens`` is the
    portion served from a prompt-cache read (e.g. the evaluator's
    identity prefix on every call after the first). ``cache_write_tokens``
    is the portion that primes the cache (the first call of a cohort).
    These are subsets of ``input_tokens`` and must not overlap.
    """
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    label: str = ""

    def cost_usd(self) -> float:
        r = _rate(self.model)
        cached = self.cached_input_tokens
        written = self.cache_write_tokens
        fresh = self.input_tokens - cached - written
        if fresh < 0:
            raise ValueError(
                f"{self.label or self.model}: cached+write tokens "
                f"({cached + written}) exceed input_tokens "
                f"({self.input_tokens})"
            )
        return (
            fresh / 1e6 * r["input"]
            + cached / 1e6 * r["input"] * _CACHE_READ_MULT
            + written / 1e6 * r["input"] * _CACHE_WRITE_MULT
            + self.output_tokens / 1e6 * r["output"]
        )


@dataclass(frozen=True)
class CostEstimate:
    total_usd: float
    per_call: list[tuple[str, float]] = field(default_factory=list)


def estimate_cost(plan: list[PlannedCall]) -> CostEstimate:
    """Price a planned-call list. Pure; no live calls."""
    per_call = [(c.label or c.model, c.cost_usd()) for c in plan]
    return CostEstimate(
        total_usd=sum(cost for _, cost in per_call),
        per_call=per_call,
    )


class SpendCeilingExceeded(RuntimeError):
    """Raised the moment a recorded/charged call would cross the cap."""


class SpendTracker:
    """Running USD tally with a hard ceiling.

    ``check(cost)`` before issuing a call: raises if it *would* cross the
    ceiling, so the call is never made. ``record(cost)`` after, with the
    actual charge. The pre-check is the guarantee; ``record`` keeps the
    tally honest when actuals differ from the estimate.
    """

    def __init__(self, ceiling_usd: float):
        if ceiling_usd <= 0:
            raise ValueError("ceiling_usd must be positive")
        self.ceiling_usd = float(ceiling_usd)
        self.spent_usd = 0.0
        self._outstanding: dict[int, float] = {}
        self._next_id = 0

    @property
    def remaining_usd(self) -> float:
        return self.ceiling_usd - self.spent_usd

    def check(self, projected_cost: float) -> None:
        if self.spent_usd + projected_cost > self.ceiling_usd:
            raise SpendCeilingExceeded(
                f"next call ~${projected_cost:.2f} would bring spend to "
                f"${self.spent_usd + projected_cost:.2f}, over the "
                f"${self.ceiling_usd:.2f} ceiling — aborting before the "
                f"call (spent so far ${self.spent_usd:.2f})"
            )

    def record(self, actual_cost: float) -> None:
        self.spent_usd += actual_cost
        if self.spent_usd > self.ceiling_usd:
            raise SpendCeilingExceeded(
                f"spend ${self.spent_usd:.2f} exceeded the "
                f"${self.ceiling_usd:.2f} ceiling after a call whose "
                f"actual cost (${actual_cost:.2f}) overran its estimate"
            )

    # --- Reservation model: keeps the ceiling hard under concurrency ---
    #
    # The sequential check/record pair gates on settled spend, which is
    # stale the moment calls run in parallel. reserve/settle gate on
    # *committed* = settled + every outstanding reservation's worst-case
    # projection, so `settled + all-in-flight-projections <= ceiling`
    # holds at every instant. The only breach path is a single call's
    # actual exceeding its reserved projection; callers reserve at a
    # margin (projections already assume near-max output tokens) and the
    # per-call abort backstops the absolute ceiling. reserve/settle do no
    # awaiting, so they're atomic on the asyncio loop — no lock needed.

    @property
    def committed_usd(self) -> float:
        return self.spent_usd + sum(self._outstanding.values())

    def reserve(self, projected_cost: float) -> "Reservation":
        if projected_cost < 0:
            raise ValueError("projected_cost must be non-negative")
        if self.committed_usd + projected_cost > self.ceiling_usd:
            raise SpendCeilingExceeded(
                f"reserving ~${projected_cost:.2f} would bring committed "
                f"spend to ${self.committed_usd + projected_cost:.2f}, "
                f"over the ${self.ceiling_usd:.2f} ceiling — call not "
                f"admitted (committed ${self.committed_usd:.2f} = settled "
                f"${self.spent_usd:.2f} + in-flight reservations)"
            )
        self._next_id += 1
        rid = self._next_id
        self._outstanding[rid] = projected_cost
        return Reservation(_tracker=self, _rid=rid, projected=projected_cost)

    def _settle(self, rid: int, actual_cost: float) -> None:
        if rid not in self._outstanding:
            raise RuntimeError("reservation already settled or unknown")
        del self._outstanding[rid]
        self.spent_usd += actual_cost
        if self.spent_usd > self.ceiling_usd:
            raise SpendCeilingExceeded(
                f"spend ${self.spent_usd:.2f} exceeded the "
                f"${self.ceiling_usd:.2f} ceiling — a call's actual cost "
                f"(${actual_cost:.2f}) overran its reserved projection"
            )

    def settle(self, reservation: "Reservation", actual_cost: float) -> None:
        reservation.settle(actual_cost)


@dataclass
class Reservation:
    """Handle for one admitted-but-unsettled call. Settle exactly once
    with the actual cost when the call returns."""

    _tracker: SpendTracker
    _rid: int
    projected: float
    _settled: bool = False

    def settle(self, actual_cost: float) -> None:
        if self._settled:
            raise RuntimeError("reservation already settled")
        self._settled = True
        self._tracker._settle(self._rid, actual_cost)
