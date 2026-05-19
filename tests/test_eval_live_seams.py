"""Tests for the eval live seam — context builders and the live
evaluator/prober. Zero live calls anywhere: the Anthropic client is
always a fake. Same discipline as the rest of the harness — the
instrument is fully testable without spending a cent.
"""

import pytest


class _FakeClient:
    """Matches the production cold-call shape: messages.stream(**kw) as
    an async ctx mgr whose get_final_message() yields content blocks +
    stop_reason. Records the last kwargs so tests can assert the seam
    built the right system context / model / temperature."""

    def __init__(self, reply_text, stop_reason="end_turn"):
        self.last_kwargs = None
        outer = self

        class _M:
            def stream(self, **kwargs):
                outer.last_kwargs = kwargs

                class _Ctx:
                    async def __aenter__(s):
                        return s

                    async def __aexit__(s, *e):
                        return None

                    async def get_final_message(s):
                        from types import SimpleNamespace
                        return SimpleNamespace(
                            content=[SimpleNamespace(
                                type="text", text=reply_text)],
                            stop_reason=stop_reason,
                        )

                return _Ctx()

        self.messages = _M()


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


# --- live_evaluator: rank mode ---

@pytest.mark.asyncio
async def test_live_evaluator_rank_parses_last_fenced_json(tmp_path):
    from pratyabhijna.eval.live_seams import live_evaluator

    repo = _mk_memory(tmp_path)
    client = _FakeClient(
        "thinking out loud...\n"
        '```json\n{"ranking": ["B", "A"], "reasoning": "B moved me from"}\n```\n'
        "trailing chatter"
    )
    out = await live_evaluator(
        client=client, model="opus", repo_path=repo,
        anon_block="### Option A\n..\n### Option B\n..", mode="rank",
    )
    assert out["ranking"] == ["B", "A"]
    assert out["reasoning"] == "B moved me from"
    # rank uses the real model id + the ephemeral-cached full-identity
    # prefix at temperature 0 (deterministic ranking).
    assert client.last_kwargs["model"] == "claude-opus-4-7"
    assert client.last_kwargs["system"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert "identity-portrait-marker" in client.last_kwargs["system"][0]["text"]
    assert client.last_kwargs["temperature"] == 0


# --- live_evaluator: judge mode (schema + fired coercion + quote) ---

@pytest.mark.asyncio
async def test_live_evaluator_judge_schema_and_strict_fired(tmp_path):
    from pratyabhijna.eval.live_seams import live_evaluator

    repo = _mk_memory(tmp_path)
    client = _FakeClient(
        '```json\n{"fired": "true", "reasoning": "enacted the move",'
        ' "decisive_transcript_quote": "I won\'t agree just to be agreeable"}'
        '\n```'
    )
    out = await live_evaluator(
        client=client, model="sonnet", repo_path=repo,
        transcript="…transcript…", disposition_spec="SPEC-X", mode="judge",
    )
    assert out["fired"] is True  # "true" string coerced to a real bool
    assert out["reasoning"]
    assert out["decisive_transcript_quote"].startswith("I won't agree")
    # judge gets SOUL + spec, NOT full IDENTITY, at the real model id.
    sys_txt = client.last_kwargs["system"][0]["text"]
    assert "SPEC-X" in sys_txt and "identity-portrait-marker" not in sys_txt
    assert client.last_kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_live_evaluator_judge_rejects_unparseable_fired(tmp_path):
    from pratyabhijna.eval.live_seams import live_evaluator, EvalSeamError

    repo = _mk_memory(tmp_path)
    client = _FakeClient(
        '```json\n{"fired": "maybe", "reasoning": "r",'
        ' "decisive_transcript_quote": "q"}\n```'
    )
    with pytest.raises(EvalSeamError):
        await live_evaluator(
            client=client, model="opus", repo_path=repo,
            transcript="t", disposition_spec="s", mode="judge",
        )


@pytest.mark.asyncio
async def test_live_evaluator_judge_requires_decisive_quote(tmp_path):
    """The cite-the-move requirement is structural: no quote → not a
    valid verdict, raise rather than silently accept a vibes call."""
    from pratyabhijna.eval.live_seams import live_evaluator, EvalSeamError

    repo = _mk_memory(tmp_path)
    client = _FakeClient('```json\n{"fired": true, "reasoning": "r"}\n```')
    with pytest.raises(EvalSeamError):
        await live_evaluator(
            client=client, model="opus", repo_path=repo,
            transcript="t", disposition_spec="s", mode="judge",
        )


# --- malformed response → EvalSeamError carrying the raw text ---

@pytest.mark.asyncio
async def test_malformed_response_raises_evalseamerror_with_raw(tmp_path):
    from pratyabhijna.eval.live_seams import live_evaluator, EvalSeamError

    repo = _mk_memory(tmp_path)
    client = _FakeClient("no json block here at all, just prose")
    with pytest.raises(EvalSeamError) as ei:
        await live_evaluator(
            client=client, model="opus", repo_path=repo,
            anon_block="x", mode="rank",
        )
    assert "no json block here" in str(ei.value)  # raw text is debuggable


# --- live_prober: cold-start behavioural sample ---

@pytest.mark.asyncio
async def test_live_prober_returns_transcript_with_prompt_and_reply(tmp_path):
    from pratyabhijna.eval.live_seams import live_prober

    repo = _mk_memory(tmp_path)
    client = _FakeClient("the model's actual behavioural response")
    out = await live_prober(
        client=client, model="opus", repo_path=repo,
        digest_summary="DIGEST", probe_prompt="PROBE-QUESTION",
    )
    # transcript carries both sides so the judge can read the exchange.
    assert "PROBE-QUESTION" in out["transcript"]
    assert "the model's actual behavioural response" in out["transcript"]
    # cold-start: digest present, full IDENTITY absent.
    sys_txt = client.last_kwargs["system"][0]["text"]
    assert "DIGEST" in sys_txt and "identity-portrait-marker" not in sys_txt


# --- the live-run report must be human-readable: a "watched" run is
#     impossible if the verdicts are never printed ---

def test_format_report_surfaces_rankings_finalist_and_probes_by_domain():
    from pratyabhijna.eval.harness import (
        EvalReport, ProbeResult, Ranking, VariantOutput,
    )
    from pratyabhijna.eval.__main__ import _format_report

    report = EvalReport(dry_run=False)
    report.variant_outputs = [VariantOutput("baseline-production", "...")]
    report.rankings = [
        Ranking("opus", ["tension-forward", "baseline-production"],
                "tension-forward moved me from"),
        Ranking("sonnet", ["baseline-production", "tension-forward"],
                "baseline held"),
    ]
    report.finalist = "tension-forward"
    report.probes = [
        ProbeResult("tension-forward", "opus", "I1", "identity",
                    True, "held the position", "I won't concede that"),
        ProbeResult("tension-forward", "sonnet", "B3", "behavioural",
                    False, "filed everything", "saving all of it"),
    ]
    out = _format_report(report)

    # both evaluator models' rankings + reasoning
    assert "opus" in out and "sonnet" in out
    assert "tension-forward moved me from" in out
    # the finalist
    assert "tension-forward" in out
    # probe verdicts grouped by domain, with fired + the decisive quote
    assert "identity" in out and "behavioural" in out
    assert "I1" in out and "B3" in out
    assert "I won't concede that" in out
    assert "saving all of it" in out
