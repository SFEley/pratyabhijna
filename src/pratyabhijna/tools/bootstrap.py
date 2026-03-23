"""The ``bootstrap`` MCP tool.

Pure read — returns the three-tier identity text (soul, identity,
context) from the subject Person node plus a delta of identity
changes since the last context rebuild. No side effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pratyabhijna.synthesis import get_identity_delta, get_subject_node

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


async def bootstrap(service: PratyabhijnaService) -> dict:
    """Return the subject's bootstrap tiers and identity delta.

    Args:
        service: The PratyabhijnaService instance.

    Returns:
        Dict with subject name, three tier texts (soul, identity,
        context), context_rebuilt_at timestamp, and delta list.
    """
    node = await get_subject_node(service)
    if node is None:
        return {
            "subject": service.config.subject_name,
            "soul": None,
            "identity": None,
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
    delta = await get_identity_delta(service, node) if node else []

    return {
        "subject": service.config.subject_name,
        "soul": attrs.get("soul"),
        "identity": attrs.get("identity"),
        "context": attrs.get("context"),
        "context_rebuilt_at": rebuilt_at,
        "delta": delta,
    }
