"""Create the subject's Person node in the graph.

The subject node is the anchor for identity-atom edges — observations,
drives, positions, and questions connect to it. The tier text itself
(SOUL, IDENTITY, USER, THREADS, CHRONICLE) lives in the subject's repo,
not on this node. Seeding therefore needs only the subject's name.

This is a deliberate CLI action, not an MCP tool — a Person node is
meant to be created once per deployment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from graphiti_core.nodes import EntityNode

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import get_subject_node

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService

log = get_logger(__name__)


async def seed_subject(service: PratyabhijnaService) -> dict:
    """Ensure the subject's Person node exists in the graph.

    Creates the node with the configured subject name if it doesn't
    exist yet. No-ops and reports ``"exists"`` when already present.

    Tier content (soul, identity, etc.) is not stored on the node —
    files in the subject's repo are canonical. This is just the anchor.
    """
    subject_name = service.config.subject_name
    node = await get_subject_node(service)
    if node is not None:
        log.info("Seed: subject node '%s' already exists", subject_name)
        return {"subject": subject_name, "action": "exists"}

    await _create_subject_node(service, subject_name)
    log.info("Seed: created subject node '%s'", subject_name)
    return {"subject": subject_name, "action": "created"}


async def _create_subject_node(
    service: PratyabhijnaService,
    name: str,
) -> EntityNode:
    """Create a new subject Person node with minimal attributes."""
    node = EntityNode(
        uuid=str(uuid4()),
        name=name,
        group_id="default",
        labels=["Person"],
        created_at=datetime.now(timezone.utc),
        name_embedding=None,
        summary="An AI identity",
        attributes={"person_type": "AI"},
    )
    await node.generate_name_embedding(service._graphiti.embedder)
    await node.save(service._graphiti.driver)
    return node
