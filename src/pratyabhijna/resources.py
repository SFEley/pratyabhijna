"""Subject repo resources.

Exposes files from a subject's git repo as read-only MCP resources.
Which directories are exposed is controlled by config. Path traversal
is blocked.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)


def _safe_resolve(
    repo_path: Path,
    directory: str,
    filename: str | None = None,
    *,
    allowed: frozenset[str],
) -> Path:
    """Resolve a path within the repo, raising on traversal attempts."""
    if directory not in allowed:
        raise ValueError(f"Directory not exposed: {directory}")

    target = repo_path / directory
    if filename is not None:
        target = target / filename

    resolved = target.resolve()
    allowed_root = (repo_path / directory).resolve()

    if not resolved.is_relative_to(allowed_root):
        raise ValueError(f"Path traversal blocked: {directory}/{filename}")

    return resolved


def register_resources(
    server: FastMCP,
    repo_path: str | None,
    directories: list[str] | None = None,
) -> None:
    """Register pratya:// resources on the server. No-op if repo_path is empty."""
    if not repo_path:
        return
    if not directories:
        return

    resolved = Path(repo_path).expanduser().resolve()

    if not resolved.is_dir():
        log.warning("Resources repo_path does not exist: %s", repo_path)
        return

    allowed = frozenset(directories)

    log.info("Registering pratya:// resources from %s (directories: %s)", resolved, directories)

    @server.resource(
        "pratya://",
        name="pratya-root",
        description="List exposed directories in the subject's repo",
        mime_type="application/json",
    )
    def pratya_root() -> str:
        dirs = []
        for name in sorted(allowed):
            d = resolved / name
            if d.is_dir():
                file_count = sum(1 for f in d.iterdir() if f.is_file())
            else:
                file_count = 0
            dirs.append({"name": name, "file_count": file_count})
        return json.dumps({"directories": dirs})

    @server.resource(
        "pratya://{directory}",
        name="pratya-directory",
        description="List files in a content directory",
        mime_type="application/json",
    )
    def pratya_directory(directory: str) -> str:
        dir_path = _safe_resolve(resolved, directory, allowed=allowed)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"Directory not found: {directory}")

        files = []
        for f in sorted(dir_path.iterdir()):
            if not f.is_file():
                continue
            stat = f.stat()
            files.append({
                "name": f.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc,
                ).isoformat(),
            })
        return json.dumps(files)

    @server.resource(
        "pratya://{directory}/{filename}",
        name="pratya-file",
        description="Read a file from the subject's repo",
        mime_type="text/markdown",
    )
    def pratya_file(directory: str, filename: str) -> str:
        file_path = _safe_resolve(resolved, directory, filename, allowed=allowed)
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: {directory}/{filename}")
        return file_path.read_text(encoding="utf-8")
