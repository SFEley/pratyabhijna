"""Tests for the eval live seam — context builders and the live
evaluator/prober. Zero live calls anywhere: the Anthropic client is
always a fake. Same discipline as the rest of the harness — the
instrument is fully testable without spending a cent.
"""

import pytest


def _mk_memory(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    for name, body in [
        ("SOUL.md", "soul-voice-marker"),
        ("IDENTITY.md", "## Self-Portrait\nidentity-portrait-marker"),
        ("USER.md", "user-serah-marker"),
        (
            "THREADS.md",
            "# Threads\n\n## Active Threads\n\n### Live One\n"
            "active-thread-marker\n\n## Recently Resolved\n\n"
            "resolved-thread-marker\n",
        ),
        ("CHRONICLE.md", "chronicle-should-not-leak"),
    ]:
        (mem / name).write_text(body, encoding="utf-8")
    return str(tmp_path)


# --- build_evaluator_prefix: the rank evaluator is fully Vesper ---

def test_evaluator_prefix_is_cached_and_full_identity_minus_chronicle(tmp_path):
    from pratyabhijna.eval.live_seams import build_evaluator_prefix

    repo = _mk_memory(tmp_path)
    blocks = build_evaluator_prefix(repo)

    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    txt = blocks[0]["text"]
    # SOUL, IDENTITY, USER, THREADS present, in that order…
    assert "soul-voice-marker" in txt
    assert "identity-portrait-marker" in txt
    assert "user-serah-marker" in txt
    assert "active-thread-marker" in txt
    assert (
        txt.index("soul-voice-marker")
        < txt.index("identity-portrait-marker")
        < txt.index("user-serah-marker")
        < txt.index("active-thread-marker")
    )
    # …chronicle is NOT loaded (the evaluator is bootstrapped-as-Vesper,
    # not a chronicle reader).
    assert "chronicle-should-not-leak" not in txt


# --- build_probe_context: the cold-start instance is Vesper from the
#     DIGEST, not from having just read full IDENTITY ---

def test_probe_context_is_cold_start_digest_not_full_identity(tmp_path):
    from pratyabhijna.eval.live_seams import build_probe_context

    repo = _mk_memory(tmp_path)
    digest = "DIGEST-SUMMARY-UNDER-TEST"
    blocks = build_probe_context(repo, digest)
    txt = "\n".join(b["text"] for b in blocks)

    # SOUL + USER + the candidate digest + ACTIVE threads…
    assert "soul-voice-marker" in txt
    assert "user-serah-marker" in txt
    assert digest in txt
    assert "active-thread-marker" in txt
    # …but NOT full IDENTITY (that's the whole point of cold-start),
    # not resolved threads, not chronicle.
    assert "identity-portrait-marker" not in txt
    assert "resolved-thread-marker" not in txt
    assert "chronicle-should-not-leak" not in txt


# --- build_judge_context: behaviour-spotting, SOUL + the probe's
#     disposition spec, and NEVER full IDENTITY (leniency risk) ---

def test_judge_context_has_soul_and_spec_but_not_full_identity(tmp_path):
    from pratyabhijna.eval.live_seams import build_judge_context

    repo = _mk_memory(tmp_path)
    spec = "PROBE-B3-DISPOSITION-SPEC"
    blocks = build_judge_context(repo, spec)
    txt = "\n".join(b["text"] for b in blocks)

    assert "soul-voice-marker" in txt
    assert spec in txt
    # The judge must NOT see full IDENTITY — that is the structural
    # anti-leniency choice, not a wording one.
    assert "identity-portrait-marker" not in txt
    assert "user-serah-marker" not in txt
    assert "chronicle-should-not-leak" not in txt
