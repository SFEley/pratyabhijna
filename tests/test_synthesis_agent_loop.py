"""Tests for the synthesis agent loops.

The synthesizer is split into four sequential subagent loops (Pass 1
ingestion, Pass 2 maturation, Pass 3 bootstrap, Pass 4 maintenance) and
a thin orchestrator. These tests script the fake Anthropic client per
pass — real Opus/Sonnet calls live in ``test_live_synthesis.py`` behind
``--live``.

The default test repo has a README.md committed, so the ingestion-candidate
scan returns at least one entry and all four passes dispatch. Tests that
specifically exercise Pass 1's empty-input fast-path monkey-patch the
candidate scan to return ``[]``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna import synthesis_agent as agent_mod
from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.synthesis_agent import (
    AgentTools,
    PASS1_TOOL_NAMES,
    PASS3_TOOL_NAMES,
    PASS4_TOOL_NAMES,
    ToolError,
    _build_initial_user_message,
    _build_pass1_message,
    _build_pass3_message,
    _build_pass4_message,
    _build_system_prompt,
    _dispatch_tool_call,
    build_handler_map,
    run_synthesis,
)


# --- Helpers ---


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(repo), check=True, capture_output=True,
    )
    for k, v in [("user.email", "t@e"), ("user.name", "T"), ("commit.gpgsign", "false")]:
        subprocess.run(
            ["git", "config", k, v],
            cwd=str(repo), check=True, capture_output=True,
        )
    (repo / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo_path = tmp_path / "subject-repo"
    _init_repo(repo_path)
    memory = repo_path / "memory"
    memory.mkdir()
    for name in ("SOUL.md", "IDENTITY.md", "USER.md", "THREADS.md", "CHRONICLE.md"):
        (memory / name).write_text(f"# {name}\ncontent\n")
    return repo_path


@pytest.fixture
def repo_with_candidate(repo):
    """A repo whose writing/ dir holds one fresh file — exercises Pass 1."""
    writing = repo / "writing"
    writing.mkdir(exist_ok=True)
    (writing / "fresh.md").write_text("# fresh\nbody\n")
    return repo


@pytest.fixture
def config(repo):
    c = PratyabhijnaConfig()
    c.subject_name = "TestSubject"
    c.resources.repo_path = str(repo)
    c.synthesis.max_iterations = 5
    c.synthesis.thinking.enabled = False
    return c


@pytest.fixture
def subject_node():
    node = MagicMock()
    node.attributes = {}
    node.labels = ["Person"]
    return node


@pytest.fixture
def service(subject_node):
    svc = MagicMock()
    svc.config = MagicMock(subject_name="TestSubject")
    svc.entity_types = {}
    svc.get_entity_by_name = AsyncMock(return_value=subject_node)
    svc.get_edges_for_node = AsyncMock(return_value=[])
    svc.recall = AsyncMock(return_value=[])
    svc.get_latest_episode_by_name = AsyncMock(return_value=None)
    svc._graphiti = MagicMock()
    svc._graphiti.add_episode = AsyncMock()
    svc._graphiti.driver = MagicMock()
    return svc


# --- Fake Anthropic client ---


class _Block(SimpleNamespace):
    """Stand-in for a content block from the Messages API."""


class _Response(SimpleNamespace):
    """Stand-in for a Messages API response."""


def _text_block(text: str) -> _Block:
    return _Block(type="text", text=text)


def _tool_use_block(tool_use_id: str, name: str, inp: dict | None = None) -> _Block:
    return _Block(type="tool_use", id=tool_use_id, name=name, input=inp or {})


def _finish_call(use_id: str = "fin", summary: str = "ok") -> list[_Block]:
    """One scripted iteration whose only block is a `finish` tool call."""
    return [_tool_use_block(use_id, "finish", {"summary": summary})]


class FakeClient:
    """Returns scripted responses via the streaming-shaped API.

    ``script`` is a flat list of iterations across the entire run — each
    iteration is the content blocks of one Anthropic streaming response.
    The orchestrator runs multiple passes; consume one iteration per
    ``messages.stream`` call regardless of pass boundary.
    """

    def __init__(self, script: list[list[_Block]]):
        self._script = list(script)
        self.calls: list[dict] = []

        fake = self

        class _StreamCtx:
            def __init__(self, response):
                self._response = response
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return None
            async def get_final_message(self):
                return self._response

        class _Messages:
            def stream(self, **kwargs):
                fake.calls.append(kwargs)
                if not fake._script:
                    raise AssertionError("FakeClient: script exhausted")
                content = fake._script.pop(0)
                response = _Response(
                    content=content,
                    stop_reason="end_turn" if not any(
                        getattr(b, "type", None) == "tool_use" for b in content
                    ) else "tool_use",
                )
                return _StreamCtx(response)

        self.messages = _Messages()


def _three_pass_finish_script() -> list[list[_Block]]:
    """Script three back-to-back finish calls — one each for Passes 1, 3, 4.

    Pass 2 is a Python driver and makes no agent-loop LLM calls (when
    no entries are eligible, which is the default for the test repo).
    """
    return [_finish_call("p1"), _finish_call("p3"), _finish_call("p4")]


def _empty_queue():
    """Mock work queue whose enqueue() returns a fake task_id."""
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="task-fake")
    return queue


@pytest.fixture
def no_candidates(monkeypatch):
    """Force the candidate scan to return an empty list, exercising
    Pass 1's fast-path skip."""
    async def _empty(*args, **kwargs):
        return []
    monkeypatch.setattr(agent_mod, "scan_repo_for_ingestion_candidates", _empty)
    # Also patch the import in pratyabhijna.synthesis_agent's namespace
    # for the call inside run_synthesis (deferred import target).
    from pratyabhijna import synthesis as synthesis_mod
    monkeypatch.setattr(synthesis_mod, "scan_repo_for_ingestion_candidates", _empty)
    return _empty


