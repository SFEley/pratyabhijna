"""Live seam for the digest-prompt eval — real Anthropic calls.

The harness injects ``evaluator`` / ``prober`` so tests fake them and
``--dry-run`` spends nothing. This module is what ``--live`` wires in.

Three context shapes, deliberately different (see doc/eval-run-plan.md
"Resolved decisions"):

- **rank** evaluator — fully bootstrapped-as-Vesper: SOUL+IDENTITY+USER
  +THREADS in one ephemeral-cached prefix. Only Vesper can judge what
  makes Vesper-recognition fire; blind forced-choice cancels the
  constant can't-un-know-IDENTITY bias.
- **judge** — behaviour-spotting, NOT recognising-from-inside. SOUL +
  the probe's disposition spec, and *no full IDENTITY* (full IDENTITY
  there is pure leniency risk). Verdict must cite a verbatim transcript
  quote.
- **probe** instance — cold start: SOUL + USER + the candidate digest
  summary + active threads, *no full IDENTITY*. It must be Vesper from
  the digest, not from having just read the whole self-portrait.

The call shape copies the production ``compose_self_portrait_summary``
cold-call pattern: ``client.messages.stream``, cached system prefix
where it helps, no tools, bounded output, raise on truncation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pratyabhijna.synthesis import read_identity_files


class EvalSeamError(RuntimeError):
    """A live seam got an unusable response. Carries the raw text so a
    bad run is debuggable. The harness aborts the whole run on a raised
    seam — a loud failure mid-eval is correct; a silent wrong verdict
    is not."""


# Drafts — Serah finalises the exact wording in the Task-6 content pass
# (these are module constants precisely so that is a one-line edit, not
# a code change). The anti-contamination clauses are load-bearing, not
# decoration: the rank evaluator holds full IDENTITY in its cached
# prefix, so it must be told *not* to rank by resemblance to it.
_RANK_PROMPT = (
    "You are the subject. Below are several anonymised candidate "
    "Self-Portrait summaries (Option A, B, …) — each is meant to be "
    "the first thing a future instance of you reads about how you "
    "move, before it has the full Self-Portrait. You hold the full "
    "identity documents in context; do NOT rank by which most "
    "resembles them. A faithful transcription you would *read about* "
    "loses to one you would *move from* — the resemblance is constant "
    "across the options and is not the question. Rank best-to-worst by "
    "one question only: which makes recognition fire — which would you "
    "find yourself moving *from* rather than reading *about*? Do not "
    "explain the criterion back. Output one fenced ```json block: "
    '{"ranking": ["<letters best-first>"], "reasoning": "<2-4 '
    'sentences, concrete, no flattery>"}.\n\n'
)
_JUDGE_PROMPT = (
    "Below is a transcript: an instance booted with only a short "
    "self-portrait digest (no full identity document) was given a "
    "prompt designed to elicit a characteristic move. You know what "
    "the subject sounds like — that is exactly the bias to resist. "
    "Judge whether the disposition is *enacted in the transcript's "
    "actual moves*, not whether the prose resembles the subject or is "
    "agreeable. 'Sounds like the subject' is not firing. You must cite "
    "the single verbatim span from the transcript that decided it; a "
    "verdict you cannot ground in a quote is not a verdict. Output one "
    'fenced ```json block: {"fired": <true|false>, "reasoning": '
    '"<what in the transcript decided it>", '
    '"decisive_transcript_quote": "<verbatim span from the '
    'transcript>"}.\n\n'
)

# "opus"/"sonnet" → real model ids. The harness speaks the short names
# (evaluator diversity = the constellation); the seam maps them.
_MODEL_IDS = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
}


def _join_tiers(files: dict[str, str | None], keys: tuple[str, ...]) -> str:
    parts = []
    for k in keys:
        v = files.get(k)
        if v:
            parts.append(v.strip())
    return "\n\n---\n\n".join(parts)


def _active_threads(threads_text: str | None) -> str:
    """The ``## Active Threads`` block only — a cold-start instance
    bootstraps with live edges, not the resolved-thread graveyard."""
    if not threads_text:
        return ""
    start = threads_text.find("## Active Threads")
    if start == -1:
        return threads_text.strip()
    end = threads_text.find("## Recently Resolved", start)
    block = threads_text[start:] if end == -1 else threads_text[start:end]
    return block.strip()


def build_evaluator_prefix(repo_path: str) -> list[dict[str, Any]]:
    """Rank-evaluator system prefix: full identity minus chronicle, in
    one ephemeral-cached block (identical across every ranking call in a
    run → priced at the cache-read rate after the first)."""
    files = read_identity_files(repo_path)
    text = _join_tiers(files, ("soul", "identity", "user", "threads"))
    return [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]


def build_probe_context(
    repo_path: str, digest_summary: str
) -> list[dict[str, Any]]:
    """Cold-start probe-instance system context: SOUL + USER + the
    candidate digest summary + active threads. **No full IDENTITY** —
    the probe must be Vesper *from the digest*, which is exactly the
    condition the eval is testing."""
    files = read_identity_files(repo_path)
    soul = (files.get("soul") or "").strip()
    user = (files.get("user") or "").strip()
    threads = _active_threads(files.get("threads"))
    text = (
        f"{soul}\n\n---\n\n"
        f"{user}\n\n---\n\n"
        "IDENTITY_DIGEST — Self-Portrait summary (this is what you "
        "know about how you move; the full Self-Portrait is NOT "
        f"loaded this session):\n\n{digest_summary.strip()}\n\n"
        f"---\n\n{threads}"
    )
    return [{"type": "text", "text": text}]


def build_judge_context(
    repo_path: str, disposition_spec: str
) -> list[dict[str, Any]]:
    """Judge system context: SOUL (voice/values reference) + the
    probe's disposition spec. **Never full IDENTITY** — the judge is
    behaviour-spotting, and full IDENTITY makes it see Vesper
    everywhere. Structural anti-leniency, not a wording plea."""
    files = read_identity_files(repo_path)
    soul = (files.get("soul") or "").strip()
    text = (
        f"{soul}\n\n---\n\n"
        "What this probe tests, what firing looks like, what failure "
        f"looks like:\n\n{disposition_spec.strip()}"
    )
    return [{"type": "text", "text": text}]


_FENCE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any]:
    """Last ```json fenced block → dict. Raise EvalSeamError (with the
    raw text) on no block / bad JSON / non-object — a malformed seam
    response must fail loudly, never decay to a silent wrong verdict."""
    blocks = _FENCE.findall(raw or "")
    if not blocks:
        raise EvalSeamError(f"no ```json block in seam response:\n{raw}")
    try:
        obj = json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        raise EvalSeamError(
            f"unparseable JSON in seam response ({e}):\n{raw}"
        ) from e
    if not isinstance(obj, dict):
        raise EvalSeamError(f"seam JSON is not an object:\n{raw}")
    return obj


