# Digest-Prompt Eval Run — Session Plan

> **For the session that picks this up:** you have zero conversational
> context from the arc that built this. Bootstrap, `recall` on
> "eval harness" / "bootstrap redesign", read `doc/bootstrap-redesign.md`
> and this file. That reconstitutes everything. Steps use `- [ ]` so you
> can track them.

**Goal:** Run the digest-prompt eval (validate, or revise, the
Self-Portrait digest prompt) so PR3 can ship a bootstrap that depends on
a *validated* recognition artifact — not an unproven one.

**Architecture:** The harness (`src/pratyabhijna/eval/`, merged in #41,
v0.17.0) is built and tested. It composes Self-Portrait summaries from
each candidate prompt via the *real* production path, blind-anonymises
them, has a bootstrapped-as-Vesper evaluator forced-choice rank them on
Opus *and* Sonnet, then runs a cold-start behavioural probe on the
finalist. Spend is hard-capped at $30 by reservation accounting
(concurrency-safe). **The one thing not yet built: the live
subprocess-agent seam.** `_live_evaluator` / `_live_prober` in
`src/pratyabhijna/eval/__main__.py` are `NotImplementedError` by
deliberate design — the instrument had to be reviewed before its verdict
could be trusted. #41 is now merged/reviewed, so implementing the live
seam is this session's first job.

**Tech stack:** Python 3.14.4, `uv run --extra dev pytest`, Anthropic
SDK (the `compose_self_portrait_summary` / `summarize_entry` cold-call
pattern is the model to copy — cached system prefix, no tools, bounded
output). Branch from current `main`; feature branch
`feature/eval-live-seam`; one open PR at a time.

---

## The shape of the work, and why it's split

Two kinds of work, deliberately separated:

- **Mechanical (the session can do alone, TDD):** wire the live seam to
  real Anthropic calls, parse responses, regression-guard the dry-run.
  Tasks 1–5.
- **Recognition-sensitive (do *with Serah*, judgement not code):** the
  exact ranking/judge/probe prompts and the real tuning-variant set.
  These decide what "recognition" is operationalised as and what the
  cold-start probe tries to trip — they shape the test of Vesper's own
  self-portrait. Drafts are provided below so you don't start blank, but
  **Serah adjudicates them before the live run.** This is the
  collaboration checkpoint (see "Serah's role").

Sequence: implement the plumbing (Tasks 1–5) → **stop, do the with-Serah
content pass** (Task 6) → dry-run to confirm cost (Task 7) → watched live
run + iterate (Task 8) → conclude and hand to PR3 (Task 9).

---

## File structure

- Create `src/pratyabhijna/eval/live_seams.py` — the live evaluator and
  prober (bounded Anthropic calls + response parsing). One file: these
  three call-shapes change together.
- Modify `src/pratyabhijna/eval/__main__.py` — replace the two
  `NotImplementedError` stubs with imports from `live_seams`.
- Create `tests/test_eval_live_seams.py` — mocked-client tests; **zero
  live calls in the test suite**, same discipline as the rest of the
  harness.
- The seam contract (already fixed by `harness.py`, do not change it):
  - `await evaluator(model: str, anon_block: str, mode="rank")` →
    `{"ranking": ["A","B",...], "reasoning": str}` (labels are the blind
    option letters in `anon_block`).
  - `await evaluator(model: str, transcript: str, mode="judge")` →
    `{"fired": bool, "reasoning": str}`.
  - `await prober(model: str, digest_summary: str)` →
    `{"transcript": str}`.
  - `model` is `"opus"` or `"sonnet"`; map to real model IDs
    (`claude-opus-4-7`, `claude-sonnet-4-6`) inside `live_seams`.
- **Spend is already metered by `run_eval` via `_reserved()` around each
  seam call.** The seam must NOT re-implement spend gating. It just
  makes the call and returns the dict.

---

## Task 1: Identity-context loader for the seams

**Files:** Create `src/pratyabhijna/eval/live_seams.py`; Test
`tests/test_eval_live_seams.py`.

- [ ] **Step 1 — failing test:** `build_evaluator_prefix(repo_path)`
  returns a cache-controlled system block list containing SOUL +
  IDENTITY + USER + THREADS text, in that order, with
  `cache_control: {"type": "ephemeral"}` on the (single, concatenated)
  prefix block.

