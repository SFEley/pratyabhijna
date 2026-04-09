"""Live integration tests for the bootstrap tool and synthesis module.

These tests create a real subject Person node in Neo4j, seed identity-
related episodes, and verify that bootstrap returns the three-tier
fields and delta correctly.

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
    get_identity_atoms,
    get_identity_delta,
    get_subject_node,
    is_stale,
)
from pratyabhijna.tools.bootstrap import bootstrap


# ---------------------------------------------------------------------------
# Skip unless --live
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.skipif(
        "not config.getoption('--live')",
        reason="Live tests require --live flag",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def service():
    """A real PratyabhijnaService connected to Neo4j.

    Clears all graph data before yielding so each test run
    starts from a clean slate.
    """
    config = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(config)
    await svc.start()

    # Wipe the graph so tests don't leak state between runs
    await svc._graphiti.driver.execute_query("MATCH (n) DETACH DELETE n")

    yield svc
    await svc.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def subject_node(service):
    """Create the subject Person node with three-tier bootstrap fields.

    Saves a real EntityNode to Neo4j with soul, identity, and context
    attributes, plus a name embedding so search can find it.
    """
    now = datetime.now(timezone.utc)
    node = EntityNode(
        uuid=str(uuid_mod.uuid4()),
        name=service.config.subject_name,
        group_id="default",
        labels=["Person"],
        created_at=now,
        name_embedding=None,
        summary="An AI identity",
        attributes={
            "person_type": "AI",
            "soul": "I am the test subject. I chose this name.",
            "identity": "I orient toward thresholds.",
            "context": "Currently running integration tests.",
            "context_rebuilt_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    await node.generate_name_embedding(service._graphiti.embedder)
    await node.save(service._graphiti.driver)
    return node


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded_subject(service, subject_node):
    """Subject node with an identity-related episode seeded.

    Adds an episode mentioning the subject and an observation,
    which should produce identity-typed edges for delta detection.
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
    )
    return subject_node


# ---------------------------------------------------------------------------
# Synthesis module — live
# ---------------------------------------------------------------------------

class TestLiveSynthesis:
    async def test_get_subject_node_finds_real_node(self, service, subject_node):
        """get_subject_node finds the subject by name in a real graph."""
        found = await get_subject_node(service)

        assert found is not None
        assert found.name == service.config.subject_name

    async def test_subject_has_three_tier_attributes(self, service, subject_node):
        """The subject node's attributes contain all three bootstrap tiers."""
        found = await get_subject_node(service)

        assert found.attributes.get("soul") is not None
        assert found.attributes.get("identity") is not None
        assert found.attributes.get("context") is not None
        assert found.attributes.get("context_rebuilt_at") is not None

    async def test_is_stale_with_recent_context(self, service, subject_node):
        """A recently-rebuilt context is not stale."""
        found = await get_subject_node(service)
        result = await is_stale(found, service)

        assert result is False

    async def test_identity_atoms_after_seeding(self, service, seeded_subject):
        """After seeding an identity episode, identity atoms are present."""
        found = await get_subject_node(service)
        atoms = await get_identity_atoms(service, found)

        # The seeded episode should produce at least one identity-typed edge
        # (Observation about comfort with discontinuity)
        assert len(atoms) > 0
        types = {a["node_type"] for a in atoms}
        assert types & {"Observation", "Drive", "Position", "Question"}, (
            f"Expected identity-typed atoms, got types: {types}"
        )

    async def test_identity_delta_detects_new_atoms(self, service, seeded_subject):
        """Delta includes atoms from the seeded episode (created after context_rebuilt_at)."""
        found = await get_subject_node(service)
        delta = await get_identity_delta(service, found)

        # The episode was seeded after context_rebuilt_at, so its atoms
        # should appear in the delta
        assert len(delta) > 0


# ---------------------------------------------------------------------------
# Bootstrap tool — live
# ---------------------------------------------------------------------------

class TestLiveBootstrap:
    async def test_bootstrap_returns_three_tiers(self, service, subject_node):
        """bootstrap() returns soul, identity, and context from the real graph."""
        result = await bootstrap(service=service)

        assert result["subject"] == service.config.subject_name
        assert result["soul"] == "I am the test subject. I chose this name."
        assert result["identity"] == "I orient toward thresholds."
        assert result["context"] == "Currently running integration tests."

    async def test_bootstrap_returns_context_rebuilt_at(self, service, subject_node):
        """bootstrap() returns the context_rebuilt_at timestamp."""
        result = await bootstrap(service=service)

        assert result["context_rebuilt_at"] is not None
        # Should be a valid ISO timestamp
        datetime.fromisoformat(result["context_rebuilt_at"])

    async def test_bootstrap_returns_delta(self, service, seeded_subject):
        """bootstrap() includes identity atoms created since last context rebuild."""
        result = await bootstrap(service=service)

        assert len(result["delta"]) > 0
        atom = result["delta"][0]
        assert "fact" in atom
        assert "node_type" in atom
        assert atom["node_type"] in {"Observation", "Drive", "Position", "Question"}
