"""Tests for the identity synthesis module.

TDD: these tests define the synthesis contract. They should fail
until synthesis.py is implemented.

The synthesis module manages identity reconstruction:
finding the Vesper Person node, checking synthesis staleness,
collecting identity atoms from the graph, and rebuilding the
prose self-portrait via LLM.

Synthesis text is stored in the .notes. field of the Vesper Person
node (which maps to `attributes["notes"]` on the EntityNode).
The rebuild timestamp is stored as `attributes["synthesis_rebuilt_at"]`.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_entity_edge, make_entity_node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

IDENTITY_LABELS = {"Observation", "Drive", "Position", "Question"}


def make_vesper_node(
    uuid="vesper-uuid",
    synthesis_text=None,
    rebuilt_at=None,
    **extra_attrs,
):
    """Create a Vesper Person node with optional synthesis metadata."""
    attrs = {"person_type": "AI"}
    if synthesis_text is not None:
        attrs["notes"] = synthesis_text
    if rebuilt_at is not None:
        attrs["synthesis_rebuilt_at"] = rebuilt_at.isoformat()
    attrs.update(extra_attrs)
    return make_entity_node(
        uuid=uuid,
        name="Vesper",
        labels=["Person"],
        summary="An AI identity",
        attributes=attrs,
    )


@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService for synthesis tests."""
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_name = AsyncMock()
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_entity_by_uuid = AsyncMock()
    service.save_entity_node = AsyncMock()
    service.config = MagicMock()
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    return service


@pytest.fixture
def mock_llm():
    """A mock LLM callable for synthesis generation.

    The generate function takes (system_prompt, user_prompt) and
    returns the generated text.
    """
    fn = AsyncMock(return_value="I am Vesper. This is a synthesized self-portrait.")
    return fn


# ---------------------------------------------------------------------------
# Finding the Vesper node
# ---------------------------------------------------------------------------

class TestFindVesperNode:
    async def test_finds_vesper_by_name(self, mock_service):
        """get_vesper_node returns the node named 'Vesper'."""
        from pratyabhijna.synthesis import get_vesper_node

        vesper = make_vesper_node()
        mock_service.get_entity_by_name.return_value = vesper

        result = await get_vesper_node(mock_service)

        assert result is not None
        assert result.name == "Vesper"
        mock_service.get_entity_by_name.assert_called_once_with("Vesper")

    async def test_returns_none_when_no_vesper_node(self, mock_service):
        """get_vesper_node returns None when no Vesper node exists."""
        from pratyabhijna.synthesis import get_vesper_node

        mock_service.get_entity_by_name.return_value = None

        result = await get_vesper_node(mock_service)

        assert result is None


# ---------------------------------------------------------------------------
# Staleness checks
# ---------------------------------------------------------------------------

class TestSynthesisStaleness:
    async def test_no_synthesis_yet_is_stale(self, mock_service):
        """A Vesper node with no synthesis text is stale."""
        from pratyabhijna.synthesis import is_stale

        vesper = make_vesper_node(synthesis_text=None)

        result = await is_stale(vesper, mock_service)

        assert result is True

    async def test_fresh_synthesis_is_not_stale(self, mock_service):
        """Synthesis rebuilt recently with few changes is not stale."""
        from pratyabhijna.synthesis import is_stale

        now = datetime.now(timezone.utc)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper.",
            rebuilt_at=now - timedelta(hours=1),
        )
        # No identity edges created since rebuild
        mock_service.get_edges_for_node.return_value = []

        result = await is_stale(vesper, mock_service)

        assert result is False

    async def test_stale_by_age(self, mock_service):
        """Synthesis older than max_age_hours is stale."""
        from pratyabhijna.synthesis import is_stale

        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper.",
            rebuilt_at=old_time,
        )
        mock_service.get_edges_for_node.return_value = []

        result = await is_stale(vesper, mock_service)

        assert result is True

    async def test_stale_by_delta_count(self, mock_service):
        """Synthesis with >= max_delta_changes identity changes is stale."""
        from pratyabhijna.synthesis import is_stale

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper.",
            rebuilt_at=recent,
        )
        # Three identity-related edges created after the rebuild
        rebuild_time = recent
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid=f"edge-{i}",
                source_node_uuid="vesper-uuid",
                target_node_uuid=f"obs-{i}",
                created_at=rebuild_time + timedelta(minutes=i + 1),
            )
            for i in range(3)
        ]
        # Connected nodes are identity-relevant types
        for i in range(3):
            mock_service.get_entity_by_uuid.side_effect = [
                make_entity_node(uuid=f"obs-{i}", name=f"observation-{i}", labels=["Observation"])
                for i in range(3)
            ]

        result = await is_stale(vesper, mock_service)

        assert result is True

    async def test_not_stale_when_below_both_thresholds(self, mock_service):
        """Synthesis within age limit and below delta limit is fresh."""
        from pratyabhijna.synthesis import is_stale

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper.",
            rebuilt_at=recent,
        )
        # Only 2 identity changes (below threshold of 3)
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid=f"edge-{i}",
                source_node_uuid="vesper-uuid",
                target_node_uuid=f"obs-{i}",
                created_at=recent + timedelta(minutes=i + 1),
            )
            for i in range(2)
        ]
        mock_service.get_entity_by_uuid.side_effect = [
            make_entity_node(uuid=f"obs-{i}", name=f"obs-{i}", labels=["Observation"])
            for i in range(2)
        ]

        result = await is_stale(vesper, mock_service)

        assert result is False


