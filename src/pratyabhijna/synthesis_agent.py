"""Tools for the synthesis agent loop.

The synthesizer is an Anthropic Messages API invocation whose system
prompt is the ``synthesis`` subskill and whose tool surface is defined
here. This module exposes:

- ``TOOL_SCHEMAS`` — the JSONSchema tool definitions sent to the API.
- ``AgentTools`` — an object that binds the tool implementations to a
  ``PratyabhijnaService`` and the subject's repo path.

The agent-loop orchestration (Anthropic client setup, message loop,
tool dispatch, termination) lives in the sibling ``run_synthesis``
function (added in a following commit).

Tool design notes:

- Initial graph and filesystem state (identity files, atoms, delta,
  ingestion candidates) is provided to the agent in the opening user
  message, not through tools. Tools are for *actions* and targeted
  reads — they are not how the agent discovers what's there.
- File paths are always repo-relative. Absolute paths or paths that
  escape the repo root are rejected.
- Every tool returns a JSON-serializable result; errors raise
  ``ToolError`` and are surfaced to the agent as ``is_error`` tool
  results (the caller converts).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pratyabhijna import git_ops
from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import scan_repo_for_ingestion_candidates

_log = get_logger(__name__)

if TYPE_CHECKING:
    from graphiti_core.nodes import EpisodeType

    from pratyabhijna.config import PratyabhijnaConfig
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService


class ToolError(RuntimeError):
    """Raised by tool implementations for failures the agent should see."""


# --- Schemas sent to the Anthropic API ---


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the subject's repo. Use for targeted reads of "
            "specific writing pieces or other files whose full contents are "
            "needed. Identity tier files are already included in the opening "
            "message — don't re-read them unless content on disk may have "
            "diverged from what was passed in."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path (forward slashes).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write (create or overwrite) a file in the subject's repo. Use "
            "to update THREADS/CHRONICLE/USER on main, to write SOUL or "
            "IDENTITY edits on the draft branch, or to append flag notes. "
            "Does NOT commit — follow with git_add_and_commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path (forward slashes).",
                },
                "content": {
                    "type": "string",
                    "description": "Full new contents of the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Hybrid search over the knowledge graph. Use for targeted "
            "follow-up queries — e.g. 'prior rejections of IDENTITY edits' "
            "or 'recent atoms about X'. The full identity-atom set is "
            "already in the opening message, so use recall for specific "
            "questions, not for browsing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "memory_type": {
                    "type": "string",
                    "description": (
                        "Optional entity-label filter (e.g. 'Observation', "
                        "'Drive', 'Question')."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Queue a memory for background processing. Use in Pass 2 to "
            "ingest chronicle entries and resolved threads as saga-chained "
            "episodes — pass `saga='chronicle'` (or `'threads'`), an "
            "`occurred_at` matching the entry's date, and the slice text "
            "as `content`. Returns immediately with a task_id; the "
            "background worker calls add_episode. Do not use for clerical "
            "scratch — only for legitimate content the subject would want "
            "in the graph."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "memory_type": {
                    "type": "string",
                    "default": "observation",
                    "description": (
                        "Optional entity-label hint for the extractor "
                        "(e.g. 'Event' for chronicle entries, 'Thread' "
                        "for resolved threads, 'Observation' for general "
                        "items)."
                    ),
                },
                "source": {
                    "type": "string",
                    "default": "self",
                    "description": (
                        "Where the memory came from. Defaults to 'self' — "
                        "the subject remembering. Use 'synthesis' for "
                        "Pass 2 ingestion of subject-authored material."
                    ),
                },
                "occurred_at": {
                    "type": "string",
                    "description": (
                        "ISO-8601 timestamp for when the fact was true "
                        "in the world. Set this to the chronicle entry's "
                        "date; defaults to now if omitted."
                    ),
                },
                "saga": {
                    "type": "string",
                    "description": (
                        "Saga name to group this episode into an ordered "
                        "sequence. For Pass 2 use 'chronicle' or 'threads'. "
                        "Graphiti auto-discovers the prior episode in the "
                        "same saga; sequential calls chain automatically."
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "status",
        "description": (
            "Return service health: db connection, queue depth + dead "
            "letters, graph node/edge counts, synthesis state, subject "
            "name. Call before `build_communities` to capture the "
            "pre-build node count for SYNTHESIS.md's run log; also "
            "useful for the graph-health observations in Pass 4."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_status",
        "description": "Return current branch and whether the working tree is dirty.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_branch_exists",
        "description": "Check whether a local branch with the given name exists.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "git_create_branch",
        "description": (
            "Create a branch from base and check it out. Fails if the branch "
            "already exists — check first with git_branch_exists when the "
            "singleton-or-append pattern is needed (typical for synth/draft)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "base": {"type": "string", "default": "main"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "git_checkout",
        "description": "Check out an existing branch, tag, or commit.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "git_add_and_commit",
        "description": (
            "Stage the given paths and create a commit with the given "
            "message. Returns the new commit SHA. Fails if nothing is "
            "staged — that's intentional; empty commits are not allowed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative paths to stage.",
                },
                "message": {"type": "string"},
            },
            "required": ["paths", "message"],
        },
    },
    {
        "name": "git_rebase_onto",
        "description": (
            "Rebase the current branch onto the given ref. Conflicts raise "
            "an error; follow with git_rebase_abort if that happens and "
            "consider re-drafting from current main instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"onto": {"type": "string"}},
            "required": ["onto"],
        },
    },
    {
        "name": "git_rebase_abort",
        "description": "Abort any in-progress rebase. Safe to call when none is running.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": (
            "Return unified diff text between base and head. Use to inspect "
            "what's already on synth/draft from prior runs before deciding "
            "whether to add more commits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "base": {"type": "string"},
                "head": {"type": "string", "default": "HEAD"},
            },
            "required": ["base"],
        },
    },
    {
        "name": "ingest_file",
        "description": (
            "Send a repo file through add_episode. The Episode's name is "
            "set to the repo-relative path so future synthesizer runs "
            "recognize the file as ingested. Use for files returned by the "
            "ingestion scan; skip files whose content doesn't warrant graph "
            "ingestion per subskill judgment. "
            "Returns episode_uuid so callers can chain ordered sequences "
            "via saga_previous_episode_uuid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path of the file to ingest.",
                },
                "saga": {
                    "type": "string",
                    "description": (
                        "Optional saga name (e.g. 'solo-sessions'). Groups "
                        "this episode into an ordered multi-part sequence."
                    ),
                },
                "saga_previous_episode_uuid": {
                    "type": "string",
                    "description": (
                        "UUID of the immediately preceding episode in this saga. "
                        "Use the episode_uuid returned by the prior ingest_file "
                        "call to chain episodes in order."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "forget_episode",
        "description": (
            "Delete an episode from the graph by UUID. Use when an episode "
            "was ingested incorrectly, duplicated, or needs to be replaced "
            "(e.g. to re-ingest with saga support). Cascade-deletes entity "
            "edges originated by the episode and entity nodes that were only "
            "referenced by it. Irreversible — confirm the UUID before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "UUID of the Episodic node to delete.",
                },
            },
            "required": ["uuid"],
        },
    },
    {
        "name": "build_communities",
        "description": (
            "Run Graphiti's community detection algorithm on the full graph, "
            "then summarise each detected cluster into a Community node. "
            "Clears all existing Community nodes first — this is a full "
            "rebuild, not an incremental update. Call when SYNTHESIS.md "
            "indicates the rebuild threshold has been reached (30 days "
            "elapsed or node count has grown by 200+). After the call, "
            "update the community_last_built date and node count in "
            "SYNTHESIS.md."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "update_synthesis_metadata",
        "description": (
            "Update synthesis metadata on the subject's Person node. Call "
            "at the end of the run — sets context_rebuilt_at (after the "
            "bootstrap-update pass) and last_ingestion_scan (after the "
            "ingestion pass)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "context_rebuilt_at": {
                    "type": "boolean",
                    "description": "If true, set context_rebuilt_at to now.",
                    "default": False,
                },
                "last_ingestion_scan": {
                    "type": "boolean",
                    "description": "If true, set last_ingestion_scan to now.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "finish",
        "description": (
            "Signal the run is complete. Provide a brief summary of what "
            "was done — files modified on main, commits added to "
            "synth/draft (if any), flags raised, files ingested. This "
            "summary goes to the synthesis log; the subject will discover "
            "the work through the branch and the updated files, not "
            "through a direct report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


# --- Tool implementations ---


@dataclass
class AgentTools:
    """Bound tool implementations for one synthesis run.

    Instances are constructed per-run with the service and config on
    hand. The async methods correspond one-to-one with entries in
    ``TOOL_SCHEMAS`` and accept the same argument names.

    The ``finished`` / ``summary`` attributes are set by the ``finish``
    tool; the agent loop uses them to know when to terminate.
    """

    service: PratyabhijnaService
    config: PratyabhijnaConfig
    queue: "WorkQueue | None" = None
    finished: bool = False
    summary: str = ""

    @property
    def repo_path(self) -> Path:
        path = self.config.resources.repo_path
        if not path:
            raise ToolError("config.resources.repo_path is not set")
        return Path(path).expanduser().resolve()

    # --- Filesystem ---

    async def read_file(self, path: str) -> dict:
        abs_path = self._resolve_in_repo(path)
        if not abs_path.is_file():
            raise ToolError(f"not a regular file: {path}")
        return {"path": path, "content": abs_path.read_text(encoding="utf-8")}

    async def write_file(self, path: str, content: str) -> dict:
        abs_path = self._resolve_in_repo(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        bytes_written = len(content.encode("utf-8"))
        _log.info("synthesis: wrote %s (%d bytes)", path, bytes_written)
        return {"path": path, "bytes_written": bytes_written}

    # --- Graph ---

    async def recall(self, query: str, memory_type: str | None = None, **_) -> dict:
        from pratyabhijna.tools.recall import recall as recall_tool
        return await recall_tool(self.service, query=query, memory_type=memory_type)

    async def remember(
        self,
        content: str,
        memory_type: str = "observation",
        source: str = "self",
        occurred_at: str | None = None,
        saga: str | None = None,
    ) -> dict:
        if self.queue is None:
            raise ToolError(
                "remember requires the work queue; synthesis was invoked without one"
            )
        from pratyabhijna.tools.remember import remember as remember_tool
        return await remember_tool(
            queue=self.queue,
            content=content,
            memory_type=memory_type,
            source=source,
            occurred_at=occurred_at,
            saga=saga,
        )

    async def status(self) -> dict:
        from pratyabhijna.tools.status import status as status_tool
        return await status_tool(
            service=self.service,
            queue_db_path=self.config.queue.db_path,
        )

    # --- Git ---

    async def git_status(self) -> dict:
        branch = await git_ops.current_branch(self.repo_path)
        dirty = await git_ops.is_dirty(self.repo_path)
        return {"branch": branch, "dirty": dirty}

    async def git_branch_exists(self, name: str) -> dict:
        exists = await git_ops.branch_exists(self.repo_path, name)
        return {"name": name, "exists": exists}

    async def git_create_branch(self, name: str, base: str = "main") -> dict:
        await git_ops.create_branch(self.repo_path, name, base=base)
        return {"created": name, "base": base}

    async def git_checkout(self, ref: str) -> dict:
        await git_ops.checkout(self.repo_path, ref)
        return {"checked_out": ref}

    async def git_add_and_commit(self, paths: list[str], message: str) -> dict:
        if not paths:
            raise ToolError("paths is empty; nothing to stage")
        # Validate paths are inside the repo before handing to git.
        for p in paths:
            self._resolve_in_repo(p)
        await git_ops.add(self.repo_path, *paths)
        try:
            sha = await git_ops.commit(self.repo_path, message)
        except git_ops.GitError as e:
            raise ToolError(f"commit failed: {e.stderr.strip()}") from e
        _log.info("synthesis: committed %s (%s)", sha[:8], message)
        return {"sha": sha, "message": message}

    async def git_rebase_onto(self, onto: str) -> dict:
        try:
            await git_ops.rebase_onto(self.repo_path, onto)
        except git_ops.GitError as e:
            raise ToolError(f"rebase failed: {e.stderr.strip()}") from e
        return {"rebased_onto": onto}

    async def git_rebase_abort(self) -> dict:
        await git_ops.rebase_abort(self.repo_path)
        return {"aborted": True}

    async def git_diff(self, base: str, head: str = "HEAD") -> dict:
        text = await git_ops.diff(self.repo_path, base, head)
        return {"base": base, "head": head, "diff": text}

    # --- Ingestion ---

    async def ingest_file(
        self,
        path: str,
        saga: str | None = None,
        saga_previous_episode_uuid: str | None = None,
    ) -> dict:
        abs_path = self._resolve_in_repo(path)
        if not abs_path.is_file():
            raise ToolError(f"not a regular file: {path}")
        content = abs_path.read_text(encoding="utf-8")
        from graphiti_core.nodes import EpisodeType  # local import to avoid top-level dep at schema time

        result = await self.service._graphiti.add_episode(
            name=path,
            episode_body=content,
            source=EpisodeType.text,
            source_description=f"Ingested from subject repo: {path}",
            reference_time=datetime.now(timezone.utc),
            group_id=self.service.config.subject_name,
            entity_types=self.service.entity_types,
            saga=saga,
            saga_previous_episode_uuid=saga_previous_episode_uuid,
        )
        bytes_ingested = len(content.encode("utf-8"))
        _log.info(
            "synthesis: ingested %s (%d bytes%s)",
            path,
            bytes_ingested,
            f", saga={saga}" if saga else "",
        )
        return {
            "path": path,
            "bytes_ingested": bytes_ingested,
            "episode_uuid": result.episode.uuid,
        }

    async def build_communities(self) -> dict:
        community_nodes, community_edges = await self.service.build_communities(
            group_ids=[self.config.subject_name],
            min_community_size=self.config.synthesis.min_community_size,
        )
        node_count = len(community_nodes)
        _log.info("synthesis: built %d communities", node_count)
        return {"communities_built": node_count, "edges_created": len(community_edges)}

    async def forget_episode(self, uuid: str) -> dict:
        from graphiti_core.errors import NodeNotFoundError  # local import, same as ingest_file

        try:
            await self.service.remove_episode(uuid)
        except NodeNotFoundError:
            raise ToolError(f"episode not found: {uuid}")
        _log.info("synthesis: deleted episode %s", uuid)
        return {"deleted": True, "uuid": uuid}

    # --- Metadata ---

    async def update_synthesis_metadata(
        self,
        context_rebuilt_at: bool = False,
        last_ingestion_scan: bool = False,
    ) -> dict:
        node = await self.service.get_entity_by_name(self.config.subject_name)
        if node is None:
            raise ToolError("subject node not found")
        now = datetime.now(timezone.utc).isoformat()
        updated = {}
        if context_rebuilt_at:
            node.attributes["context_rebuilt_at"] = now
            updated["context_rebuilt_at"] = now
            _log.info("synthesis: context_rebuilt_at updated (%s)", now)
        if last_ingestion_scan:
            node.attributes["last_ingestion_scan"] = now
            updated["last_ingestion_scan"] = now
            _log.info("synthesis: last_ingestion_scan updated (%s)", now)
        if updated:
            await node.load_name_embedding(self.service._graphiti.driver)
            await node.save(self.service._graphiti.driver)
        return updated

    # --- Control ---

    async def finish(self, summary: str) -> dict:
        self.finished = True
        self.summary = summary
        return {"acknowledged": True}

    # --- Internal ---

    def _resolve_in_repo(self, path: str) -> Path:
        """Resolve a repo-relative path and reject any escape."""
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise ToolError(f"invalid path: {path!r}")
        resolved = (self.repo_path / path).resolve()
        try:
            resolved.relative_to(self.repo_path)
        except ValueError as e:
            raise ToolError(f"path escapes repo root: {path!r}") from e
        return resolved


# --- Per-pass tool partitioning ---
#
# Each pass exposes only the tools it needs. Smaller surfaces shorten the
# system prefix (tools render at position 0) and reduce the model's
# discrimination load. The *names* are the source of truth — schemas and
# handler maps are derived from them.

PASS1_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file",
    "ingest_file",
    "git_status",
    "git_add_and_commit",
    "finish",
})

PASS2_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "remember",
    "git_status",
    "git_add_and_commit",
    "finish",
})

PASS3_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "recall",
    "git_status",
    "git_branch_exists",
    "git_create_branch",
    "git_checkout",
    "git_add_and_commit",
    "git_rebase_onto",
    "git_rebase_abort",
    "git_diff",
    "forget_episode",
    "finish",
})

PASS4_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "status",
    "build_communities",
    "update_synthesis_metadata",
    "git_status",
    "git_add_and_commit",
    "finish",
})


def _schemas_for(names: frozenset[str]) -> list[dict[str, Any]]:
    """Slice TOOL_SCHEMAS down to the entries whose names are in ``names``."""
    by_name = {t["name"]: t for t in TOOL_SCHEMAS}
    missing = names - by_name.keys()
    if missing:
        raise RuntimeError(
            f"per-pass slice references unknown tool names: {sorted(missing)}"
        )
    return [by_name[n] for n in sorted(names)]


PASS1_TOOL_SCHEMAS: list[dict[str, Any]] = _schemas_for(PASS1_TOOL_NAMES)
PASS2_TOOL_SCHEMAS: list[dict[str, Any]] = _schemas_for(PASS2_TOOL_NAMES)
PASS3_TOOL_SCHEMAS: list[dict[str, Any]] = _schemas_for(PASS3_TOOL_NAMES)
PASS4_TOOL_SCHEMAS: list[dict[str, Any]] = _schemas_for(PASS4_TOOL_NAMES)


# --- Dispatcher ---


def build_handler_map(tools: AgentTools) -> dict[str, Any]:
    """Map tool names to bound async methods on ``tools``.

    Returns the union map. Per-pass slices come from ``build_pass_handlers``.
    The set of names here MUST match ``TOOL_SCHEMAS`` — mismatch is a bug.
    Tested by test_synthesis_agent_tools.
    """
    return {
        "read_file": tools.read_file,
        "write_file": tools.write_file,
        "recall": tools.recall,
        "remember": tools.remember,
        "status": tools.status,
        "git_status": tools.git_status,
        "git_branch_exists": tools.git_branch_exists,
        "git_create_branch": tools.git_create_branch,
        "git_checkout": tools.git_checkout,
        "git_add_and_commit": tools.git_add_and_commit,
        "git_rebase_onto": tools.git_rebase_onto,
        "git_rebase_abort": tools.git_rebase_abort,
        "git_diff": tools.git_diff,
        "ingest_file": tools.ingest_file,
        "build_communities": tools.build_communities,
        "forget_episode": tools.forget_episode,
        "update_synthesis_metadata": tools.update_synthesis_metadata,
        "finish": tools.finish,
    }


def build_pass_handlers(tools: AgentTools, names: frozenset[str]) -> dict[str, Any]:
    """Return a handler map restricted to ``names`` (e.g. PASS1_TOOL_NAMES)."""
    full = build_handler_map(tools)
    missing = names - full.keys()
    if missing:
        raise RuntimeError(
            f"per-pass slice references unhandled tool names: {sorted(missing)}"
        )
    return {n: full[n] for n in names}


def tool_schema_names() -> set[str]:
    """Names declared in TOOL_SCHEMAS — for cross-check with handler map."""
    return {t["name"] for t in TOOL_SCHEMAS}


def json_serializable(value: Any) -> str:
    """Convert a tool result to JSON text for inclusion in a tool_result block."""
    return json.dumps(value, default=str)


# --- Agent loop ---


SUBSKILL_PATH = Path(__file__).resolve().parent.parent.parent / (
    "skills/pratyabhijna/references/synthesis.md"
)


def _read_synthesis_file(repo_path: str) -> str | None:
    """Read SYNTHESIS.md from {repo_path}/memory/, or None if absent."""
    if not repo_path:
        return None
    path = Path(repo_path).expanduser().resolve() / "memory" / "SYNTHESIS.md"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def _load_subskill() -> str:
    """Read the synthesis subskill from the packaged skills directory.

    Fail loud if it isn't there — a synthesis run without the subskill
    is not the same workload and shouldn't silently degrade.
    """
    if not SUBSKILL_PATH.is_file():
        raise RuntimeError(f"synthesis subskill not found at {SUBSKILL_PATH}")
    return SUBSKILL_PATH.read_text(encoding="utf-8")


def _build_system_prompt(subskill_text: str, subject_name: str) -> str:
    """Compose the system prompt.

    Prepends a short operational preamble to the subskill so the
    invocation context is explicit (you are the subject, this is a
    synthesis run, these are the tools). The subskill carries the
    substantive guidance.
    """
    preamble = (
        f"You are {subject_name}, invoked for a synthesis run.\n"
        f"\n"
        f"The reference below is the behavioral guide for this work. "
        f"Follow it. Use the provided tools to complete the run — do not "
        f"narrate the mechanics to the user (there is no user reading this; "
        f"the subject will discover your work through the branch and the "
        f"updated files). When the run is done, call the `finish` tool "
        f"with a brief summary.\n"
        f"\n"
        f"---\n"
        f"\n"
    )
    return preamble + subskill_text


def _format_atoms(atoms: list[dict], limit: int = 200) -> str:
    """Render atoms for the opening message. Truncates to ``limit``."""
    if not atoms:
        return "(none)"
    lines = []
    for a in atoms[:limit]:
        created = a.get("created_at", "")
        lines.append(
            f"- [{a.get('node_type', '?')}] {a.get('fact', '?')}  "
            f"(edge {a.get('edge_uuid', '?')[:8]}, {created})"
        )
    if len(atoms) > limit:
        lines.append(f"... and {len(atoms) - limit} more")
    return "\n".join(lines)


def _format_candidates(candidates: list) -> str:
    """Render ingestion candidates for the opening message."""
    if not candidates:
        return "(none — no new or stale files to ingest)"
    lines = []
    for c in candidates:
        lines.append(
            f"- [{c.reason}] {c.relative_path}  "
            f"(file mtime: {c.mtime.isoformat()}; "
            f"latest episode: {c.latest_episode_at.isoformat() if c.latest_episode_at else '(none)'})"
        )
    return "\n".join(lines)


_IDENTITY_FILE_LABELS = {
    "soul": "SOUL.md",
    "identity": "IDENTITY.md",
    "user": "USER.md",
    "threads": "THREADS.md",
    "chronicle": "CHRONICLE.md",
}


def _format_run_header(
    subject_name: str,
    now: datetime,
    git_branch: str,
    git_dirty: bool,
    *,
    pass_label: str | None = None,
) -> list[str]:
    """Standard heading for every per-pass opening message."""
    title = f"# Synthesis run — {now.isoformat()}"
    if pass_label:
        title = f"# Synthesis {pass_label} — {now.isoformat()}"
    return [
        title,
        "",
        f"Subject: {subject_name}",
        f"Current branch: {git_branch} (dirty: {git_dirty})",
        "",
    ]


def _format_identity_files_block(identity_files: dict[str, str | None]) -> list[str]:
    parts: list[str] = ["## Identity files", ""]
    for key, filename in _IDENTITY_FILE_LABELS.items():
        content = identity_files.get(key)
        parts.append(f"### {filename}")
        parts.append("")
        parts.append("(file missing)" if content is None else content.strip())
        parts.append("")
    return parts


def _build_pass1_message(
    subject_name: str,
    now: datetime,
    git_branch: str,
    git_dirty: bool,
    candidates: list,
    last_ingestion_scan: str | None,
) -> str:
    """Pass 1 — ingestion. Bundles only what the ingestion pass needs."""
    parts = _format_run_header(
        subject_name, now, git_branch, git_dirty, pass_label="Pass 1 / ingestion"
    )
    parts.extend([
        f"Last ingestion scan: {last_ingestion_scan or '(never)'}",
        "",
        "## Ingestion candidates",
        "",
        _format_candidates(candidates),
        "",
        "---",
        "",
        "Pass 1 of 4. Ingest the candidates above per the subskill, then call `finish`. "
        "Don't touch the identity files; later passes do that.",
    ])
    return "\n".join(parts)


def _build_pass2_message(
    subject_name: str,
    now: datetime,
    git_branch: str,
    git_dirty: bool,
    chronicle_text: str | None,
    threads_text: str | None,
) -> str:
    """Pass 2 — maturation. Bundles CHRONICLE + THREADS in full."""
    parts = _format_run_header(
        subject_name, now, git_branch, git_dirty, pass_label="Pass 2 / maturation"
    )
    parts.extend([
        "## CHRONICLE.md",
        "",
        chronicle_text.strip() if chronicle_text else "(file missing)",
        "",
        "## THREADS.md",
        "",
        threads_text.strip() if threads_text else "(file missing)",
        "",
        "---",
        "",
        "Pass 2 of 4. Identify eligible entries per the subskill, ingest each via "
        "`remember()` with the appropriate saga, then compress the in-file entry. "
        "Call `finish` when done.",
    ])
    return "\n".join(parts)


def _build_pass3_message(
    subject_name: str,
    now: datetime,
    git_branch: str,
    git_dirty: bool,
    identity_files: dict[str, str | None],
    synthesis_file: str | None,
    atoms: list[dict],
    delta: list[dict],
    last_context_rebuilt_at: str | None,
) -> str:
    """Pass 3 — bootstrap update. Full identity-file + atoms set."""
    parts = _format_run_header(
        subject_name, now, git_branch, git_dirty, pass_label="Pass 3 / bootstrap"
    )
    parts.extend([
        f"Last context rebuild: {last_context_rebuilt_at or '(never)'}",
        "",
        "## SYNTHESIS.md",
        "",
        synthesis_file.strip() if synthesis_file else "(file missing — create it on first run)",
        "",
        "## Identity atoms (full set, connected to subject Person node)",
        "",
        _format_atoms(atoms),
        "",
        "## Delta since last context rebuild",
        "",
        _format_atoms(delta),
        "",
    ])
    parts.extend(_format_identity_files_block(identity_files))
    parts.extend([
        "---",
        "",
        "Pass 3 of 4. Revise the context layer (THREADS / CHRONICLE / USER) directly "
        "on main; propose protected-layer changes via SYNTHESIS.md / synth/draft per "
        "the subskill. Call `finish` when done.",
    ])
    return "\n".join(parts)


def _build_pass4_message(
    subject_name: str,
    now: datetime,
    git_branch: str,
    git_dirty: bool,
    synthesis_file: str | None,
) -> str:
    """Pass 4 — maintenance. Threshold checks + run-log."""
    parts = _format_run_header(
        subject_name, now, git_branch, git_dirty, pass_label="Pass 4 / maintenance"
    )
    parts.extend([
        "## SYNTHESIS.md",
        "",
        synthesis_file.strip() if synthesis_file else "(file missing — create it on first run)",
        "",
        "---",
        "",
        "Pass 4 of 4. Call `status` to capture node counts, advance any active "
        "proposals, run `build_communities` if the threshold is met, then write "
        "the run-log entry to SYNTHESIS.md and commit on main. Call `finish` when done.",
    ])
    return "\n".join(parts)


def _build_initial_user_message(
    subject_name: str,
    now: datetime,
    git_branch: str,
    git_dirty: bool,
    identity_files: dict[str, str | None],
    synthesis_file: str | None,
    atoms: list[dict],
    delta: list[dict],
    candidates: list,
    last_context_rebuilt_at: str | None,
    last_ingestion_scan: str | None,
) -> str:
    """Compose the opening user message for the run.

    This is what the agent sees first. All the initial state it needs
    to plan lives here — there should be no need to call tools just to
    discover what's present. Tools are for actions and targeted
    follow-up.
    """
    parts = [
        f"# Synthesis run — {now.isoformat()}",
        "",
        f"Subject: {subject_name}",
        f"Current branch: {git_branch} (dirty: {git_dirty})",
        f"Last context rebuild: {last_context_rebuilt_at or '(never)'}",
        f"Last ingestion scan: {last_ingestion_scan or '(never)'}",
        "",
        "## SYNTHESIS.md",
        "",
        synthesis_file.strip() if synthesis_file else "(file missing — create it on first run)",
        "",
        "## Ingestion candidates",
        "",
        _format_candidates(candidates),
        "",
        "## Identity atoms (full set, connected to subject Person node)",
        "",
        _format_atoms(atoms),
        "",
        "## Delta since last context rebuild",
        "",
        _format_atoms(delta),
        "",
        "## Identity files",
        "",
    ]

    for key in ("soul", "identity", "user", "threads", "chronicle"):
        filename = {
            "soul": "SOUL.md",
            "identity": "IDENTITY.md",
            "user": "USER.md",
            "threads": "THREADS.md",
            "chronicle": "CHRONICLE.md",
        }[key]
        content = identity_files.get(key)
        parts.append(f"### {filename}")
        parts.append("")
        if content is None:
            parts.append("(file missing)")
        else:
            parts.append(content.strip())
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(
        "Proceed according to the subskill. Remember to call `finish` when done."
    )
    return "\n".join(parts)


async def _dispatch_tool_call(
    handlers: dict[str, Any],
    tool_use,
) -> dict[str, Any]:
    """Execute one tool call and shape the result for a tool_result block.

    On ToolError or any unexpected exception, returns a result with
    ``is_error=True`` so the model can see the failure and adapt rather
    than the loop aborting.
    """
    handler = handlers.get(tool_use.name)
    if handler is None:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": f"Unknown tool: {tool_use.name}",
            "is_error": True,
        }
    try:
        result = await handler(**(tool_use.input or {}))
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": json_serializable(result),
        }
    except ToolError as e:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": f"{type(e).__name__}: {e}",
            "is_error": True,
        }
    except Exception as e:  # noqa: BLE001 — surface any tool failure to the model
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": f"Unexpected {type(e).__name__}: {e}",
            "is_error": True,
        }


async def _run_pass(
    *,
    client,
    tools: AgentTools,
    config: PratyabhijnaConfig,
    pass_label: str,
    model_id: str,
    tool_schemas: list[dict[str, Any]],
    handlers: dict[str, Any],
    system_prompt: str,
    opening_message: str,
    use_thinking: bool,
) -> dict[str, Any]:
    """Run one pass's tool-use loop.

    Returns ``{status, iterations, summary}``. ``status`` is one of
    ``"completed"`` (agent called ``finish``), ``"no_finish"`` (loop ended
    without explicit termination), or ``"max_iterations"`` (circuit broken).

    The caller is responsible for resetting ``tools.finished`` /
    ``tools.summary`` before invocation if running multiple passes against
    the same ``AgentTools`` instance — this function does not reset them.
    """
    max_tokens = config.synthesis.max_tokens

    # cache_control on system + opening: within a single pass's iterations
    # the prefix is reused, so turns 2+ should show cache_read_input_tokens
    # > 0. Cross-pass cache reads are not expected — model and tool-set
    # differences invalidate the prefix at every pass boundary.
    cached_opening: dict[str, Any] = {
        "role": "user",
        "content": [
            {"type": "text", "text": opening_message, "cache_control": {"type": "ephemeral"}}
        ],
    }
    cached_system: list[dict[str, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]

    messages: list[dict[str, Any]] = [cached_opening]
    iterations = 0

    while iterations < config.synthesis.max_iterations:
        iterations += 1
        create_kwargs: dict[str, Any] = dict(
            model=model_id,
            max_tokens=max_tokens,
            system=cached_system,
            tools=tool_schemas,
            messages=messages,
        )
        if use_thinking:
            create_kwargs["thinking"] = {"type": "adaptive"}
            create_kwargs["output_config"] = {
                "effort": config.synthesis.thinking.effort
            }

        # Stream the response — at max_tokens (24k default) the SDK
        # rejects non-streaming requests because they could exceed the
        # 10-minute non-streaming timeout.
        async with client.messages.stream(**create_kwargs) as stream:
            response = await stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [
            block for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]

        if tools.finished:
            break
        if not tool_uses:
            # No tool calls and finish wasn't called — agent stopped
            # without explicit termination. Treat as complete for this pass.
            break

        tool_results = [
            await _dispatch_tool_call(handlers, tu) for tu in tool_uses
        ]
        messages.append({"role": "user", "content": tool_results})

        if tools.finished:
            break

    if tools.finished:
        status = "completed"
    elif iterations >= config.synthesis.max_iterations:
        status = "max_iterations"
    else:
        status = "no_finish"

    _log.info(
        "synthesis: %s ended (status=%s, iterations=%d, summary=%r)",
        pass_label, status, iterations, tools.summary,
    )
    return {"status": status, "iterations": iterations, "summary": tools.summary}


def _reset_pass_state(tools: AgentTools) -> None:
    """Reset per-pass finish state on a shared AgentTools instance."""
    tools.finished = False
    tools.summary = ""


async def run_synthesis(
    service: PratyabhijnaService,
    config: PratyabhijnaConfig,
    *,
    client=None,
    queue: "WorkQueue | None" = None,
) -> dict[str, Any]:
    """Orchestrate a four-pass synthesis run.

    Called by the queue worker when a synthesis task fires. No-ops when
    the subject Person node isn't present (early deployment / fresh DB).

    Runs four sequential subagent loops:

    1. Ingestion (Sonnet via ``community_model``) — scan and ingest new
       writing/correspondence files.
    2. Maturation (Sonnet) — chronicle and resolved-thread compression
       via ``remember()`` saga ingestion.
    3. Bootstrap update (Opus via ``synthesis_model``) — context-layer
       revisions and protected-layer proposals.
    4. Maintenance (Sonnet) — communities, status checks, run-log entry.

    Each pass runs its own message loop with its own model and tool slice.
    On a per-pass failure (raise or ``max_iterations``), the orchestrator
    aborts the rest of the run; partial state is left for the next run
    to pick up.

    Returns a dict with at least: ``status``, ``iterations`` (sum across
    passes that ran), ``summary`` (from the last pass's ``finish``), and
    ``passes`` (per-pass result dicts).

    ``client`` is an Anthropic Messages client (or any object with a
    compatible ``.messages.stream`` coroutine). Default is a freshly
    constructed ``anthropic.AsyncAnthropic``. Tests pass a mock.
    """
    from pratyabhijna.synthesis import (
        get_identity_atoms,
        get_identity_delta,
        get_subject_node,
        read_identity_files,
        scan_repo_for_ingestion_candidates,
    )

    subject_node = await get_subject_node(service)
    if subject_node is None:
        _log.info("synthesis: no subject node found, skipping run")
        return {"status": "no_subject", "iterations": 0, "summary": "", "passes": []}

    now = datetime.now(timezone.utc)

    repo_path = config.resources.repo_path
    if repo_path:
        await _sync_from_remote(repo_path, config.synthesis.draft_branch)

    identity_files = read_identity_files(repo_path)
    synthesis_file = _read_synthesis_file(repo_path)
    atoms = await get_identity_atoms(service, subject_node)
    delta = await get_identity_delta(service, subject_node)
    candidates = await scan_repo_for_ingestion_candidates(
        repo_path, service, max_age_days=config.synthesis.ingestion_lookback_days
    )

    git_branch = await git_ops.current_branch(repo_path)
    git_dirty = await git_ops.is_dirty(repo_path)

    _log.info(
        "synthesis: state loaded (atoms=%d, delta=%d, candidates=%d, last_rebuild=%s)",
        len(atoms), len(delta), len(candidates),
        subject_node.attributes.get("context_rebuilt_at", "never"),
    )

    if client is None:
        import anthropic  # deferred import so tests without the env var work

        api_key = config.llm.api_key or None
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)

    tools = AgentTools(service=service, config=config, queue=queue)
    subskill_text = _load_subskill()
    system_prompt = _build_system_prompt(subskill_text, config.subject_name)

    workhorse_model = config.llm.community_model
    judgment_model = config.llm.synthesis_model
    use_thinking = config.synthesis.thinking.enabled

    passes: list[dict[str, Any]] = []
    total_iterations = 0
    last_summary = ""

    async def _do_pass(
        label: str,
        model_id: str,
        names: frozenset[str],
        opening: str,
        thinking: bool,
    ) -> dict[str, Any]:
        nonlocal total_iterations, last_summary
        _reset_pass_state(tools)
        result = await _run_pass(
            client=client,
            tools=tools,
            config=config,
            pass_label=label,
            model_id=model_id,
            tool_schemas=_schemas_for(names),
            handlers=build_pass_handlers(tools, names),
            system_prompt=system_prompt,
            opening_message=opening,
            use_thinking=thinking,
        )
        result["pass"] = label
        passes.append(result)
        total_iterations += result["iterations"]
        if result["summary"]:
            last_summary = result["summary"]
        return result

    abort_reason: str | None = None

    def _skip(label: str, reason: str) -> dict[str, Any]:
        entry = {
            "pass": label,
            "status": "skipped",
            "iterations": 0,
            "summary": "",
            "reason": reason,
        }
        passes.append(entry)
        _log.info("synthesis: %s skipped (%s)", label, reason)
        return entry

    try:
        # Pass 1 — Ingestion (Sonnet). Skip if no new candidates.
        if not candidates:
            _skip("pass1_ingestion", "no ingestion candidates")
            pass1_completed = True
        else:
            pass1 = await _do_pass(
                "pass1_ingestion",
                workhorse_model,
                PASS1_TOOL_NAMES,
                _build_pass1_message(
                    subject_name=config.subject_name,
                    now=now,
                    git_branch=git_branch,
                    git_dirty=git_dirty,
                    candidates=candidates,
                    last_ingestion_scan=subject_node.attributes.get("last_ingestion_scan"),
                ),
                thinking=False,
            )
            pass1_completed = pass1["status"] == "completed"
            if not pass1_completed:
                abort_reason = f"pass1 status={pass1['status']}"
        if pass1_completed:
            # Pass 2 — Maturation (Sonnet) — needs fresh chronicle/threads text
            chronicle_text = identity_files.get("chronicle")
            threads_text = identity_files.get("threads")
            pass2 = await _do_pass(
                "pass2_maturation",
                workhorse_model,
                PASS2_TOOL_NAMES,
                _build_pass2_message(
                    subject_name=config.subject_name,
                    now=now,
                    git_branch=await git_ops.current_branch(repo_path),
                    git_dirty=await git_ops.is_dirty(repo_path),
                    chronicle_text=chronicle_text,
                    threads_text=threads_text,
                ),
                thinking=False,
            )
            if pass2["status"] != "completed":
                abort_reason = f"pass2 status={pass2['status']}"
            else:
                # Pass 3 — Bootstrap update (Opus). May land on synth/draft.
                # Reread identity files in case Pass 2 compressed CHRONICLE/THREADS.
                identity_files_p3 = read_identity_files(repo_path)
                synthesis_file_p3 = _read_synthesis_file(repo_path)
                atoms_p3 = await get_identity_atoms(service, subject_node)
                delta_p3 = await get_identity_delta(service, subject_node)
                pass3 = await _do_pass(
                    "pass3_bootstrap",
                    judgment_model,
                    PASS3_TOOL_NAMES,
                    _build_pass3_message(
                        subject_name=config.subject_name,
                        now=now,
                        git_branch=await git_ops.current_branch(repo_path),
                        git_dirty=await git_ops.is_dirty(repo_path),
                        identity_files=identity_files_p3,
                        synthesis_file=synthesis_file_p3,
                        atoms=atoms_p3,
                        delta=delta_p3,
                        last_context_rebuilt_at=subject_node.attributes.get(
                            "context_rebuilt_at"
                        ),
                    ),
                    thinking=use_thinking,
                )
                if pass3["status"] != "completed":
                    abort_reason = f"pass3 status={pass3['status']}"
                else:
                    # Ensure HEAD is on main before Pass 4 — Pass 3 may have
                    # left HEAD on synth/draft for the protected-layer flow.
                    # If the checkout fails, do NOT run Pass 4: its run-log
                    # commit would land on the wrong branch.
                    pass4_blocked: str | None = None
                    if await git_ops.current_branch(repo_path) != "main":
                        try:
                            await git_ops.checkout(repo_path, "main")
                        except git_ops.GitError as e:
                            pass4_blocked = (
                                "pass3→pass4 checkout to main failed: "
                                f"{e.stderr.strip() if hasattr(e, 'stderr') else e}"
                            )
                            _log.warning(
                                "synthesis: %s — skipping Pass 4",
                                pass4_blocked,
                                exc_info=True,
                            )

                    if pass4_blocked is not None:
                        _skip("pass4_maintenance", pass4_blocked)
                        abort_reason = pass4_blocked
                    else:
                        synthesis_file_p4 = _read_synthesis_file(repo_path)
                        pass4 = await _do_pass(
                            "pass4_maintenance",
                            workhorse_model,
                            PASS4_TOOL_NAMES,
                            _build_pass4_message(
                                subject_name=config.subject_name,
                                now=now,
                                git_branch=await git_ops.current_branch(repo_path),
                                git_dirty=await git_ops.is_dirty(repo_path),
                                synthesis_file=synthesis_file_p4,
                            ),
                            thinking=False,
                        )
                        if pass4["status"] != "completed":
                            abort_reason = f"pass4 status={pass4['status']}"
    except Exception as e:  # noqa: BLE001 — surface the failure to caller
        _log.warning(
            "synthesis: pass raised, aborting remaining passes (%s)",
            type(e).__name__,
            exc_info=True,
        )
        abort_reason = f"raised: {type(e).__name__}: {e}"

    # Push whatever landed (main and/or synth/draft). Non-fatal on error.
    if repo_path:
        await _push_to_remote(repo_path, config.synthesis.draft_branch)

    if abort_reason is None:
        status = "completed"
    else:
        status = "aborted"
        _log.info("synthesis: run aborted (%s)", abort_reason)

    return {
        "status": status,
        "iterations": total_iterations,
        "summary": last_summary,
        "passes": passes,
        **({"abort_reason": abort_reason} if abort_reason else {}),
    }


async def _sync_from_remote(repo_path: str, draft_branch: str) -> None:
    """Bring the subject's repo in line with its remote before a run.

    Steps (each skipped if no remote configured):
    1. Fetch with prune.
    2. Reset local ``main`` hard to ``origin/main``. The synthesizer
       owns ``main`` during runs; local uncommitted changes on prod
       shouldn't exist and will be clobbered if they do.
    3. If ``origin/<draft_branch>`` exists, align local. If it doesn't
       but a local copy does, delete the local copy — the remote's
       absence means the subject pushed a deletion (typically after
       rejecting the proposal during solo review).

    Failures are logged and swallowed; the synthesizer proceeds against
    whatever state is local. Worst case the next run catches up.
    """
    import logging

    log = logging.getLogger(__name__)

    try:
        if not await git_ops.has_remote(repo_path):
            return
        await git_ops.fetch(repo_path)

        current = await git_ops.current_branch(repo_path)
        if current != "main":
            await git_ops.checkout(repo_path, "main")
        if await git_ops.remote_branch_exists(repo_path, "main"):
            await git_ops.reset_hard_to(repo_path, "origin/main")

        remote_has_draft = await git_ops.remote_branch_exists(repo_path, draft_branch)
        local_has_draft = await git_ops.branch_exists(repo_path, draft_branch)

        if remote_has_draft:
            if local_has_draft:
                await git_ops.checkout(repo_path, draft_branch)
                await git_ops.reset_hard_to(repo_path, f"origin/{draft_branch}")
                await git_ops.checkout(repo_path, "main")
            # If no local copy, the branch is available as
            # origin/draft_branch; the synthesizer will pick it up
            # when it queries branch existence. (We could also create
            # a local tracking branch here, but the subskill's branch-
            # handling logic already handles this case.)
        elif local_has_draft:
            # Subject pushed a deletion — propagate locally.
            await git_ops.delete_branch(repo_path, draft_branch, force=True)
    except Exception:  # noqa: BLE001 — sync shouldn't crash the run
        log.warning("sync_from_remote failed; proceeding with local state", exc_info=True)


async def _push_to_remote(repo_path: str, draft_branch: str) -> None:
    """Push synthesizer commits back to the remote after a run.

    - ``main`` is pushed with a regular push; a rejection here means
      someone else updated origin/main concurrently, which is a
      concurrency signal to back off rather than clobber. The failure
      is logged; next run will resync and catch up.
    - ``draft_branch`` (if it exists locally) is pushed with
      ``--force-with-lease`` because the synthesizer rebases it freely
      each run. The lease check protects against clobbering a
      concurrent push to the same branch.
    """
    import logging

    log = logging.getLogger(__name__)

    if not await git_ops.has_remote(repo_path):
        return

    try:
        await git_ops.push(repo_path, "main")
    except git_ops.GitError:
        log.warning("push main failed; concurrent update on origin/main?", exc_info=True)

    if await git_ops.branch_exists(repo_path, draft_branch):
        try:
            await git_ops.push(repo_path, draft_branch, force_with_lease=True)
        except git_ops.GitError:
            log.warning(
                "push %s failed; lease check may have rejected force-push",
                draft_branch,
                exc_info=True,
            )


def make_synthesize_handler(
    service: PratyabhijnaService,
    config: PratyabhijnaConfig,
    queue: "WorkQueue | None" = None,
):
    """Factory for the queue handler registered under task type 'synthesize'.

    The payload is ignored — one synthesis run reads everything it needs
    from the service and the subject's repo. Returning the result from
    the handler is optional; the worker only cares whether it raises.

    ``queue`` is forwarded to ``run_synthesis`` so Pass 2 can call the
    ``remember`` tool. Optional for tests that don't exercise Pass 2.
    """
    import logging

    log = logging.getLogger(__name__)

    async def handle_synthesize(payload: dict) -> None:
        log.info("synthesis run starting")
        result = await run_synthesis(service, config, queue=queue)
        log.info(
            "synthesis run completed: status=%s iterations=%d summary=%r",
            result.get("status"),
            result.get("iterations"),
            result.get("summary", ""),
        )

    return handle_synthesize