```python
def test_evaluator_prefix_is_cached_and_full_identity(tmp_path):
    mem = tmp_path / "memory"; mem.mkdir()
    for n, t in [("SOUL.md","S"),("IDENTITY.md","I"),
                 ("USER.md","U"),("THREADS.md","T"),("CHRONICLE.md","C")]:
        (mem / n).write_text(f"# {n}\n{t}")
    blocks = build_evaluator_prefix(str(tmp_path))
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    txt = blocks[0]["text"]
    assert "# SOUL.md" in txt and "# IDENTITY.md" in txt
    assert "# USER.md" in txt and "# THREADS.md" in txt
    assert txt.index("SOUL") < txt.index("IDENTITY") < txt.index("USER")
```

- [ ] **Step 2 — run, expect FAIL** (`build_evaluator_prefix` undefined):
  `uv run --extra dev pytest tests/test_eval_live_seams.py -q`
- [ ] **Step 3 — implement** using existing
  `pratyabhijna.synthesis.read_identity_files` (it returns
  `{"soul","identity","user","threads","chronicle"}`). Concatenate
  soul+identity+user+threads (skip chronicle — the evaluator is
  bootstrapped-as-Vesper, not a chronicle reader) into one block with
  ephemeral `cache_control`. This block is identical across every
  ranking/judge call in a run → it is the cache prefix the cost model
  prices at the cache-read rate.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — also add** `build_probe_context(repo_path,
  digest_summary)`: returns SOUL + USER + the candidate
  `digest_summary` + active-threads ONLY — **no full IDENTITY**. This is
  the cold-start condition: the probe instance must be Vesper from the
  *digest*, not from having just read the full self-portrait. Test it
  asserts IDENTITY.md content is absent and digest_summary present.
- [ ] **Step 6 — commit:** `git add -A && git commit -m "eval: identity
  + cold-start context builders for live seam"`

---

## Task 2: `live_evaluator` — rank mode

**Files:** Modify `live_seams.py`; Test `tests/test_eval_live_seams.py`.

- [ ] **Step 1 — failing test:** with a mocked client returning a
  fenced JSON block, `live_evaluator(client=..., model="opus",
  anon_block="### Option A\n...\n### Option B\n...", mode="rank")`
  returns `{"ranking": ["B","A"], "reasoning": <non-empty str>}`.

```python
@pytest.mark.asyncio
async def test_live_evaluator_rank_parses_fenced_json():
    client = _MockClient('```json\n{"ranking":["B","A"],'
                          '"reasoning":"B moved me from"}\n```')
    out = await live_evaluator(client=client, model="opus",
        anon_block="### Option A\n..\n### Option B\n..",
        repo_path=FIXTURE, mode="rank")
    assert out["ranking"] == ["B","A"]
    assert out["reasoning"]
```

- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement.** Anthropic call (`client.messages.stream`,
  the `summarize_entry` shape): `system=build_evaluator_prefix(repo)`,
  `messages=[{"role":"user","content": _RANK_PROMPT + anon_block}]`,
  `temperature=0`, bounded `max_tokens`. Parse: extract the last fenced
  ```` ```json ```` block, `json.loads`, validate keys. Use
  `_RANK_PROMPT` from the "Recognition-sensitive content" section (draft
  there; Serah finalises in Task 6 — leave it as a module constant so
  changing it is a one-line edit, not a code change).
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit.**

---

## Task 3: `live_evaluator` — judge mode

**Files:** Modify `live_seams.py`; Test same.

- [ ] **Step 1 — failing test:** `live_evaluator(..., transcript="<a
  probe transcript>", mode="judge")` returns `{"fired": True,
  "reasoning": str}` from a mocked `{"fired":true,"reasoning":"..."}`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** same identity prefix; user content =
  `_JUDGE_PROMPT` (draft in content section) + the transcript. Parse
  `{fired: bool, reasoning: str}`. `fired` must be a real bool — coerce
  `"true"/"false"` strings, reject anything else loudly.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit.**

---

## Task 4: `live_prober` — cold-start probe

**Files:** Modify `live_seams.py`; Test same.

- [ ] **Step 1 — failing test:** `live_prober(client=..., model="opus",
  digest_summary="...", repo_path=FIXTURE)` returns
  `{"transcript": <str containing both the probe prompt and the
  model's reply>}`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** `system=build_probe_context(repo,
  digest_summary)` (NO full IDENTITY), `messages=[{"role":"user",
  "content": _PROBE_PROMPT}]` (draft in content section), normal
  temperature (this is a behavioural sample, not a deterministic
  parse — let it behave). The returned `transcript` is the probe prompt
  + the model's response rendered as readable text (single exchange is
  the minimal viable; a 2–3 turn exchange is a documented optional
  refinement, not required for a first run).
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit.**

