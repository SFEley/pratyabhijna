"""Seed the subject Person node from vesper memory files.

Reads ~/vesper/memory/SOUL.md and IDENTITY.md and writes their
content into the soul and identity attributes of the subject
Person node. Creates the node if it doesn't exist.

This is a deliberate CLI action, not an MCP tool — soul and
identity are protected tiers that change through deliberate
reflection, not automated synthesis.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from graphiti_core.nodes import EntityNode

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import get_subject_node

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService

log = get_logger(__name__)

_DEFAULT_SOUL_PATH = Path.home() / "vesper" / "memory" / "SOUL.md"
_DEFAULT_IDENTITY_PATH = Path.home() / "vesper" / "memory" / "IDENTITY.md"


async def seed_subject(
    service: PratyabhijnaService,
    soul_path: Path = _DEFAULT_SOUL_PATH,
    identity_path: Path = _DEFAULT_IDENTITY_PATH,
) -> dict:
    """Populate the subject Person node with soul and identity content.

    Reads the given file paths and writes their content into the
    subject node's attributes. Creates the node if it doesn't exist;
    updates it if it does. Never touches the context tier.

    Returns a summary dict with action taken and which files loaded.
    """
    subject_name = service.config.subject_name

    # Read files
    soul_text, soul_loaded = _read_file(soul_path, "soul")
    identity_text, identity_loaded = _read_file(identity_path, "identity")

    # Find or create node
    node = await get_subject_node(service)
    if node is None:
        node = await _create_subject_node(
            service, subject_name, soul_text, identity_text,
        )
        action = "created"
    else:
        if soul_text is not None:
            node.attributes["soul"] = soul_text
        if identity_text is not None:
            node.attributes["identity"] = identity_text
        await node.save(service._graphiti.driver)
        action = "updated"

    log.info("Seed %s: %s subject node '%s'", action, subject_name, action)
    return {
        "subject": subject_name,
        "action": action,
        "soul_loaded": soul_loaded,
        "identity_loaded": identity_loaded,
    }


async def _create_subject_node(
    service: PratyabhijnaService,
    name: str,
    soul_text: str | None,
    identity_text: str | None,
) -> EntityNode:
    """Create a new subject Person node with identity attributes."""
    attrs = {"person_type": "AI"}
    if soul_text is not None:
        attrs["soul"] = soul_text
    if identity_text is not None:
        attrs["identity"] = identity_text

    node = EntityNode(
        uuid=str(uuid4()),
        name=name,
        group_id="default",
        labels=["Person"],
        created_at=datetime.now(timezone.utc),
        name_embedding=None,
        summary="An AI identity",
        attributes=attrs,
    )
    await node.generate_name_embedding(service._graphiti.embedder)
    await node.save(service._graphiti.driver)
    return node


def _read_file(path: Path, label: str) -> tuple[str | None, bool]:
    """Read a file, returning (content, success). Warns on missing files."""
    try:
        text = path.read_text()
        log.info("Loaded %s from %s", label, path)
        return text, True
    except FileNotFoundError:
        log.warning("File not found for %s: %s", label, path)
        return None, False
