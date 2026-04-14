"""The ``bootstrap`` MCP tool.

Returns the subject's identity tiers from repo files plus synthesis
metadata (context_rebuilt_at, delta) from the Person node.

The tier text itself lives only in the subject's repo — the graph no
longer duplicates SOUL/IDENTITY/USER/THREADS/CHRONICLE as Person-node
attributes. Files are canonical. The Person node carries synthesis
metadata and anchors identity-atom edges.
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

    Reads identity files from the repo (``config.resources.repo_path``)
    and combines them with synthesis metadata from the Person node. All
    five tier fields (``soul``, ``identity``, ``user``, ``threads``,
    ``chronicle``) are always present in the response; values are None
    when the corresponding file is missing or ``repo_path`` is unset.
    """
    node = await get_subject_node(service)
    files = read_identity_files(service.config.resources.repo_path)
    tiers = {key: (files.get(key) if files else None) for key in IDENTITY_FILES}

    base = {"subject": service.config.subject_name, **tiers}

    if node is None:
        return {
            **base,
            "context_rebuilt_at": None,
            "delta": [],
            "message": (
                f"No Person node found for '{service.config.subject_name}'. "
                "The subject node must be created before bootstrap can "
                "return synthesis metadata or delta."
            ),
        }

    return {
        **base,
        "context_rebuilt_at": node.attributes.get("context_rebuilt_at"),
        "delta": await get_identity_delta(service, node),
    }