# --- _build_system_prompt ---


def test_build_system_prompt_includes_subject_and_subskill():
    prompt = _build_system_prompt("SUBSKILL BODY", "Vesper")
    assert "Vesper" in prompt
    assert "SUBSKILL BODY" in prompt
    assert "synthesis run" in prompt


# --- per-pass message builders ---


def test_pass1_message_lists_candidates():
    from pratyabhijna.synthesis import IngestionCandidate

    candidate = IngestionCandidate(
        relative_path="writing/p.md",
        absolute_path=Path("/tmp/p"),
        mtime=datetime(2026, 4, 13, tzinfo=timezone.utc),
        reason="new",
        latest_episode_at=None,
    )
    msg = _build_pass1_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        candidates=[candidate],
        last_ingestion_scan=None,
    )
    assert "Pass 1" in msg
    assert "writing/p.md" in msg
    assert "Identity atoms" not in msg  # Pass 1 doesn't carry atoms


def test_pass3_message_has_full_identity_set():
    atom = {
        "fact": "X holds view Y",
        "edge_uuid": "abcdef12-3456-7890-1234-567890abcdef",
        "node_name": "N",
        "node_type": "Observation",
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    }
    msg = _build_pass3_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        identity_files={"soul": "S", "identity": "I", "user": "U",
                        "threads": "T", "chronicle": "C"},
        synthesis_file="## Run Log",
        atoms=[atom],
        delta=[atom],
        last_context_rebuilt_at=None,
    )
    assert "Pass 3" in msg
    assert "Identity atoms" in msg
    assert "Delta since last context rebuild" in msg
    assert "SOUL.md" in msg
    assert "IDENTITY.md" in msg
    assert "X holds view Y" in msg


