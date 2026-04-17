"""Unit tests for the synthesis agent's tool implementations.

The agent loop itself (Anthropic Messages API call, message-passing,
termination) is tested separately. Here we verify each tool acts on the
filesystem, git repo, and service in the expected way.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna.synthesis_agent import (
    TOOL_SCHEMAS,
    AgentTools,
    ToolError,
    build_handler_map,
    tool_schema_names,
)


# --- Test fixtures ---


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
    return repo_path


@pytest.fixture
def config(repo):
    cfg = MagicMock()
    cfg.resources.repo_path = str(repo)
    cfg.subject_name = "TestSubject"
    return cfg


@pytest.fixture
def service():
    svc = MagicMock()
    svc.recall = AsyncMock(return_value=[{"fact": "x"}])
    svc.get_entity_by_name = AsyncMock()
    svc.entity_types = {}
    svc.config = MagicMock(subject_name="TestSubject")
    svc._graphiti = MagicMock()
    svc._graphiti.add_episode = AsyncMock()
    svc._graphiti.remove_episode = AsyncMock()
    svc._graphiti.driver = MagicMock()
    return svc


@pytest.fixture
def tools(service, config):
    return AgentTools(service=service, config=config)


# --- Schema / handler map consistency ---


def test_handler_map_covers_all_schemas(tools):
    handlers = build_handler_map(tools)
    assert set(handlers.keys()) == tool_schema_names()


def test_all_schemas_have_required_fields():
    for schema in TOOL_SCHEMAS:
        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema
        assert schema["input_schema"].get("type") == "object"


# --- Path resolution / security ---


@pytest.mark.asyncio
async def test_read_file_reads_existing_file(tools, repo):
    (repo / "writing").mkdir()
    (repo / "writing" / "solo.md").write_text("hello")

    result = await tools.read_file("writing/solo.md")

    assert result["content"] == "hello"
    assert result["path"] == "writing/solo.md"


@pytest.mark.asyncio
async def test_read_file_rejects_missing(tools):
    with pytest.raises(ToolError, match="not a regular file"):
        await tools.read_file("nope.md")


@pytest.mark.asyncio
async def test_read_file_rejects_absolute_path(tools):
    with pytest.raises(ToolError, match="invalid path"):
        await tools.read_file("/etc/passwd")


@pytest.mark.asyncio
async def test_read_file_rejects_parent_escape(tools):
    with pytest.raises(ToolError, match="invalid path"):
        await tools.read_file("../secret")


@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tools, repo):
    await tools.write_file("new/dir/file.md", "content")
    assert (repo / "new" / "dir" / "file.md").read_text() == "content"


@pytest.mark.asyncio
async def test_write_file_rejects_escape(tools):
    with pytest.raises(ToolError):
        await tools.write_file("../outside.md", "x")


@pytest.mark.asyncio
async def test_write_file_overwrites(tools, repo):
    (repo / "x.md").write_text("old")
    await tools.write_file("x.md", "new")
    assert (repo / "x.md").read_text() == "new"


# --- Git tools ---


@pytest.mark.asyncio
async def test_git_status_returns_branch_and_dirty(tools, repo):
    result = await tools.git_status()
    assert result["branch"] == "main"
    assert result["dirty"] is False

    (repo / "untracked.md").write_text("x")
    result = await tools.git_status()
    assert result["dirty"] is True


@pytest.mark.asyncio
async def test_git_branch_exists(tools):
    assert (await tools.git_branch_exists("main"))["exists"] is True
    assert (await tools.git_branch_exists("nope"))["exists"] is False


@pytest.mark.asyncio
async def test_git_create_and_checkout(tools):
    await tools.git_create_branch("synth/draft", base="main")
    assert (await tools.git_status())["branch"] == "synth/draft"
    await tools.git_checkout("main")
    assert (await tools.git_status())["branch"] == "main"


@pytest.mark.asyncio
async def test_git_add_and_commit(tools, repo):
    (repo / "a.md").write_text("a")
    result = await tools.git_add_and_commit(["a.md"], "add a")
    assert len(result["sha"]) == 40
    assert result["message"] == "add a"


@pytest.mark.asyncio
async def test_git_add_and_commit_rejects_empty_paths(tools):
    with pytest.raises(ToolError):
        await tools.git_add_and_commit([], "msg")


@pytest.mark.asyncio
async def test_git_add_and_commit_rejects_escape(tools):
    with pytest.raises(ToolError):
        await tools.git_add_and_commit(["../x"], "msg")


@pytest.mark.asyncio
async def test_git_add_and_commit_fails_on_nothing_staged(tools):
    with pytest.raises(ToolError, match="commit failed"):
        await tools.git_add_and_commit(["README.md"], "nothing changed")


@pytest.mark.asyncio
async def test_git_diff_returns_text(tools, repo):
    await tools.git_create_branch("feature", base="main")
    (repo / "f.md").write_text("feature content\n")
    await tools.git_add_and_commit(["f.md"], "add f")

    result = await tools.git_diff("main", "feature")
    assert "f.md" in result["diff"]
    assert "+feature content" in result["diff"]


@pytest.mark.asyncio
async def test_git_rebase_abort_safe_when_none_running(tools):
    result = await tools.git_rebase_abort()
    assert result["aborted"] is True


# --- Recall ---


@pytest.mark.asyncio
async def test_recall_delegates_to_service(tools, service):
    result = await tools.recall("my query", memory_type="Observation", limit=5)
    service.recall.assert_awaited_once_with(
        query="my query", memory_type="Observation", limit=5
    )
    assert result["results"] == [{"fact": "x"}]


# --- Ingestion ---


@pytest.mark.asyncio
async def test_ingest_file_calls_add_episode(tools, service, repo):
    (repo / "writing").mkdir()
    (repo / "writing" / "piece.md").write_text("the piece")

    fake_result = MagicMock()
    fake_result.episode.uuid = "00000000-0000-0000-0000-000000000001"
    service._graphiti.add_episode.return_value = fake_result

    await tools.ingest_file("writing/piece.md")

    service._graphiti.add_episode.assert_awaited_once()
    kwargs = service._graphiti.add_episode.await_args.kwargs
    assert kwargs["name"] == "writing/piece.md"
    assert kwargs["episode_body"] == "the piece"
    assert "writing/piece.md" in kwargs["source_description"]
    assert kwargs["group_id"] == "TestSubject"


@pytest.mark.asyncio
async def test_ingest_file_rejects_missing(tools):
    with pytest.raises(ToolError):
        await tools.ingest_file("nope.md")


@pytest.mark.asyncio
async def test_ingest_file_returns_episode_uuid(tools, service, repo):
    """ingest_file returns episode_uuid from the add_episode result."""
    from unittest.mock import MagicMock

    (repo / "writing").mkdir()
    (repo / "writing" / "solo-1.md").write_text("session one")

    fake_result = MagicMock()
    fake_result.episode.uuid = "11111111-2222-3333-4444-555555555555"
    service._graphiti.add_episode.return_value = fake_result

    result = await tools.ingest_file("writing/solo-1.md")
    assert result["episode_uuid"] == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_ingest_file_passes_saga(tools, service, repo):
    """ingest_file forwards saga name to add_episode."""
    from unittest.mock import MagicMock

    (repo / "writing").mkdir(exist_ok=True)
    (repo / "writing" / "solo-2.md").write_text("session two")

    fake_result = MagicMock()
    fake_result.episode.uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    service._graphiti.add_episode.return_value = fake_result

    await tools.ingest_file("writing/solo-2.md", saga="solo-sessions")

    kwargs = service._graphiti.add_episode.await_args.kwargs
    assert kwargs["saga"] == "solo-sessions"
    assert kwargs["saga_previous_episode_uuid"] is None


@pytest.mark.asyncio
async def test_ingest_file_passes_saga_chain(tools, service, repo):
    """ingest_file forwards saga_previous_episode_uuid for ordering."""
    from unittest.mock import MagicMock

    (repo / "writing").mkdir(exist_ok=True)
    (repo / "writing" / "solo-3.md").write_text("session three")

    prev_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    fake_result = MagicMock()
    fake_result.episode.uuid = "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"
    service._graphiti.add_episode.return_value = fake_result

    result = await tools.ingest_file(
        "writing/solo-3.md",
        saga="solo-sessions",
        saga_previous_episode_uuid=prev_uuid,
    )

    kwargs = service._graphiti.add_episode.await_args.kwargs
    assert kwargs["saga"] == "solo-sessions"
    assert kwargs["saga_previous_episode_uuid"] == prev_uuid
    assert result["episode_uuid"] == "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"


# --- Deletion ---


@pytest.mark.asyncio
async def test_forget_episode_calls_remove_episode(tools, service):
    await tools.forget_episode("aaaa-bbbb")
    service._graphiti.remove_episode.assert_awaited_once_with("aaaa-bbbb")


@pytest.mark.asyncio
async def test_forget_episode_returns_uuid(tools, service):
    result = await tools.forget_episode("aaaa-bbbb")
    assert result == {"deleted": True, "uuid": "aaaa-bbbb"}


@pytest.mark.asyncio
async def test_forget_episode_raises_tool_error_on_not_found(tools, service):
    from graphiti_core.errors import NodeNotFoundError
    service._graphiti.remove_episode.side_effect = NodeNotFoundError("aaaa-bbbb")
    with pytest.raises(ToolError, match="not found"):
        await tools.forget_episode("aaaa-bbbb")


# --- Metadata ---


@pytest.mark.asyncio
async def test_update_synthesis_metadata_sets_flags(tools, service):
    node = MagicMock()
    node.attributes = {}
    node.load_name_embedding = AsyncMock()
    node.save = AsyncMock()
    service.get_entity_by_name = AsyncMock(return_value=node)

    result = await tools.update_synthesis_metadata(
        context_rebuilt_at=True, last_ingestion_scan=True
    )

    assert "context_rebuilt_at" in node.attributes
    assert "last_ingestion_scan" in node.attributes
    assert "context_rebuilt_at" in result
    assert "last_ingestion_scan" in result
    node.load_name_embedding.assert_awaited_once()
    node.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_synthesis_metadata_only_sets_what_was_requested(tools, service):
    node = MagicMock()
    node.attributes = {}
    node.load_name_embedding = AsyncMock()
    node.save = AsyncMock()
    service.get_entity_by_name = AsyncMock(return_value=node)

    await tools.update_synthesis_metadata(context_rebuilt_at=True)

    assert "context_rebuilt_at" in node.attributes
    assert "last_ingestion_scan" not in node.attributes


@pytest.mark.asyncio
async def test_update_synthesis_metadata_no_op_when_no_subject(tools, service):
    service.get_entity_by_name = AsyncMock(return_value=None)
    with pytest.raises(ToolError, match="subject node not found"):
        await tools.update_synthesis_metadata(context_rebuilt_at=True)


# --- Control ---


@pytest.mark.asyncio
async def test_finish_sets_state(tools):
    result = await tools.finish("ran cleanly")
    assert result["acknowledged"] is True
    assert tools.finished is True
    assert tools.summary == "ran cleanly"


# --- Config guards ---


@pytest.mark.asyncio
async def test_missing_repo_path_raises(service):
    cfg = MagicMock()
    cfg.resources.repo_path = ""
    t = AgentTools(service=service, config=cfg)
    with pytest.raises(ToolError, match="repo_path"):
        await t.read_file("anything.md")
