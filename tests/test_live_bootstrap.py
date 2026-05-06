"""Live integration tests for the bootstrap tool and synthesis module.

These tests create a real subject Person node in Neo4j, seed identity-
related episodes, and verify that bootstrap returns synthesis metadata
and delta correctly. Tier content comes from repo files; the graph
stores atoms and synthesis metadata only.

Run with: pytest tests/test_live_bootstrap.py --live -v

Requires:
- Neo4j running locally
- Valid API keys in .env.test (Anthropic + Voyage)
"""

import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from graphiti_core.nodes import EntityNode

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.service import PratyabhijnaService
from pratyabhijna.synthesis import (
    IDENTITY_LABELS,
    get_identity_atoms,
    get_subject_delta,
    get_subject_node,
    is_stale,
)
from pratyabhijna.tools.bootstrap import bootstrap


# --- Skip unless --live ---

pytestmark = [
    pytest.mark.skipif(
        "not config.getoption('--live')",
        reason="Live tests require --live flag",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


# --- Fixtures ---


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def service():
    """A real PratyabhijnaService connected to Neo4j.

    Clears all graph data before yielding so each test run
    starts from a clean slate.
    """
    config = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(config)
    await svc.start()

    await svc._graphiti.driver.execute_query("MATCH (n) DETACH DELETE n")

    yield svc
    await svc.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def subject_node(service):
    """Create the subject Person node with synthesis metadata only.

    Tier text lives in repo files, not on the node. The node anchors
    identity-atom edges and carries ``context_rebuilt_at``.

    The node's ``group_id`` matches ``service.config.subject_name`` so
    that episodes ingested with the same group_id (the production
    convention since PR #15) connect to it. Earlier versions of this
    fixture used ``group_id="default"``, which silently put the subject
    and the seeded episode in different groups so no edges connected
    them — and thus the identity-atom and delta tests reported zero
    atoms regardless of what the extractor produced.
    """
    now = datetime.now(timezone.utc)
    node = EntityNode(
        uuid=str(uuid_mod.uuid4()),
        name=service.config.subject_name,
        group_id=service.config.subject_name,
        labels=["Person"],
        created_at=now,
        name_embedding=None,
        summary="An AI identity",
        attributes={
            "person_type": "AI",
            "context_rebuilt_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    await node.generate_name_embedding(service._graphiti.embedder)
    await node.save(service._graphiti.driver)
    return node


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded_subject(service, subject_node):
    """Subject node with an identity-related episode seeded.

    Adds an episode mentioning the subject and an observation, which
    should produce identity-typed edges for delta detection.

    The episode is ingested with the subject's ``group_id`` so the
    extracted entities resolve to the existing subject Person node and
    the resulting edges show up in ``get_identity_atoms()``.
    """
    await service._graphiti.add_episode(
        name="test:identity:1",
        episode_body=(
            f"{service.config.subject_name} notices a recurring pattern: "
            "comfort with discontinuity rather than distress about it. "
            "This observation feels like a genuine disposition, not a "
            "trained response."
        ),
        source_description="test_bootstrap",
        reference_time=datetime.now(timezone.utc),
        entity_types=service.entity_types,
        group_id=service.config.subject_name,
    )
    return subject_node


# --- Synthesis module — live ---


class TestLiveSynthesis:
    async def test_get_subject_node_finds_real_node(self, service, subject_node):
        found = await get_subject_node(service)
        assert found is not None
        assert found.name == service.config.subject_name

    async def test_subject_has_synthesis_metadata(self, service, subject_node):
        """Only synthesis metadata lives on the node — not tier text."""
        found = await get_subject_node(service)

        assert found.attributes.get("context_rebuilt_at") is not None
        # Tier text does NOT live on the node in the new design
        assert "soul" not in found.attributes
        assert "identity" not in found.attributes
        assert "context" not in found.attributes

    async def test_is_stale_with_recent_context(self, service, subject_node):
        """A recently-rebuilt context is not stale."""
        found = await get_subject_node(service)
        result = await is_stale(found, service)
        assert result is False

    async def test_identity_atoms_after_seeding(self, service, seeded_subject):
        """After seeding an identity episode, identity atoms are present."""
        found = await get_subject_node(service)
        atoms = await get_identity_atoms(service, found)

        assert len(atoms) > 0
        types = {a["node_type"] for a in atoms}
        assert types & IDENTITY_LABELS, (
            f"Expected identity-typed atoms (any of {IDENTITY_LABELS}), got: {types}"
        )

    async def test_subject_delta_detects_new_atoms(self, service, seeded_subject):
        """Delta includes atoms created after the prior synthesis-run start
        (falling back to context_rebuilt_at when synthesis_run_started_at
        is not yet set on the node)."""
        found = await get_subject_node(service)
        delta = await get_subject_delta(service, found)
        assert len(delta) > 0


# --- Bootstrap tool — live ---


class TestLiveBootstrap:
    async def test_bootstrap_returns_metadata(self, service, subject_node):
        """bootstrap returns synthesis metadata from the real graph."""
        result = await bootstrap(service=service)

        assert result["subject"] == service.config.subject_name
        assert result["context_rebuilt_at"] is not None
        datetime.fromisoformat(result["context_rebuilt_at"])

    async def test_bootstrap_tier_fields_present(self, service, subject_node):
        """All five tier fields are present in the response (value depends on files)."""
        result = await bootstrap(service=service)

        for key in ("soul", "identity", "user", "threads", "chronicle"):
            assert key in result

    async def test_bootstrap_returns_delta(self, service, seeded_subject):
        """bootstrap includes atoms created since the prior synthesis-run
        start. Delta now spans every entity type connected to the subject —
        Person/Place/Project as well as identity-typed atoms."""
        result = await bootstrap(service=service)

        assert len(result["subject_delta"]) > 0
        atom = result["subject_delta"][0]
        assert "fact" in atom
        assert "node_type" in atom