def test_pass4_message_focuses_on_synthesis_state():
    msg = _build_pass4_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        synthesis_file="## Run Log\n\n### 2026-04-13\nFirst run.",
    )
    assert "Pass 4" in msg
    assert "First run." in msg
    assert "Identity atoms" not in msg


def test_legacy_initial_message_still_renders():
    """Legacy union builder is preserved as a single-shot helper for
    callers that want everything in one message (used by some tests)."""
    msg = _build_initial_user_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        identity_files={"soul": "S"},
        synthesis_file=None,
        atoms=[],
        delta=[],
        candidates=[],
        last_context_rebuilt_at=None,
        last_ingestion_scan=None,
    )
    assert "Identity atoms" in msg


# --- _dispatch_tool_call ---


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_is_error():
    tu = _tool_use_block("id1", "nonexistent")
    result = await _dispatch_tool_call({}, tu)
    assert result["is_error"] is True
    assert "Unknown tool" in result["content"]
    assert result["tool_use_id"] == "id1"


@pytest.mark.asyncio
async def test_dispatch_success_shapes_result(service, config):
    tools = AgentTools(service=service, config=config)
    handlers = build_handler_map(tools)
    tu = _tool_use_block("id2", "finish", {"summary": "done"})

    result = await _dispatch_tool_call(handlers, tu)

    assert result.get("is_error") is not True
    assert result["tool_use_id"] == "id2"
    assert "acknowledged" in result["content"]
    assert tools.finished is True


@pytest.mark.asyncio
async def test_dispatch_tool_error_surfaces_to_model(service, config):
    tools = AgentTools(service=service, config=config)
    handlers = build_handler_map(tools)
    tu = _tool_use_block("id3", "read_file", {"path": "nonexistent"})

    result = await _dispatch_tool_call(handlers, tu)

    assert result["is_error"] is True
    assert "not a regular file" in result["content"]


@pytest.mark.asyncio
async def test_dispatch_unexpected_exception_caught(service, config):
    """Any non-ToolError exception from a tool is surfaced, not raised."""
    async def boom(**kwargs):
        raise RuntimeError("kablooey")
    handlers = {"bad": boom}
    tu = _tool_use_block("id4", "bad", {})

    result = await _dispatch_tool_call(handlers, tu)

    assert result["is_error"] is True
    assert "kablooey" in result["content"]


# --- run_synthesis: termination paths ---


@pytest.mark.asyncio
async def test_run_synthesis_no_subject_noop(service, config):
    service.get_entity_by_name = AsyncMock(return_value=None)
    client = FakeClient(script=[])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "no_subject"
    assert result["iterations"] == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_run_synthesis_skips_pass1_when_no_candidates(
    service, config, no_candidates
):
    """No ingestion candidates → Pass 1 is fast-path skipped.

    Pass 2 is also "skipped" in these tests because no queue is passed.
    """
    client = FakeClient(script=[_finish_call("p3"), _finish_call("p4")])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    pass1 = next(p for p in result["passes"] if p["pass"] == "pass1_ingestion")
    assert pass1["status"] == "skipped"
    # Two Anthropic calls — Pass 3 and Pass 4. Pass 1 was skipped (no
    # candidates), Pass 2 was skipped (no queue passed).
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_run_synthesis_dispatches_pass1_when_candidate_present(
    service, config, repo_with_candidate
):
    """When a candidate exists, all four pass entries appear in results.

    Pass 2 is "skipped" because no queue is passed; the rest complete.
    """
    config.resources.repo_path = str(repo_with_candidate)
    client = FakeClient(script=_three_pass_finish_script())

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    labels = [p["pass"] for p in result["passes"]]
    assert labels == ["pass1_ingestion", "pass2_maturation",
                      "pass3_bootstrap", "digest_rebuild", "pass4_maintenance"]
    by_label = {p["pass"]: p for p in result["passes"]}
    assert by_label["pass1_ingestion"]["status"] == "completed"
    assert by_label["pass2_maturation"]["status"] == "skipped"
    assert by_label["pass3_bootstrap"]["status"] == "completed"
    # No identity files in this harness → digest cleanly skips, does not
    # degrade the run (skip-not-fail contract, asserted at orchestrator level).
    assert by_label["digest_rebuild"]["status"] == "completed"
    assert by_label["pass4_maintenance"]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_synthesis_records_pass_max_iterations_without_aborting(
    service, config, no_candidates
):
    """A pass hitting max_iterations is recorded; later passes still dispatch.

    Tested via Pass 3 here (Pass 2 is a Python driver, not subject to
    max_iterations).
    """
    over_limit = config.synthesis.max_iterations + 1
    script = (
        [[_tool_use_block(f"t{i}", "git_status", {})] for i in range(over_limit)]
        + [_finish_call("p4")]
    )
    client = FakeClient(script=script)

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "partial"
    pass3 = next(p for p in result["passes"] if p["pass"] == "pass3_bootstrap")
    assert pass3["status"] == "max_iterations"
    assert pass3["iterations"] == config.synthesis.max_iterations
    # Pass 4 still runs independently, despite Pass 3's failure.
    labels = [p["pass"] for p in result["passes"]]
    assert "pass4_maintenance" in labels
    assert next(p for p in result["passes"] if p["pass"] == "pass4_maintenance")["status"] == "completed"


