"""Tests for the synthesis agent loop.

The loop itself — message threading, tool dispatch, termination — is
tested with a scripted fake Anthropic client. Real Opus calls are
expensive and flaky for CI; use ``--live`` integration tests for
real-service confidence.
"""

from __future__ import annotations

import subprocess  # noqa: F401 — used by the remote-sync tests below
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna import synthesis_agent as agent_mod
from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.synthesis_agent import (
    _build_initial_user_message,
    _build_system_prompt,
    _dispatch_tool_call,
    AgentTools,
    ToolError,
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
    # Also create identity files so bootstrap has something to pass in.
    memory = repo_path / "memory"
    memory.mkdir()
    for name in ("SOUL.md", "IDENTITY.md", "USER.md", "THREADS.md", "CHRONICLE.md"):
        (memory / name).write_text(f"# {name}\ncontent\n")
    return repo_path


@pytest.fixture
def config(repo):
    c = PratyabhijnaConfig()
    c.subject_name = "TestSubject"
    c.resources.repo_path = str(repo)
    # Make the loop terminate quickly in failure modes
    c.synthesis.max_iterations = 5
    # Don't hit thinking in tests — smaller max_tokens, no thinking param
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


class FakeClient:
    """Returns scripted responses via a streaming-shaped API.

    ``messages.stream(**kwargs)`` returns an async context manager; on
    entry it yields a stream object whose ``get_final_message()`` returns
    the next scripted response. Matches Anthropic's streaming shape
    since ``run_synthesis`` uses ``messages.stream`` (required by the
    SDK when ``max_tokens`` would exceed the non-streaming timeout).
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


# --- _build_system_prompt ---


def test_build_system_prompt_includes_subject_and_subskill():
    prompt = _build_system_prompt("SUBSKILL BODY", "Vesper")
    assert "Vesper" in prompt
    assert "SUBSKILL BODY" in prompt
    assert "synthesis run" in prompt


# --- _build_initial_user_message ---


def test_initial_message_has_all_sections():
    msg = _build_initial_user_message(
        subject_name="TestSubject",
        now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        identity_files={"soul": "S", "identity": "I", "user": "U",
                        "threads": "T", "chronicle": "C"},
        synthesis_file=None,
        atoms=[],
        delta=[],
        candidates=[],
        last_context_rebuilt_at=None,
        last_ingestion_scan=None,
    )
    assert "TestSubject" in msg
    assert "Ingestion candidates" in msg
    assert "Identity atoms" in msg
    assert "Delta since last context rebuild" in msg
    assert "SOUL.md" in msg
    assert "IDENTITY.md" in msg
    assert "USER.md" in msg
    assert "THREADS.md" in msg
    assert "CHRONICLE.md" in msg


def test_initial_message_shows_synthesis_file():
    msg = _build_initial_user_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        identity_files={},
        synthesis_file="## Run Log\n\n### 2026-04-13\nFirst run.",
        atoms=[],
        delta=[],
        candidates=[],
        last_context_rebuilt_at=None,
        last_ingestion_scan=None,
    )
    assert "SYNTHESIS.md" in msg
    assert "First run." in msg


def test_initial_message_renders_candidates_and_atoms():
    from pratyabhijna.synthesis import IngestionCandidate

    candidate = IngestionCandidate(
        relative_path="writing/p.md",
        absolute_path=Path("/tmp/p"),
        mtime=datetime(2026, 4, 13, tzinfo=timezone.utc),
        reason="new",
        latest_episode_at=None,
    )
    atom = {
        "fact": "X holds view Y",
        "edge_uuid": "abcdef12-3456-7890-1234-567890abcdef",
        "node_name": "N",
        "node_type": "Observation",
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    }
    msg = _build_initial_user_message(
        subject_name="X",
        now=datetime(2026, 4, 13, tzinfo=timezone.utc),
        git_branch="main",
        git_dirty=False,
        identity_files={},
        synthesis_file=None,
        atoms=[atom],
        delta=[atom],
        candidates=[candidate],
        last_context_rebuilt_at=None,
        last_ingestion_scan=None,
    )
    assert "writing/p.md" in msg
    assert "[new]" in msg
    assert "X holds view Y" in msg
    assert "[Observation]" in msg


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
async def test_run_synthesis_no_subject_noop(service, config, monkeypatch):
    service.get_entity_by_name = AsyncMock(return_value=None)
    client = FakeClient(script=[])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "no_subject"
    assert result["iterations"] == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_run_synthesis_finishes_on_first_call(service, config):
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "nothing to do"})],
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    assert result["summary"] == "nothing to do"
    assert result["iterations"] == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_run_synthesis_dispatches_tool_then_finishes(service, config):
    client = FakeClient(script=[
        [_tool_use_block("t1", "git_status", {})],
        [_tool_use_block("t2", "finish", {"summary": "ok"})],
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    assert result["iterations"] == 2

    # Second call should include the tool_result in messages
    second_messages = client.calls[1]["messages"]
    last_user_turn = [m for m in second_messages if m["role"] == "user"][-1]
    # Tool results come back as a list of dicts under "content"
    assert isinstance(last_user_turn["content"], list)
    assert any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in last_user_turn["content"]
    )


@pytest.mark.asyncio
async def test_run_synthesis_max_iterations(service, config):
    # Agent keeps calling a tool forever, never calling finish
    script = [[_tool_use_block(f"t{i}", "git_status", {})]
              for i in range(config.synthesis.max_iterations + 5)]
    client = FakeClient(script=script)

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "max_iterations"
    assert result["iterations"] == config.synthesis.max_iterations


@pytest.mark.asyncio
async def test_run_synthesis_stops_on_no_tools_no_finish(service, config):
    client = FakeClient(script=[
        [_text_block("I'm just going to sit here and think out loud.")],
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "no_finish"
    assert result["iterations"] == 1


@pytest.mark.asyncio
async def test_run_synthesis_uses_adaptive_thinking_when_enabled(service, config):
    config.synthesis.thinking.enabled = True
    config.synthesis.thinking.effort = "high"
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    await run_synthesis(service, config, client=client)

    assert client.calls[0].get("thinking") == {"type": "adaptive"}
    assert client.calls[0].get("output_config") == {"effort": "high"}


@pytest.mark.asyncio
async def test_run_synthesis_passes_configured_effort(service, config):
    config.synthesis.thinking.enabled = True
    config.synthesis.thinking.effort = "medium"
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    await run_synthesis(service, config, client=client)

    assert client.calls[0]["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_run_synthesis_omits_thinking_when_disabled(service, config):
    config.synthesis.thinking.enabled = False
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    await run_synthesis(service, config, client=client)

    assert "thinking" not in client.calls[0]
    assert "output_config" not in client.calls[0]


@pytest.mark.asyncio
async def test_run_synthesis_uses_configured_model(service, config):
    config.llm.synthesis_model = "claude-opus-4-6"
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    await run_synthesis(service, config, client=client)

    assert client.calls[0]["model"] == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_run_synthesis_system_prompt_includes_subskill(service, config):
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    await run_synthesis(service, config, client=client)

    system = " ".join(
        block["text"] for block in client.calls[0]["system"] if block.get("type") == "text"
    )
    assert "TestSubject" in system
    # Subskill content markers
    assert "synthesizer" in system.lower()


@pytest.mark.asyncio
async def test_run_synthesis_opening_message_has_identity_files(
    service, config, repo
):
    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    await run_synthesis(service, config, client=client)

    opening = " ".join(
        block["text"]
        for block in client.calls[0]["messages"][0]["content"]
        if block.get("type") == "text"
    )
    assert "SOUL.md" in opening
    assert "IDENTITY.md" in opening


# --- Remote sync behavior ---


@pytest.mark.asyncio
async def test_run_synthesis_no_op_sync_when_no_remote(
    service, config, repo, monkeypatch
):
    """Without a remote, sync steps should be silent no-ops."""
    from pratyabhijna import git_ops

    fetch_calls = []

    async def record_fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))

    monkeypatch.setattr(git_ops, "fetch", record_fetch)

    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "x"})],
    ])

    result = await run_synthesis(service, config, client=client)

    assert result["status"] == "completed"
    assert fetch_calls == []  # no remote → no fetch


@pytest.mark.asyncio
async def test_run_synthesis_syncs_and_pushes_with_remote(
    service, config, repo, monkeypatch, tmp_path
):
    """With a remote: fetch at start, push at end."""
    from pratyabhijna import git_ops

    # Add a bare remote so has_remote() returns True
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

    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "done"})],
    ])

    await run_synthesis(service, config, client=client)

    assert fetch_called  # sync_from_remote fetched
    # push was called for main at end (draft branch doesn't exist)
    pushed_branches = {args[1] for args, _ in push_calls}
    assert "main" in pushed_branches


@pytest.mark.asyncio
async def test_sync_failure_does_not_block_run(
    service, config, repo, monkeypatch, tmp_path
):
    """If the fetch raises, the run should still proceed with local state."""
    from pratyabhijna import git_ops

    # Add a broken remote so has_remote returns True but fetch fails
    subprocess.run(
        ["git", "remote", "add", "origin", "/nonexistent/remote.git"],
        cwd=str(repo), check=True, capture_output=True,
    )

    client = FakeClient(script=[
        [_tool_use_block("t1", "finish", {"summary": "ok"})],
    ])

    result = await run_synthesis(service, config, client=client)

    # Broken remote shouldn't have prevented the run from completing.
    assert result["status"] == "completed"
