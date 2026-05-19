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

from typing import Any

from pratyabhijna.synthesis import read_identity_files

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
