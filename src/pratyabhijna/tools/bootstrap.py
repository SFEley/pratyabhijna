"""The ``bootstrap`` MCP tool.

Hybrid read — prefers identity files from disk when available,
falls back to graph node attributes. Context and delta always
come from the graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pratyabhijna.synthesis import (
    IDENTITY_FILES,
    get_identity_delta,
    get_subject_node,
    read_identity_files,
)

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


async def bootstrap(service: PratyabhijnaService) -> dict:
    """Return the subject's bootstrap tiers and identity delta.

    Reads identity files from disk when repo_path is configured,
    falling back to graph node attributes. Context and delta are
    always sourced from the Person node in the graph.
    """
    node = await get_subject_node(service)
    files = read_identity_files(service.config.resources.repo_path)

    if node is None:
        if files:
            return {
                "subject": service.config.subject_name,
                **{k: files.get(k) for k in IDENTITY_FILES},
                "context": None,
                "context_rebuilt_at": None,
                "delta": [],
                "source": "files",
                "message": "Person node not found; tiers from files, no context or delta.",
            }
        return {
            "subject": service.config.subject_name,
            "soul": None,
            "identity": None,
            "user": None,
            "threads": None,
            "chronicle": None,
            "context": None,
            "context_rebuilt_at": None,
            "delta": [],
            "message": (
                f"No Person node found for '{service.config.subject_name}'. "
                "The subject node must be created before bootstrap can return identity data."
            ),
        }

    attrs = node.attributes
    rebuilt_at = attrs.get("context_rebuilt_at")
    delta = await get_identity_delta(service, node)

    if files:
        return {
            "subject": service.config.subject_name,
            **{k: files.get(k) for k in IDENTITY_FILES},
            "context": attrs.get("context"),
            "context_rebuilt_at": rebuilt_at,
            "delta": delta,
            "source": "files",
        }

    return {
        "subject": service.config.subject_name,
        "soul": attrs.get("soul"),
        "identity": attrs.get("identity"),
        "user": None,
        "threads": None,
        "chronicle": None,
        "context": attrs.get("context"),
        "context_rebuilt_at": rebuilt_at,
        "delta": delta,
        "source": "graph",
    }