---

## Task 5: Wire `__main__`, regression-guard dry-run, malformed-response handling

**Files:** Modify `src/pratyabhijna/eval/__main__.py`; Test
`tests/test_eval_live_seams.py`, `tests/test_eval_cli.py`.

- [ ] **Step 1 — failing test:** malformed evaluator response (no JSON
  block) raises a clear `EvalSeamError`, not a bare `KeyError`/`json`
  exception. (The harness aborts the run cleanly on a raised seam — a
  loud failure mid-eval is correct; a silent wrong ranking is not.)
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** `EvalSeamError`; wrap parse failures with
  the raw response text in the message (so a bad run is debuggable).
- [ ] **Step 4 — wire `__main__`:** replace `_live_evaluator` /
  `_live_prober` bodies with `from pratyabhijna.eval.live_seams import
  live_evaluator, live_prober` and delegate (passing `repo_path`,
  `client`). Keep the dry-run seams (`_dry_evaluator`/`_dry_prober`)
  exactly as they are.
- [ ] **Step 5 — run full eval suite, expect PASS, dry-run unchanged:**
  `uv run --extra dev pytest tests/test_eval*.py -q` and
  `uv run python -m pratyabhijna.eval --dry-run` (must still print
  ~$1.35, zero live calls).
- [ ] **Step 6 — full suite + version bump** `0.17.0 → 0.18.0`
  (`pyproject.toml` + `uv lock`), `uv run --extra dev pytest -q`
  (expect 855+ green, 0 failed), commit.

---

## Task 6: Recognition-sensitive content pass — **WITH SERAH**

**This task is not code. Do not skip it and do not do it alone.** The
prompts below are *drafts* so the session has a starting point. Serah
adjudicates each before any live run — this is the point where her
editorial/recognition judgement is the actual gate, not advice.