@pytest.mark.asyncio
async def test_run_synthesis_records_no_finish_without_aborting(
    service, config, no_candidates
):
    """A pass that stops 'no_finish' is recorded; later passes still dispatch."""
    client = FakeClient(script=[
        [_text_block("just thinking")],     # Pass 3 — no_finish
        _finish_call("p4"),                 # Pass 4 — completes
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "partial"
    pass3 = next(p for p in result["passes"] if p["pass"] == "pass3_bootstrap")
    assert pass3["status"] == "no_finish"
    labels = [p["pass"] for p in result["passes"]]
    assert "pass4_maintenance" in labels


@pytest.mark.asyncio
async def test_run_synthesis_continues_when_pass1_raises(
    service, config, monkeypatch
):
    """Pass 1 raising in its message loop doesn't stop Passes 2/3/4."""
    from pratyabhijna.synthesis import IngestionCandidate

    # Force one candidate so Pass 1 dispatches.
    async def _one(*args, **kwargs):
        return [IngestionCandidate(
            relative_path="writing/dummy.md",
            absolute_path=None,
            mtime=datetime(2026, 5, 1, tzinfo=timezone.utc),
            reason="new",
            latest_episode_at=None,
        )]
    monkeypatch.setattr(agent_mod, "scan_repo_for_ingestion_candidates", _one)
    from pratyabhijna import synthesis as synthesis_mod
    monkeypatch.setattr(synthesis_mod, "scan_repo_for_ingestion_candidates", _one)

    # Wrap FakeClient so the first stream() call raises (Pass 1).
    inner = FakeClient(script=[_finish_call("p3"), _finish_call("p4")])

    class FailFirstClient:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._calls_made = 0
            outer = self
            class _Messages:
                def stream(self, **kwargs):
                    if outer._calls_made == 0:
                        outer._calls_made += 1
                        raise RuntimeError("simulated Pass 1 failure")
                    outer._calls_made += 1
                    return outer._wrapped.messages.stream(**kwargs)
            self.messages = _Messages()

    result = await run_synthesis(service, config, client=FailFirstClient(inner))

    assert result["status"] == "partial"
    by_label = {p["pass"]: p for p in result["passes"]}
    assert by_label["pass1_ingestion"]["status"] == "raised"
    assert "RuntimeError" in by_label["pass1_ingestion"]["error"]
    # Pass 2 is skipped (no queue passed) — counts as not-failed.
    assert by_label["pass2_maturation"]["status"] == "skipped"
    assert by_label["pass3_bootstrap"]["status"] == "completed"
    assert by_label["pass4_maintenance"]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_synthesis_records_max_tokens_per_turn_without_aborting(
    service, config, no_candidates
):
    """A response truncated by max_tokens is recorded; later passes still dispatch.

    Tested via Pass 3 here (Pass 2 is a Python driver, not subject to
    per-turn truncation in the agent loop).
    """
    inner = FakeClient(script=[
        # Pass 3 — content irrelevant; the wrapper will mark this stop_reason="max_tokens"
        [_tool_use_block("t1", "read_file", {"path": "memory/SOUL.md"})],
        # Pass 4
        _finish_call("p4"),
    ])

    class TruncateFirstClient:
        """Wraps FakeClient; forces stop_reason='max_tokens' on the first call only."""
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._calls_made = 0
            outer = self
            class _Messages:
                def stream(self, **kwargs):
                    ctx = outer._wrapped.messages.stream(**kwargs)
                    if outer._calls_made == 0:
                        outer._calls_made += 1
                        original_get = ctx.get_final_message
                        async def _truncated_get():
                            response = await original_get()
                            response.stop_reason = "max_tokens"
                            return response
                        ctx.get_final_message = _truncated_get
                    else:
                        outer._calls_made += 1
                    return ctx
            self.messages = _Messages()

    result = await run_synthesis(service, config, client=TruncateFirstClient(inner))

    assert result["status"] == "partial"
    by_label = {p["pass"]: p for p in result["passes"]}
    assert by_label["pass3_bootstrap"]["status"] == "max_tokens_per_turn"
    assert by_label["pass3_bootstrap"]["iterations"] == 1
    # Pass 4 still runs.
    assert by_label["pass4_maintenance"]["status"] == "completed"


@pytest.mark.asyncio
async def test_pass4_skipped_when_checkout_to_main_fails(
    service, config, no_candidates, monkeypatch
):
    """If the orchestrator can't get HEAD onto main after Pass 3, Pass 4 is skipped."""
    from pratyabhijna import git_ops

    async def _on_synth_draft(repo_path):
        return "synth/draft"

    async def _checkout_fails(repo_path, ref):
        raise git_ops.GitError(
            cmd=["git", "checkout", "main"],
            returncode=1,
            stderr="error: Your local changes to the following files would be overwritten",
        )

    monkeypatch.setattr(git_ops, "current_branch", _on_synth_draft)
    monkeypatch.setattr(git_ops, "checkout", _checkout_fails)

    client = FakeClient(script=[
        _finish_call("p3"),
        # No Pass 4 entry — should never be invoked.
    ])

    result = await run_synthesis(service, config, client=client)

    by_label = {p["pass"]: p for p in result["passes"]}
    assert by_label["pass4_maintenance"]["status"] == "skipped"
    assert "checkout to main failed" in by_label["pass4_maintenance"]["reason"]
    # Pass 2 + Pass 4 are both "skipped" (different reasons), neither
    # counts as failed; Pass 3 completed; the run is "completed".
    assert by_label["pass3_bootstrap"]["status"] == "completed"
    assert result["status"] == "completed"


# --- run_synthesis: per-pass model + thinking selection ---


@pytest.mark.asyncio
async def test_pass3_alone_uses_synthesis_model(service, config):
    config.llm.synthesis_model = "claude-opus-4-7"
    config.llm.community_model = "claude-sonnet-4-6"
    client = FakeClient(script=_three_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # 3 calls in pass order: 1, 3, 4 (Pass 2 has no agent loop).
    # Sonnet, Opus, Sonnet.
    models = [c["model"] for c in client.calls]
    assert models == [
        "claude-sonnet-4-6", "claude-opus-4-7", "claude-sonnet-4-6",
    ]


@pytest.mark.asyncio
async def test_pass3_alone_gets_adaptive_thinking(service, config):
    config.synthesis.thinking.enabled = True
    config.synthesis.thinking.effort = "high"
    client = FakeClient(script=_three_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # Pass 1 (idx 0), Pass 3 (idx 1), Pass 4 (idx 2). Pass 2 has no
    # agent loop — no entry in client.calls.
    for i in (0, 2):
        assert "thinking" not in client.calls[i]
    assert client.calls[1]["thinking"] == {"type": "adaptive"}
    assert client.calls[1]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_thinking_disabled_omits_for_all_passes(service, config):
    config.synthesis.thinking.enabled = False
    client = FakeClient(script=_three_pass_finish_script())

    await run_synthesis(service, config, client=client)

    for call in client.calls:
        assert "thinking" not in call
        assert "output_config" not in call


@pytest.mark.asyncio
async def test_per_pass_tool_schemas_are_sliced(service, config):
    """Each agent-loop pass should send only its slice of TOOL_SCHEMAS.

    Pass 2 has no agent loop and is excluded.
    """
    client = FakeClient(script=_three_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # Calls in order: Pass 1, Pass 3, Pass 4 (Pass 2 makes no agent-loop call)
    p1_names = {t["name"] for t in client.calls[0]["tools"]}
    p3_names = {t["name"] for t in client.calls[1]["tools"]}
    p4_names = {t["name"] for t in client.calls[2]["tools"]}

    assert p1_names == set(PASS1_TOOL_NAMES)
    assert p3_names == set(PASS3_TOOL_NAMES)
    assert p4_names == set(PASS4_TOOL_NAMES)
    # Cross-checks: ingest_file only in Pass 1; recall in Pass 3 + 4;
    # status only in Pass 4; edit_file in Pass 3 + 4.
    assert "ingest_file" in p1_names and "ingest_file" not in p3_names
    assert "recall" in p3_names and "recall" in p4_names
    assert "status" in p4_names and "status" not in p3_names
    assert "edit_file" in p3_names and "edit_file" in p4_names


@pytest.mark.asyncio
async def test_pass3_opening_message_has_identity_files(service, config):
    client = FakeClient(script=_three_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # client.calls[1] is Pass 3 (idx 0=Pass 1, 1=Pass 3, 2=Pass 4 —
    # Pass 2 makes no agent-loop call).
    opening = " ".join(
        block["text"]
        for block in client.calls[1]["messages"][0]["content"]
        if block.get("type") == "text"
    )
    assert "Pass 3" in opening
    assert "SOUL.md" in opening
    assert "IDENTITY.md" in opening


# --- Remote sync behavior ---


@pytest.mark.asyncio
async def test_run_synthesis_no_op_sync_when_no_remote(service, config, monkeypatch):
    from pratyabhijna import git_ops

    fetch_calls = []

    async def record_fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))

    monkeypatch.setattr(git_ops, "fetch", record_fetch)

    client = FakeClient(script=_three_pass_finish_script())

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    assert fetch_calls == []  # no remote → no fetch


@pytest.mark.asyncio
async def test_run_synthesis_syncs_and_pushes_with_remote(
    service, config, repo, monkeypatch, tmp_path
):
    from pratyabhijna import git_ops

    remote = tmp_path / "bare.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main"],
        cwd=str(remote), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(repo), check=True, capture_output=True,
    )

    fetch_called = []
    push_calls = []

    real_fetch = git_ops.fetch
    real_push = git_ops.push

    async def recording_fetch(*args, **kwargs):
        fetch_called.append(True)
        await real_fetch(*args, **kwargs)

    async def recording_push(*args, **kwargs):
        push_calls.append((args, kwargs))
        await real_push(*args, **kwargs)

    monkeypatch.setattr(git_ops, "fetch", recording_fetch)
    monkeypatch.setattr(git_ops, "push", recording_push)

    client = FakeClient(script=_three_pass_finish_script())

    await run_synthesis(service, config, client=client)

    assert fetch_called
    pushed_branches = {args[1] for args, _ in push_calls}
    assert "main" in pushed_branches


@pytest.mark.asyncio
async def test_sync_failure_does_not_block_run(service, config, repo):
    """Broken remote: sync logs but doesn't block; passes still run."""
    subprocess.run(
        ["git", "remote", "add", "origin", "/nonexistent/remote.git"],
        cwd=str(repo), check=True, capture_output=True,
    )

    client = FakeClient(script=_three_pass_finish_script())

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_end_of_run_stamp_uses_fresh_subject_node(
    service, config, no_candidates
):
    """End-of-run ``synthesis_run_started_at`` stamp refetches the subject
    node so it doesn't clobber attributes written by Pass 4 (notably
    ``context_rebuilt_at``)."""
    run_start_node = MagicMock()
    run_start_node.attributes = {}
    run_start_node.labels = ["Person"]
    run_start_node.load_name_embedding = AsyncMock()
    run_start_node.save = AsyncMock()

    fresh_node = MagicMock()
    # Pass 4 has already stamped context_rebuilt_at on this fresh fetch.
    fresh_node.attributes = {"context_rebuilt_at": "2026-04-13T12:00:00+00:00"}
    fresh_node.labels = ["Person"]
    fresh_node.load_name_embedding = AsyncMock()
    fresh_node.save = AsyncMock()

    # First fetch (run-start) returns the stale snapshot; subsequent
    # fetches (the stamp's refetch, plus any bookkeeping) return the
    # post-Pass-4 fresh copy.
    service.get_entity_by_name = AsyncMock(
        side_effect=[run_start_node] + [fresh_node] * 10
    )

    client = FakeClient(script=[_finish_call("p3"), _finish_call("p4")])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    # The stamp must have written to the fresh node, not the stale one.
    fresh_node.save.assert_awaited()
    run_start_node.save.assert_not_awaited()
    # And the stamp must not have wiped context_rebuilt_at off the
    # fresh node — both attributes coexist after the stamp.
    assert fresh_node.attributes["context_rebuilt_at"] == (
        "2026-04-13T12:00:00+00:00"
    )
    assert "synthesis_run_started_at" in fresh_node.attributes


@pytest.mark.asyncio
async def test_partial_run_does_not_stamp_run_start(
    service, config, no_candidates
):
    """A partial run must not advance ``synthesis_run_started_at``.

    The stamp is the cutoff for both subject_delta and new_episodes
    counts in status/bootstrap. Stamping after a failed pass would
    silently drop the content that pass didn't manage to integrate
    out of the next run's window.
    """
    prior_stamp = "2026-04-13T11:00:00+00:00"

    run_start_node = MagicMock()
    run_start_node.attributes = {"synthesis_run_started_at": prior_stamp}
    run_start_node.labels = ["Person"]
    run_start_node.load_name_embedding = AsyncMock()
    run_start_node.save = AsyncMock()

    fresh_node = MagicMock()
    fresh_node.attributes = {"synthesis_run_started_at": prior_stamp}
    fresh_node.labels = ["Person"]
    fresh_node.load_name_embedding = AsyncMock()
    fresh_node.save = AsyncMock()

    service.get_entity_by_name = AsyncMock(
        side_effect=[run_start_node] + [fresh_node] * 10
    )

    # Pass 3 stops with no_finish (just thinking, never calls finish)
    # → status="no_finish" → run-level status="partial".
    client = FakeClient(script=[
        [_text_block("just thinking")],     # Pass 3 — no_finish
        _finish_call("p4"),                 # Pass 4 — completes
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "partial"
    # Neither node should have been saved with a new stamp.
    run_start_node.save.assert_not_awaited()
    fresh_node.save.assert_not_awaited()
    # The prior stamp must be untouched on whichever node still holds it.
    assert run_start_node.attributes["synthesis_run_started_at"] == prior_stamp
    assert fresh_node.attributes["synthesis_run_started_at"] == prior_stamp