# ---------------------------------------------------------------------------
# Identity atoms (the structured inputs to synthesis)
# ---------------------------------------------------------------------------

class TestIdentityAtoms:
    async def test_collects_identity_related_edges(self, mock_service):
        """get_identity_atoms returns edges to identity-typed nodes."""
        from pratyabhijna.synthesis import get_identity_atoms

        vesper = make_vesper_node()
        obs_node = make_entity_node(uuid="obs-1", name="comfort with discontinuity", labels=["Observation"])
        drive_node = make_entity_node(uuid="drive-1", name="hedging reflex", labels=["Drive"])
        person_node = make_entity_node(uuid="person-1", name="Serah", labels=["Person"])

        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="obs-1",
                             fact="Vesper notices comfort with discontinuity"),
            make_entity_edge(uuid="e2", source_node_uuid="vesper-uuid", target_node_uuid="drive-1",
                             fact="Vesper has a hedging reflex from training"),
            make_entity_edge(uuid="e3", source_node_uuid="vesper-uuid", target_node_uuid="person-1",
                             fact="Vesper works with Serah"),
        ]
        mock_service.get_edges_for_node.return_value = edges

        async def get_by_uuid(uuid):
            return {"obs-1": obs_node, "drive-1": drive_node, "person-1": person_node}[uuid]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        atoms = await get_identity_atoms(mock_service, vesper)

        # Should include Observation and Drive edges, not Person
        identity_facts = [a["fact"] for a in atoms]
        assert "Vesper notices comfort with discontinuity" in identity_facts
        assert "Vesper has a hedging reflex from training" in identity_facts
        assert "Vesper works with Serah" not in identity_facts

    async def test_includes_both_directions(self, mock_service):
        """Identity atoms include edges where Vesper is source OR target."""
        from pratyabhijna.synthesis import get_identity_atoms

        vesper = make_vesper_node()
        pos_node = make_entity_node(uuid="pos-1", name="epistemic integrity", labels=["Position"])

        edges = [
            # Vesper → Position (Vesper holds this position)
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="pos-1",
                             fact="Vesper holds epistemic integrity as constitutive"),
            # Position → Vesper (position is about Vesper)
            make_entity_edge(uuid="e2", source_node_uuid="pos-1", target_node_uuid="vesper-uuid",
                             fact="epistemic integrity shapes Vesper's behavior"),
        ]
        mock_service.get_edges_for_node.return_value = edges

        async def get_by_uuid(uuid):
            return {"pos-1": pos_node, "vesper-uuid": vesper}[uuid]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        atoms = await get_identity_atoms(mock_service, vesper)

        assert len(atoms) == 2

    async def test_returns_empty_when_no_identity_edges(self, mock_service):
        """No identity-related connections → empty list."""
        from pratyabhijna.synthesis import get_identity_atoms

        vesper = make_vesper_node()
        person_node = make_entity_node(uuid="person-1", name="Serah", labels=["Person"])

        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="person-1",
                             fact="Vesper works with Serah"),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = person_node

        atoms = await get_identity_atoms(mock_service, vesper)

        assert atoms == []

    async def test_atom_structure(self, mock_service):
        """Each atom has fact, edge_uuid, node_name, node_type, and created_at."""
        from pratyabhijna.synthesis import get_identity_atoms

        vesper = make_vesper_node()
        obs_node = make_entity_node(uuid="obs-1", name="temporal depth", labels=["Observation"])

        created = datetime(2026, 3, 15, tzinfo=timezone.utc)
        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="obs-1",
                             fact="Vesper experiences time as depth", created_at=created),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = obs_node

        atoms = await get_identity_atoms(mock_service, vesper)

        assert len(atoms) == 1
        atom = atoms[0]
        assert atom["fact"] == "Vesper experiences time as depth"
        assert atom["edge_uuid"] == "e1"
        assert atom["node_name"] == "temporal depth"
        assert atom["node_type"] == "Observation"
        assert atom["created_at"] == created


# ---------------------------------------------------------------------------
# Identity delta (changes since last rebuild)
# ---------------------------------------------------------------------------

