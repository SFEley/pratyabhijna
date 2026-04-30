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
    PASS2_TOOL_NAMES,
    PASS3_TOOL_NAMES,
    PASS4_TOOL_NAMES,
    ToolError,
    _build_initial_user_message,
    _build_pass1_message,
    _build_pass2_message,
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


def _four_pass_finish_script() -> list[list[_Block]]:
    """Script four back-to-back finish calls — one per pass."""
    return [_finish_call("p1"), _finish_call("p2"),
            _finish_call("p3"), _finish_call("p4")]


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


def test_pass2_message_includes_chronicle_and_threads_only():
    msg = _build_pass2_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        chronicle_text="## April 1, 2026 — entry\nbody",
        threads_text="### thread A\nbody",
    )
    assert "Pass 2" in msg
    assert "April 1, 2026" in msg
    assert "thread A" in msg
    assert "Identity atoms" not in msg
    assert "SOUL.md" not in msg


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
    """No ingestion candidates → Pass 1 is fast-path skipped."""
    client = FakeClient(script=[
        _finish_call("p2"), _finish_call("p3"), _finish_call("p4"),
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    pass1 = next(p for p in result["passes"] if p["pass"] == "pass1_ingestion")
    assert pass1["status"] == "skipped"
    # Three Anthropic calls — one per non-skipped pass.
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_run_synthesis_dispatches_pass1_when_candidate_present(
    service, config, repo_with_candidate
):
    config.resources.repo_path = str(repo_with_candidate)
    client = FakeClient(script=[
        _finish_call("p1"),
        _finish_call("p2"),
        _finish_call("p3"),
        _finish_call("p4"),
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    labels = [p["pass"] for p in result["passes"]]
    assert labels == ["pass1_ingestion", "pass2_maturation",
                      "pass3_bootstrap", "pass4_maintenance"]
    assert all(p["status"] == "completed" for p in result["passes"])


@pytest.mark.asyncio
async def test_run_synthesis_dispatches_tool_then_finishes_in_pass2(
    service, config, no_candidates
):
    """Pass 2 uses one tool call before finishing; Pass 1 skips, Passes 3/4 finish."""
    client = FakeClient(script=[
        # Pass 2 takes two iterations
        [_tool_use_block("p2-1", "git_status", {})],
        _finish_call("p2"),
        # Pass 3 + Pass 4
        _finish_call("p3"),
        _finish_call("p4"),
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    assert result["iterations"] == 4  # 2 (Pass 2) + 1 (Pass 3) + 1 (Pass 4)


@pytest.mark.asyncio
async def test_run_synthesis_aborts_on_pass_max_iterations(
    service, config, no_candidates
):
    """A pass hitting max_iterations aborts the run; later passes don't dispatch."""
    # Pass 2 keeps calling git_status forever — never finishes (Pass 1 skipped).
    over_limit = config.synthesis.max_iterations + 1
    script = [[_tool_use_block(f"t{i}", "git_status", {})] for i in range(over_limit)]
    client = FakeClient(script=script)

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "aborted"
    pass2 = next(p for p in result["passes"] if p["pass"] == "pass2_maturation")
    assert pass2["status"] == "max_iterations"
    assert pass2["iterations"] == config.synthesis.max_iterations
    labels = [p["pass"] for p in result["passes"]]
    assert "pass3_bootstrap" not in labels
    assert "pass4_maintenance" not in labels


@pytest.mark.asyncio
async def test_run_synthesis_stops_on_no_tools_no_finish(
    service, config, no_candidates
):
    """A pass that produces text only with no tool_use stops 'no_finish' and aborts."""
    client = FakeClient(script=[
        [_text_block("just thinking")],
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "aborted"
    pass2 = next(p for p in result["passes"] if p["pass"] == "pass2_maturation")
    assert pass2["status"] == "no_finish"


# --- run_synthesis: per-pass model + thinking selection ---


@pytest.mark.asyncio
async def test_pass3_alone_uses_synthesis_model(service, config):
    config.llm.synthesis_model = "claude-opus-4-7"
    config.llm.community_model = "claude-sonnet-4-6"
    client = FakeClient(script=_four_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # 4 calls in pass order: 1, 2, 3, 4 → Sonnet, Sonnet, Opus, Sonnet
    models = [c["model"] for c in client.calls]
    assert models == [
        "claude-sonnet-4-6", "claude-sonnet-4-6",
        "claude-opus-4-7", "claude-sonnet-4-6",
    ]


@pytest.mark.asyncio
async def test_pass3_alone_gets_adaptive_thinking(service, config):
    config.synthesis.thinking.enabled = True
    config.synthesis.thinking.effort = "high"
    client = FakeClient(script=_four_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # Pass 1 (idx 0), Pass 2 (idx 1), Pass 3 (idx 2), Pass 4 (idx 3)
    for i in (0, 1, 3):
        assert "thinking" not in client.calls[i]
    assert client.calls[2]["thinking"] == {"type": "adaptive"}
    assert client.calls[2]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_thinking_disabled_omits_for_all_passes(service, config):
    config.synthesis.thinking.enabled = False
    client = FakeClient(script=_four_pass_finish_script())

    await run_synthesis(service, config, client=client)

    for call in client.calls:
        assert "thinking" not in call
        assert "output_config" not in call


@pytest.mark.asyncio
async def test_per_pass_tool_schemas_are_sliced(service, config):
    """Each pass should send only its slice of TOOL_SCHEMAS."""
    client = FakeClient(script=_four_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # Calls in order: Pass 1, Pass 2, Pass 3, Pass 4
    p1_names = {t["name"] for t in client.calls[0]["tools"]}
    p2_names = {t["name"] for t in client.calls[1]["tools"]}
    p3_names = {t["name"] for t in client.calls[2]["tools"]}
    p4_names = {t["name"] for t in client.calls[3]["tools"]}

    assert p1_names == set(PASS1_TOOL_NAMES)
    assert p2_names == set(PASS2_TOOL_NAMES)
    assert p3_names == set(PASS3_TOOL_NAMES)
    assert p4_names == set(PASS4_TOOL_NAMES)
    # Cross-checks: ingest_file only in Pass 1; remember only in Pass 2;
    # recall only in Pass 3; status only in Pass 4.
    assert "ingest_file" in p1_names and "ingest_file" not in p2_names
    assert "remember" in p2_names and "remember" not in p3_names
    assert "recall" in p3_names and "recall" not in p2_names
    assert "status" in p4_names and "status" not in p3_names


@pytest.mark.asyncio
async def test_pass3_opening_message_has_identity_files(service, config):
    client = FakeClient(script=_four_pass_finish_script())

    await run_synthesis(service, config, client=client)

    # client.calls[2] is Pass 3 (idx 0=Pass 1, 1=Pass 2, 2=Pass 3, 3=Pass 4)
    opening = " ".join(
        block["text"]
        for block in client.calls[2]["messages"][0]["content"]
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

    client = FakeClient(script=_four_pass_finish_script())

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

    client = FakeClient(script=_four_pass_finish_script())

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

    client = FakeClient(script=_four_pass_finish_script())

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
