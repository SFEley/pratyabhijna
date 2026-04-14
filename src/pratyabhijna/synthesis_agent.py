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
from pratyabhijna.synthesis import scan_repo_for_ingestion_candidates

if TYPE_CHECKING:
    from graphiti_core.nodes import EpisodeType

    from pratyabhijna.config import PratyabhijnaConfig
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
                        "'Position', 'Question')."
                    ),
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
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
            "ingestion per subskill judgment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path of the file to ingest.",
                },
            },
            "required": ["path"],
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
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    # --- Graph ---

    async def recall(
        self,
        query: str,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> dict:
        results = await self.service.recall(
            query=query, memory_type=memory_type, limit=limit
        )
        return {"results": results}

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

    async def ingest_file(self, path: str) -> dict:
        abs_path = self._resolve_in_repo(path)
        if not abs_path.is_file():
            raise ToolError(f"not a regular file: {path}")
        content = abs_path.read_text(encoding="utf-8")
        from graphiti_core.nodes import EpisodeType  # local import to avoid top-level dep at schema time

        await self.service.graphiti.add_episode(
            name=path,
            episode_body=content,
            source=EpisodeType.text,
            source_description=f"Ingested from subject repo: {path}",
            reference_time=datetime.now(timezone.utc),
            group_id=self.service.config.subject_name,
            entity_types=self.service.entity_types,
        )
        return {"path": path, "bytes_ingested": len(content.encode("utf-8"))}

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
        if last_ingestion_scan:
            node.attributes["last_ingestion_scan"] = now
            updated["last_ingestion_scan"] = now
        if updated:
            await node.save(self.service.graphiti.driver)
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


# --- Dispatcher ---


def build_handler_map(tools: AgentTools) -> dict[str, Any]:
    """Map tool names to bound async methods on ``tools``.

    The set of names here MUST match ``TOOL_SCHEMAS`` — mismatch is a
    bug. Tested by test_synthesis_agent_tools.
    """
    return {
        "read_file": tools.read_file,
        "write_file": tools.write_file,
        "recall": tools.recall,
        "git_status": tools.git_status,
        "git_branch_exists": tools.git_branch_exists,
        "git_create_branch": tools.git_create_branch,
        "git_checkout": tools.git_checkout,
        "git_add_and_commit": tools.git_add_and_commit,
        "git_rebase_onto": tools.git_rebase_onto,
        "git_rebase_abort": tools.git_rebase_abort,
        "git_diff": tools.git_diff,
        "ingest_file": tools.ingest_file,
        "update_synthesis_metadata": tools.update_synthesis_metadata,
        "finish": tools.finish,
    }


def tool_schema_names() -> set[str]:
    """Names declared in TOOL_SCHEMAS — for cross-check with handler map."""
    return {t["name"] for t in TOOL_SCHEMAS}


def json_serializable(value: Any) -> str:
    """Convert a tool result to JSON text for inclusion in a tool_result block."""
    return json.dumps(value, default=str)
