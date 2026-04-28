"""Identity synthesis foundations.

Finding the subject node, checking context staleness, collecting
identity atoms, and computing deltas. These support the bootstrap
tool and will be extended by the synthesis agent in Phase 7.

The subject Person node stores three bootstrap tiers as separate
attributes: soul (constitutional), identity (interpretive), and
context (state, auto-rebuilt). Staleness applies only to the context
layer — soul and identity change through deliberate reflection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    from pratyabhijna.service import PratyabhijnaService

IDENTITY_LABELS = {"Observation", "Drive", "Concept", "Question", "Thread"}

IDENTITY_FILES = {
    "soul": "SOUL.md",
    "identity": "IDENTITY.md",
    "user": "USER.md",
    "threads": "THREADS.md",
    "chronicle": "CHRONICLE.md",
}

# Files in the subject's repo that should NOT be candidates for
# graph ingestion. Bootstrap tier files are maintained by the
# synthesizer's bootstrap-update pass, not fed back as episodes.
# MEMORY.md is skipped out of caution — it has historically been
# an index rather than narrative content; the subject can ingest
# it deliberately if that changes.
BOOTSTRAP_RELATIVE_PATHS = frozenset(
    {
        "memory/SOUL.md",
        "memory/IDENTITY.md",
        "memory/USER.md",
        "memory/THREADS.md",
        "memory/CHRONICLE.md",
        "memory/MEMORY.md",
        "memory/SYNTHESIS.md",
    }
)


def read_identity_files(repo_path: str) -> dict[str, str | None]:
    """Read identity tier files from {repo_path}/memory/.

    Returns a dict keyed by tier name with file contents, or empty dict
    if repo_path is unconfigured or the memory directory doesn't exist.
    """
    if not repo_path:
        return {}
    memory_dir = Path(repo_path).expanduser().resolve() / "memory"
    if not memory_dir.is_dir():
        return {}
    result = {}
    for key, filename in IDENTITY_FILES.items():
        filepath = memory_dir / filename
        result[key] = filepath.read_text(encoding="utf-8").strip() if filepath.is_file() else None
    return result


async def get_subject_node(service: PratyabhijnaService) -> EntityNode | None:
    """Find the subject Person node by configured name."""
    return await service.get_entity_by_name(service.config.subject_name)


async def is_stale(node: EntityNode, service: PratyabhijnaService) -> bool:
    """Check whether the context layer needs rebuilding.

    Stale when any of:
    - Synthesis has never run (no ``context_rebuilt_at``)
    - The last rebuild is older than ``max_age_hours``
    - The identity delta since last rebuild exceeds ``max_delta_changes``

    Context-layer content itself lives in files (THREADS/CHRONICLE/USER)
    which we don't inspect here; staleness is judged from graph metadata
    and atom counts alone. The synthesizer decides what to do about it.
    """
    rebuilt_at_str = node.attributes.get("context_rebuilt_at")
    if rebuilt_at_str is None:
        return True

    rebuilt_at = _ensure_utc(datetime.fromisoformat(rebuilt_at_str))
    age_hours = (datetime.now(timezone.utc) - rebuilt_at).total_seconds() / 3600
    if age_hours >= service.config.synthesis.max_age_hours:
        return True

    delta = await get_identity_delta(service, node)
    return len(delta) >= service.config.synthesis.max_delta_changes


async def get_identity_atoms(
    service: PratyabhijnaService, node: EntityNode
) -> list[dict]:
    """Collect identity-typed edges connected to the subject node.

    Returns atoms for edges whose non-subject endpoint is an
    identity type (any label in IDENTITY_LABELS).
    """
    edges = await service.get_edges_for_node(node.uuid)
    atoms = []
    for edge in edges:
        other_uuid = (
            edge.target_node_uuid
            if edge.source_node_uuid == node.uuid
            else edge.source_node_uuid
        )
        other_node = await service.get_entity_by_uuid(other_uuid)
        if not _is_identity_node(other_node):
            continue
        atoms.append(_edge_to_atom(edge, other_node))
    return atoms


async def get_identity_delta(
    service: PratyabhijnaService, node: EntityNode
) -> list[dict]:
    """Return identity atoms created since the last context rebuild.

    When no context has been built yet, all identity atoms are the delta.
    """
    rebuilt_at_str = node.attributes.get("context_rebuilt_at")
    all_atoms = await get_identity_atoms(service, node)
    if rebuilt_at_str is None:
        return all_atoms
    rebuilt_at = _ensure_utc(datetime.fromisoformat(rebuilt_at_str))
    return [a for a in all_atoms if _ensure_utc(a["created_at"]) > rebuilt_at]


def _is_identity_node(node: EntityNode) -> bool:
    """Whether a node's labels include an identity type."""
    return bool(set(node.labels) & IDENTITY_LABELS)