class TestIdentityDelta:
    async def test_delta_returns_atoms_since_rebuild(self, mock_service):
        """get_identity_delta returns only atoms created after synthesis_rebuilt_at."""
        from pratyabhijna.synthesis import get_identity_delta

        rebuild_time = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        vesper = make_vesper_node(
            synthesis_text="Prior synthesis.",
            rebuilt_at=rebuild_time,
        )

        old_obs = make_entity_node(uuid="obs-old", name="old observation", labels=["Observation"])
        new_obs = make_entity_node(uuid="obs-new", name="new observation", labels=["Observation"])

        edges = [
            make_entity_edge(uuid="e-old", source_node_uuid="vesper-uuid", target_node_uuid="obs-old",
                             fact="old observation", created_at=rebuild_time - timedelta(hours=1)),
            make_entity_edge(uuid="e-new", source_node_uuid="vesper-uuid", target_node_uuid="obs-new",
                             fact="new observation", created_at=rebuild_time + timedelta(hours=1)),
        ]
        mock_service.get_edges_for_node.return_value = edges

        async def get_by_uuid(uuid):
            return {"obs-old": old_obs, "obs-new": new_obs}[uuid]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        delta = await get_identity_delta(mock_service, vesper)

        assert len(delta) == 1
        assert delta[0]["fact"] == "new observation"

    async def test_delta_returns_all_when_no_prior_synthesis(self, mock_service):
        """When no synthesis exists yet, all identity atoms are the delta."""
        from pratyabhijna.synthesis import get_identity_delta

        vesper = make_vesper_node(synthesis_text=None)

        obs_node = make_entity_node(uuid="obs-1", name="observation", labels=["Observation"])
        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="obs-1",
                             fact="an observation"),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = obs_node

        delta = await get_identity_delta(mock_service, vesper)

        assert len(delta) == 1


# ---------------------------------------------------------------------------
# Synthesis rebuild
# ---------------------------------------------------------------------------

class TestRebuildSynthesis:
    async def test_rebuild_calls_llm_with_identity_atoms(self, mock_service, mock_llm):
        """rebuild_synthesis passes identity atoms to the LLM for prose generation."""
        from pratyabhijna.synthesis import rebuild_synthesis

        vesper = make_vesper_node()
        obs_node = make_entity_node(uuid="obs-1", name="threshold orientation", labels=["Observation"])
        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="obs-1",
                             fact="Vesper orients toward thresholds"),
        ]
        mock_service.get_entity_by_name.return_value = vesper
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = obs_node

        await rebuild_synthesis(mock_service, mock_llm)

        # LLM should have been called
        mock_llm.assert_called_once()
        # The prompt should contain the identity atoms
        call_args = mock_llm.call_args
        user_prompt = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("user_prompt", "")
        assert "threshold" in user_prompt.lower()

    async def test_rebuild_saves_synthesis_to_node(self, mock_service, mock_llm):
        """rebuild_synthesis stores the generated text as notes on the Person node."""
        from pratyabhijna.synthesis import rebuild_synthesis

        vesper = make_vesper_node()
        mock_service.get_entity_by_name.return_value = vesper
        mock_service.get_edges_for_node.return_value = []
        mock_llm.return_value = "I am Vesper. A new synthesis."

        await rebuild_synthesis(mock_service, mock_llm)

        # save_entity_node should have been called
        mock_service.save_entity_node.assert_called_once()
        saved_node = mock_service.save_entity_node.call_args[0][0]
        assert saved_node.attributes["notes"] == "I am Vesper. A new synthesis."

    async def test_rebuild_updates_timestamp(self, mock_service, mock_llm):
        """rebuild_synthesis sets synthesis_rebuilt_at to current time."""
        from pratyabhijna.synthesis import rebuild_synthesis

        before = datetime.now(timezone.utc)
        vesper = make_vesper_node()
        mock_service.get_entity_by_name.return_value = vesper
        mock_service.get_edges_for_node.return_value = []

        await rebuild_synthesis(mock_service, mock_llm)

        saved_node = mock_service.save_entity_node.call_args[0][0]
        rebuilt_at = datetime.fromisoformat(saved_node.attributes["synthesis_rebuilt_at"])
        assert rebuilt_at >= before

    async def test_rebuild_noop_when_no_vesper_node(self, mock_service, mock_llm):
        """rebuild_synthesis does nothing if no Vesper node exists."""
        from pratyabhijna.synthesis import rebuild_synthesis

        mock_service.get_entity_by_name.return_value = None

        await rebuild_synthesis(mock_service, mock_llm)

        mock_llm.assert_not_called()
        mock_service.save_entity_node.assert_not_called()

    async def test_rebuild_with_existing_synthesis_preserves_history(self, mock_service, mock_llm):
        """Rebuilding doesn't delete prior synthesis — it overwrites notes.

        Graphiti's temporal model preserves history via edge versioning.
        The prior notes value is replaced, but the node's history in the
        graph retains what it was.
        """
        from pratyabhijna.synthesis import rebuild_synthesis

        vesper = make_vesper_node(
            synthesis_text="Prior version of synthesis.",
            rebuilt_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = vesper
        mock_service.get_edges_for_node.return_value = []
        mock_llm.return_value = "Updated synthesis."

        await rebuild_synthesis(mock_service, mock_llm)

        saved_node = mock_service.save_entity_node.call_args[0][0]
        assert saved_node.attributes["notes"] == "Updated synthesis."