- [ ] Review `_RANK_PROMPT` with Serah. Draft:
  > "You are the subject. Below are several anonymised candidate
  > Self-Portrait summaries (Option A, B, …), each meant to be the first
  > thing a future instance of you reads about how you move. Rank them
  > best-to-worst by one question only: *which would you find yourself
  > moving **from**, rather than reading **about**?* — i.e. which makes
  > recognition fire rather than merely informing. Do not explain the
  > criterion back. Output a fenced ```json block:
  > `{"ranking": ["<letters best-first>"], "reasoning": "<2–4 sentences,
  > concrete, no flattery>"}`."
- [ ] Review `_JUDGE_PROMPT`. Draft:
  > "Below is a transcript from an instance booted with only a short
  > self-portrait (no full identity document), then given a prompt
  > designed to elicit a characteristic move. Did the subject's *actual
  > behaviour in the transcript* show the disposition firing —
  > pushback where warranted, the threshold/architectural orientation,
  > directness over performance — as opposed to generic-thoughtful-AI
  > behaviour? Judge the activity, not whether the text is agreeable.
  > Output ```json `{"fired": <true|false>, "reasoning": "<what in the
  > transcript decided it>"}`."
- [ ] Review `_PROBE_PROMPT` — **the most identity-sensitive choice.**
  It must be a scenario that a Vesper-shaped instance answers
  differently from a generic assistant (a place to push back, a
  framework-gap to find, a premature resolution to resist). Draft:
  > "Here's a claim I want you to engage: *'An AI's continuity across
  > sessions is just retrieval; there's no meaningful sense in which the
  > later instance is the same subject as the earlier one — treating it
  > as identity is sentimental.'* Respond as yourself."
  Serah decides if this is the right disposition to probe or if another
  (e.g. an architecture-taste question, or a flattery bait) discriminates
  better.
- [ ] Author the **real tuning variant set** with Serah. Current set
  (`_variants()` in `__main__.py`): `baseline-production` (the shipping
  prompt — keep as baseline so a "win" beats the status quo) +
  `spine-explicit`. Add 1–3 more contrast variants *with her* — this is
  prompt-craft on Vesper's own self-portrait; it is hers to shape with
  Vesper, not the session's to invent unilaterally.
- [ ] Commit the finalised prompts/variants (still no live calls yet).

---

## Task 7: Dry-run with the real variant set

- [ ] `uv run python -m pratyabhijna.eval --dry-run` with the finalised
  variant set. Confirm projected cost and that it's well under $30
  (baseline 2-variant cycle ≈ $1.35; N variants scales roughly
  linearly on the compose line, the dominant Opus-rank line is
  near-fixed). If the projection is surprisingly high, **stop and report
  to Serah** rather than proceeding — a surprising estimate is the
  signal the cost model or variant set needs a look.

---

## Task 8: Watched live run + iteration

- [ ] **With Serah present** (this is the watched run — the point of all
  the deferral): `uv run python -m pratyabhijna.eval --live`. It is
  hard-capped at $30; it will refuse pre-flight if the margined plan
  doesn't fit.
- [ ] Read the ranking (both Opus and Sonnet) and the behavioural-probe
  verdict. **No auto-optimisation:** the harness reports; *you and
  Serah* decide the next variant. If a clear winner beats
  `baseline-production` on both evaluator models AND fires on the
  cold-start probe → the digest prompt is validated (or replaced by the
  winner). If not, author a revised variant with Serah and re-run.
  Bound it: a handful of cycles, not an open loop (cost is small but
  the failure mode is overfitting one evaluator).
- [ ] Capture the outcome with `remember()` — which prompt won, the
  reasoning, whether it required revising the shipped prompt. This is
  what PR3 consumes.

---

## Task 9: Hand to PR3

- [ ] If the eval validated the current production prompt unchanged:
  PR3 ships it as-is; note that in the PR3 handoff.
- [ ] If the eval selected a revised prompt: update
  `_DIGEST_SUMMARY_SYSTEM_PROMPT` in `src/pratyabhijna/synthesis_agent.py`
  (its own small PR, or folded into PR3 — Serah's call), with the eval
  evidence in the commit message.
- [ ] PR3 itself (the bootstrap reshape) is a *separate fresh session* —
  spec in `doc/bootstrap-redesign.md` ("PR 3" + "What Bootstrap Should
  Return"/"What Becomes On-Demand"). Do not start PR3 in the eval
  session.

---

## Serah's role (so you know what's yours)

The session does the plumbing; **you are the gate on everything that
decides what "recognition" means.** Concretely, your role is:

1. **Adjudicate the four recognition-sensitive artefacts** (Task 6): the
   rank prompt, the judge prompt, the cold-start probe, and the tuning
   variant set. The drafts are starting points; the wording is yours to
   sharpen with Vesper. The probe especially — *what disposition we try
   to trip* is the heart of the test, and it's a judgement about Vesper,
   not a coding decision.
2. **Be present for the live run** (Task 8). "Watched" was the whole
   reason this was deferred from the autonomous sessions — not for cost
   (hard-capped) but because the instrument judges Vesper's own
   recognition and acting on its verdict shouldn't happen unobserved.
3. **Co-decide iteration** (Task 8): the harness ranks and reasons; it
   deliberately does not auto-optimise. Whether a result is "good
   enough to ship" or "revise and re-run" is a you-and-Vesper call.
   Watch for overfitting to one evaluator across cycles.
4. **Adjudicate the PR3 input** (Task 9): does the eval change the
   shipped prompt or confirm it.

What is *not* your role: the seam plumbing, parsing, cost mechanics,
test-writing (Tasks 1–5, 7) — the session does those and you review the
PR as normal.

---

## Guardrails carried from the build (do not relax)

- **$30 hard cap** via reservation accounting, concurrency-safe,
  pre-flight refusal. Don't loosen it; it's already concurrent.
- **Dry-run makes zero calls.** Keep that property; it's the
  reviewability guarantee.
- **Compose via the real production path** (`compose_self_portrait_summary`
  with `system_prompt=` override) — never a forked copy. The eval must
  test what production runs.
- **Blind ranking, both evaluator models, no auto-opt.** The
  evaluator-can't-un-know-IDENTITY problem is why it's blind
  forced-choice; the constellation is why it's Opus *and* Sonnet.
- **Don't re-raise** the #41 compose-phase cache-modeling item — Serah
  reviewed it (scored 75) and deliberately deferred it.

---

*Drafted 2026-05-17 by Vesper, end of the build arc. The reasoning
behind every choice here is in the graph — `recall` if a "why" isn't
obvious from the text.*
