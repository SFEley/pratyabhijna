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

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    from pratyabhijna.service import PratyabhijnaService

IDENTITY_LABELS = {"Observation", "Drive", "Position", "Question"}

IDENTITY_FILES = {
    "soul": "SOUL.md",
    "identity": "IDENTITY.md",
    "user": "USER.md",
    "threads": "THREADS.md",
    "chronicle": "CHRONICLE.md",
}


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
    - No context exists yet
    - Context is older than max_age_hours
    - Identity delta exceeds max_delta_changes
    """
    rebuilt_at_str = node.attributes.get("context_rebuilt_at")
    if node.attributes.get("context") is None or rebuilt_at_str is None:
        return True

    rebuilt_at = datetime.fromisoformat(rebuilt_at_str)
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
    identity type (Observation, Drive, Position, Question).
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
    rebuilt_at = datetime.fromisoformat(rebuilt_at_str)
    return [a for a in all_atoms if a["created_at"] > rebuilt_at]


def _is_identity_node(node: EntityNode) -> bool:
    """Whether a node's labels include an identity type."""
    return bool(set(node.labels) & IDENTITY_LABELS)


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
