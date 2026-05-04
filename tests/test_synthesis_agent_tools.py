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
    PASS1_TOOL_NAMES,
    PASS1_TOOL_SCHEMAS,
    PASS3_TOOL_NAMES,
    PASS3_TOOL_SCHEMAS,
    PASS4_TOOL_NAMES,
    PASS4_TOOL_SCHEMAS,
    TOOL_SCHEMAS,
    build_pass_handlers,
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
    svc.remove_episode = AsyncMock()
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


# --- Per-pass slice consistency ---
#
# Each pass exposes only the tools it needs. The schema slice and the
# handler slice must agree, and every name in the slice must exist in the
# union TOOL_SCHEMAS / build_handler_map. Tested per pass below.


@pytest.mark.parametrize(
    ("names", "schemas", "label"),
    [
        # Pass 2 has no slice — it's a Python driver. Tested separately
        # (test_pass2_has_no_tool_slice).
        (PASS1_TOOL_NAMES, PASS1_TOOL_SCHEMAS, "pass1_ingestion"),
        (PASS3_TOOL_NAMES, PASS3_TOOL_SCHEMAS, "pass3_bootstrap"),
        (PASS4_TOOL_NAMES, PASS4_TOOL_SCHEMAS, "pass4_maintenance"),
    ],
)
def test_pass_schemas_match_pass_names(names, schemas, label):
    assert {s["name"] for s in schemas} == set(names), label


@pytest.mark.parametrize(
    "names",
    [PASS1_TOOL_NAMES, PASS3_TOOL_NAMES, PASS4_TOOL_NAMES],
)
def test_pass_handler_slice_matches_names(tools, names):
    handlers = build_pass_handlers(tools, names)
    assert set(handlers.keys()) == set(names)


def test_pass_names_subset_of_union():
    """No per-pass name should reference a tool not in TOOL_SCHEMAS."""
    union = {t["name"] for t in TOOL_SCHEMAS}
    for names in (PASS1_TOOL_NAMES, PASS3_TOOL_NAMES, PASS4_TOOL_NAMES):
        assert names <= union


def test_finish_present_in_every_agent_loop_pass():
    """`finish` is the termination signal — every subagent loop pass needs it.

    Pass 2 is excluded: it's a Python driver, not an agent loop, so it
    has no tool slice and no `finish` tool.
    """
    for names in (PASS1_TOOL_NAMES, PASS3_TOOL_NAMES, PASS4_TOOL_NAMES):
        assert "finish" in names


def test_subskill_prose_only_mentions_tools_each_pass_actually_has():
    """Regression guard against the recurring "subskill instructs the agent
    to use a tool that isn't in the pass's slice" bug.

    Three times in the past month a Pass N section's prose has referenced a
    tool not actually wired into PASS{N}_TOOL_NAMES — Pass 2's `remember`
    (PR #30/#31), Pass 4's `recall` (PR #33), Pass 3's `remember` exception
    in "What not to do" (caught pre-merge in #34). Each one resulted in the
    agent silently doing the wrong thing or nothing. This test parses
    `synthesis.md`, finds backtick-quoted tool references inside each Pass
    N section, and asserts they're in that pass's TOOL_NAMES.

    Tool name references in `code blocks` are detected; references in
    sentences like "use `recall` for X" are also caught. False positives
    (e.g. mentioning a tool by name in a comparative aside) are addressed
    by adding the tool to the slice OR rewording the prose.
    """
    import re
    from pathlib import Path
    import pratyabhijna.synthesis_agent as agent_mod

    skill_path = (
        Path(__file__).parent.parent
        / "skills" / "pratyabhijna" / "references" / "synthesis.md"
    )
    text = skill_path.read_text(encoding="utf-8")

    # Map heading → pass slice. "Pass 2" intentionally has an empty
    # allowed set since it's a Python driver — its prose should not
    # reference any agent-callable tool by name.
    pass_sections: dict[str, frozenset[str]] = {
        "Pass 1": PASS1_TOOL_NAMES,
        "Pass 2": frozenset(),
        "Pass 3": PASS3_TOOL_NAMES,
        "Pass 4": PASS4_TOOL_NAMES,
    }
    # Bracket each pass section: from its `## Pass N:` heading to the next
    # `## ` heading or EOF.
    section_text: dict[str, str] = {}
    for label in pass_sections:
        m = re.search(rf"^## {re.escape(label)}: ", text, re.MULTILINE)
        if not m:
            pytest.fail(f"could not find '## {label}: ' heading in synthesis.md")
        start = m.start()
        # Find the next top-level heading after this one.
        rest = text[m.end():]
        next_m = re.search(r"^## ", rest, re.MULTILINE)
        end = m.end() + (next_m.start() if next_m else len(rest))
        section_text[label] = text[start:end]

    # Universe of agent-callable tool names. Subskill references to names
    # outside this set (e.g. `add_episode`, `remember_tool`) are runtime
    # internals, not agent tools — exclude them.
    agent_tools = {t["name"] for t in agent_mod.TOOL_SCHEMAS}

    # Find backtick-quoted identifiers like `name` or `name(`. A name is
    # a Python-shaped identifier.
    pattern = re.compile(r"`([a-z_][a-z_0-9]*)\(?\)?`")
    failures: list[str] = []
    for label, allowed in pass_sections.items():
        body = section_text[label]
        mentioned = {m.group(1) for m in pattern.finditer(body)}
        # Filter to agent tool names.
        mentioned_tools = mentioned & agent_tools
        bad = mentioned_tools - allowed
        if bad:
            failures.append(
                f"{label} prose mentions agent tools not in its slice: "
                f"{sorted(bad)} (allowed: {sorted(allowed)})"
            )
    assert not failures, "\n".join(failures)