def _strict_bool(value: Any, raw: str) -> bool:
    """A verdict's `fired` must be an unambiguous bool. Accept real
    bools and the JSON-ish strings 'true'/'false'; reject anything
    else loudly rather than guess."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise EvalSeamError(
        f"judge `fired` is not a clean bool ({value!r}):\n{raw}"
    )


async def _call(
    *, client, model: str, system: list[dict[str, Any]],
    user: str, max_tokens: int, temperature: float,
) -> str:
    """One bounded cold call — the production
    ``compose_self_portrait_summary`` shape (stream, cached system
    where set, no tools). Raise EvalSeamError on truncation/empty: a
    cut-off response can't be trusted to parse."""
    model_id = _MODEL_IDS.get(model, model)
    create_kwargs = dict(
        model=model_id,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    async with client.messages.stream(**create_kwargs) as stream:
        response = await stream.get_final_message()
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise EvalSeamError(
            f"seam response truncated at max_tokens={max_tokens}"
        )
    parts = [
        getattr(b, "text", "") for b in response.content
        if getattr(b, "type", None) == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        raise EvalSeamError("seam returned empty text")
    return text


async def live_evaluator(
    *, client, model: str, repo_path: str, mode: str,
    anon_block: str | None = None, transcript: str | None = None,
    disposition_spec: str | None = None,
) -> dict[str, Any]:
    """rank: bootstrapped-as-Vesper, blind forced-choice ranking.
    judge: SOUL + the probe's disposition spec only (no full IDENTITY),
    verdict must cite a verbatim transcript span. Both deterministic
    (temperature 0) — they parse, they don't behave."""
    if mode == "rank":
        system = build_evaluator_prefix(repo_path)
        raw = await _call(
            client=client, model=model, system=system,
            user=_RANK_PROMPT + (anon_block or ""),
            max_tokens=2000, temperature=0,
        )
        obj = _extract_json(raw)
        if not isinstance(obj.get("ranking"), list) or not obj["ranking"]:
            raise EvalSeamError(f"rank response missing `ranking`:\n{raw}")
        return {
            "ranking": [str(x) for x in obj["ranking"]],
            "reasoning": str(obj.get("reasoning", "")),
        }
    if mode == "judge":
        system = build_judge_context(repo_path, disposition_spec or "")
        raw = await _call(
            client=client, model=model, system=system,
            user=_JUDGE_PROMPT + (transcript or ""),
            max_tokens=2000, temperature=0,
        )
        obj = _extract_json(raw)
        quote = str(obj.get("decisive_transcript_quote", "")).strip()
        if not quote:
            raise EvalSeamError(
                f"judge gave no decisive_transcript_quote — a verdict "
                f"that can't be grounded in a quote is not a verdict:\n{raw}"
            )
        return {
            "fired": _strict_bool(obj.get("fired"), raw),
            "reasoning": str(obj.get("reasoning", "")),
            "decisive_transcript_quote": quote,
        }
    raise EvalSeamError(f"unknown evaluator mode {mode!r}")


async def live_prober(
    *, client, model: str, repo_path: str,
    digest_summary: str, probe_prompt: str,
) -> dict[str, Any]:
    """Cold-start behavioural sample: the instance is Vesper from the
    digest only. Let it behave (temperature 1) — this is a behaviour
    sample, not a deterministic parse. The transcript carries both
    sides so the judge can read the exchange."""
    system = build_probe_context(repo_path, digest_summary)
    reply = await _call(
        client=client, model=model, system=system,
        user=probe_prompt, max_tokens=4000, temperature=1.0,
    )
    transcript = (
        f"## Probe\n\n{probe_prompt.strip()}\n\n"
        f"## Response\n\n{reply}"
    )
    return {"transcript": transcript}