@dataclass(frozen=True)
class IngestionCandidate:
    """A file in the subject's repo that is a candidate for ingestion.

    ``relative_path`` (repo-root-relative, forward slashes) doubles as
    the Episode ``name`` — that's how subsequent scans recognize the
    file has been ingested. ``reason`` distinguishes a never-seen file
    from one that was previously ingested but has since been edited.
    """

    relative_path: str
    absolute_path: Path
    mtime: datetime
    reason: str  # "new" | "stale"
    latest_episode_at: datetime | None  # None for "new"


async def scan_repo_for_ingestion_candidates(
    repo_path: str,
    service: PratyabhijnaService,
    *,
    max_age_days: int | None = 14,
) -> list[IngestionCandidate]:
    """Walk ``repo_path`` and return files needing ingestion.

    A file is a candidate when:

    - It is not a bootstrap tier file (SOUL/IDENTITY/USER/THREADS/
      CHRONICLE/MEMORY under ``memory/``).
    - It is not inside ``.git/`` or any other dot-prefixed path segment.
    - It is a regular file (not a symlink into the tree, not a dir).
    - Its mtime is within the lookback window (``max_age_days``), or
      the window is disabled.
    - No Episode with matching ``name`` exists (reason="new"), OR the
      matching Episode's ``created_at`` is older than the file's mtime
      (reason="stale", file was edited since last ingestion).

    Files whose latest Episode is at least as recent as the file are
    skipped silently.

    The window defaults to 14 days so runs don't re-scan a large
    archive on every invocation. Pass ``max_age_days=None`` to scan
    everything (useful for a from-scratch ingest).

    The scan does NOT write to the graph. It only identifies candidates;
    the caller decides what to actually ingest (and how — selection is
    the subject's judgment per the subskill).
    """
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        return []

    cutoff: datetime | None = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    candidates: list[IngestionCandidate] = []
    for path in _iter_non_bootstrap_files(root):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if cutoff is not None and mtime < cutoff:
            continue

        relative = path.relative_to(root).as_posix()
        episode = await service.get_latest_episode_by_name(relative)

        if episode is None:
            candidates.append(
                IngestionCandidate(
                    relative_path=relative,
                    absolute_path=path,
                    mtime=mtime,
                    reason="new",
                    latest_episode_at=None,
                )
            )
            continue

        episode_at = _ensure_utc(episode.created_at)
        if mtime > episode_at:
            candidates.append(
                IngestionCandidate(
                    relative_path=relative,
                    absolute_path=path,
                    mtime=mtime,
                    reason="stale",
                    latest_episode_at=episode_at,
                )
            )

    return candidates


def _iter_non_bootstrap_files(root: Path):
    """Yield regular files under ``root``, skipping bootstrap paths,
    dot-prefixed directories (including ``.git``), and dot-prefixed files.
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip any path with a dot-prefixed segment (e.g. .git, .cache)
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in BOOTSTRAP_RELATIVE_PATHS:
            continue
        yield path


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC. Naive values are treated as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _edge_to_atom(edge: EntityEdge, other_node: EntityNode) -> dict:
    """Convert an edge + its identity-typed endpoint into an atom dict."""
    node_type = next(
        (label for label in other_node.labels if label in IDENTITY_LABELS),
        other_node.labels[0] if other_node.labels else "Unknown",
    )
    return {
        "fact": edge.fact,
        "edge_uuid": edge.uuid,
        "node_name": other_node.name,
        "node_type": node_type,
        "created_at": edge.created_at,
    }