def test_pass2_has_no_tool_slice():
    """Pass 2 is a deterministic Python driver, not an agent loop.

    It should have no `PASS2_TOOL_NAMES` constant exposed from the module —
    importing it should fail. This is a regression guard against a future
    change accidentally turning Pass 2 back into a tool-using subagent
    without revisiting the cost analysis that drove v0.14.4.
    """
    import pratyabhijna.synthesis_agent as agent_mod
    assert not hasattr(agent_mod, "PASS2_TOOL_NAMES")
    assert not hasattr(agent_mod, "PASS2_TOOL_SCHEMAS")


def test_pass1_has_ingestion_pass3_has_recall_pass4_has_status():
    """Anchor each pass's distinguishing tool (regression guard).

    `recall` is shared between Pass 3 (bootstrap reasoning) and Pass 4
    (graph health check). `edit_file` is shared between Passes 3 and 4
    (file-edit primitive); Pass 1 doesn't edit files. Pass 2 is excluded
    — it's a Python driver, not an agent loop.
    """
    assert "ingest_file" in PASS1_TOOL_NAMES
    assert "ingest_file" not in PASS3_TOOL_NAMES
    assert "ingest_file" not in PASS4_TOOL_NAMES
    assert "recall" in PASS3_TOOL_NAMES
    assert "recall" in PASS4_TOOL_NAMES
    assert "status" in PASS4_TOOL_NAMES
    assert "status" not in PASS3_TOOL_NAMES
    assert "edit_file" in PASS3_TOOL_NAMES
    assert "edit_file" in PASS4_TOOL_NAMES
    assert "edit_file" not in PASS1_TOOL_NAMES


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
    from graphiti_core.search.search_config import SearchResults
    from graphiti_core.search.search_filters import SearchFilters

    service.recall = AsyncMock(return_value=SearchResults())

    result = await tools.recall("my query", memory_type="Observation")

    call_kwargs = service.recall.call_args.kwargs
    assert call_kwargs["query"] == "my query"
    assert call_kwargs["search_filter"] == SearchFilters(node_labels=["Observation"])
    assert result["results"] == []


@pytest.mark.asyncio
async def test_status_delegates_to_status_tool(tools, service, config, monkeypatch):
    """status tool wraps the existing pratyabhijna.tools.status.status function,
    threading the service and the queue db_path from config."""
    config.queue.db_path = "/var/queue.sqlite"
    fake_status = AsyncMock(return_value={"version": "x", "graph": {"node_count": 162}})
    monkeypatch.setattr("pratyabhijna.tools.status.status", fake_status)

    result = await tools.status()

    fake_status.assert_awaited_once_with(
        service=service, queue_db_path="/var/queue.sqlite",
    )
    assert result["graph"]["node_count"] == 162


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
    service.remove_episode.assert_awaited_once_with("aaaa-bbbb")


@pytest.mark.asyncio
async def test_forget_episode_returns_uuid(tools, service):
    result = await tools.forget_episode("aaaa-bbbb")
    assert result == {"deleted": True, "uuid": "aaaa-bbbb"}


@pytest.mark.asyncio
async def test_forget_episode_raises_tool_error_on_not_found(tools, service):
    from graphiti_core.errors import NodeNotFoundError
    service.remove_episode.side_effect = NodeNotFoundError("aaaa-bbbb")
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
