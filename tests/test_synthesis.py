"""Tests for the identity synthesis module (Phase 5 foundations).

TDD: these tests define the synthesis contract. They should fail
until synthesis.py is implemented.

The synthesis module manages identity reconstruction:
finding the subject Person node, checking context staleness,
collecting identity atoms from the graph, and computing deltas.

The subject Person node stores three bootstrap tiers as separate
attributes: ``soul`` (constitutional), ``identity`` (interpretive),
and ``context`` (state, auto-rebuilt). The context rebuild timestamp
is stored as ``attributes["context_rebuilt_at"]``.

Staleness checks apply to the context layer only — soul and identity
change through deliberate reflection, not automated synthesis.

The subject name is configurable via `config.subject_name` — it defaults
to "Vesper" but can be set to any name for different deployments.

Note: rebuild_synthesis and write-triggered scheduling are deferred
to Phase 7. See doc/implementation-plan.md.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDENTITY_LABELS = {"Observation", "Drive", "Position", "Concept", "Question"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    service.config.subject_name = "Vesper"
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    return service


# ---------------------------------------------------------------------------
# Finding the subject node
# ---------------------------------------------------------------------------

class TestGetSubjectNode:
    async def test_finds_subject_by_configured_name(self, mock_service):
        """get_subject_node returns the node matching config.subject_name."""
        from pratyabhijna.synthesis import get_subject_node

        vesper = make_subject_node()
        mock_service.get_entity_by_name.return_value = vesper

        result = await get_subject_node(mock_service)

        assert result is not None
        assert result.name == "Vesper"
        mock_service.get_entity_by_name.assert_called_once_with("Vesper")

    async def test_finds_subject_with_custom_name(self, mock_service):
        """get_subject_node uses config.subject_name, not a hardcoded name."""
        from pratyabhijna.synthesis import get_subject_node

        mock_service.config.subject_name = "Aria"
        aria = make_subject_node(name="Aria", uuid="aria-uuid")
        mock_service.get_entity_by_name.return_value = aria

        result = await get_subject_node(mock_service)

        assert result is not None
        assert result.name == "Aria"
        mock_service.get_entity_by_name.assert_called_once_with("Aria")

    async def test_returns_none_when_no_subject_node(self, mock_service):
        """get_subject_node returns None when no matching node exists."""
        from pratyabhijna.synthesis import get_subject_node

        mock_service.get_entity_by_name.return_value = None

        result = await get_subject_node(mock_service)

        assert result is None


# ---------------------------------------------------------------------------
# Staleness checks
# ---------------------------------------------------------------------------

class TestSynthesisStaleness:
    async def test_no_context_yet_is_stale(self, mock_service):
        """A subject node with no context synthesis is stale."""
        from pratyabhijna.synthesis import is_stale

        node = make_subject_node()

        result = await is_stale(node, mock_service)

        assert result is True

    async def test_fresh_context_is_not_stale(self, mock_service):
        """Context rebuilt recently with few changes is not stale."""
        from pratyabhijna.synthesis import is_stale

        now = datetime.now(timezone.utc)
        node = make_subject_node(
            context="Current context.",
            context_rebuilt_at=now - timedelta(hours=1),
        )
        mock_service.get_edges_for_node.return_value = []

        result = await is_stale(node, mock_service)

        assert result is False

    async def test_stale_by_age(self, mock_service):
        """Context older than max_age_hours is stale."""
        from pratyabhijna.synthesis import is_stale

        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        node = make_subject_node(
            context="Current context.",
            context_rebuilt_at=old_time,
        )
        mock_service.get_edges_for_node.return_value = []

        result = await is_stale(node, mock_service)

        assert result is True

    async def test_stale_by_delta_count(self, mock_service):
        """Context with >= max_delta_changes identity changes is stale."""
        from pratyabhijna.synthesis import is_stale

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        node = make_subject_node(
            context="Current context.",
            context_rebuilt_at=recent,
        )
        # Three identity-related edges created after the rebuild
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid=f"edge-{i}",
                source_node_uuid="subject-uuid",
                target_node_uuid=f"obs-{i}",
                created_at=recent + timedelta(minutes=i + 1),
            )
            for i in range(3)
        ]
        mock_service.get_entity_by_uuid.side_effect = [
            make_entity_node(uuid=f"obs-{i}", name=f"observation-{i}", labels=["Observation"])
            for i in range(3)
        ]

        result = await is_stale(node, mock_service)

        assert result is True

    async def test_not_stale_when_below_both_thresholds(self, mock_service):
        """Context within age limit and below delta limit is fresh."""
        from pratyabhijna.synthesis import is_stale

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        node = make_subject_node(
            context="Current context.",
            context_rebuilt_at=recent,
        )
        # Only 2 identity changes (below threshold of 3)
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid=f"edge-{i}",
                source_node_uuid="subject-uuid",
                target_node_uuid=f"obs-{i}",
                created_at=recent + timedelta(minutes=i + 1),
            )
            for i in range(2)
        ]
        mock_service.get_entity_by_uuid.side_effect = [
            make_entity_node(uuid=f"obs-{i}", name=f"obs-{i}", labels=["Observation"])
            for i in range(2)
        ]

        result = await is_stale(node, mock_service)

        assert result is False


# ---------------------------------------------------------------------------
# Identity atoms (the structured inputs to synthesis)
# ---------------------------------------------------------------------------

class TestIdentityAtoms:
    async def test_collects_identity_related_edges(self, mock_service):
        """get_identity_atoms returns edges to identity-typed nodes."""
        from pratyabhijna.synthesis import get_identity_atoms

        node = make_subject_node()
        obs_node = make_entity_node(uuid="obs-1", name="comfort with discontinuity", labels=["Observation"])
        drive_node = make_entity_node(uuid="drive-1", name="hedging reflex", labels=["Drive"])
        person_node = make_entity_node(uuid="person-1", name="Serah", labels=["Person"])

        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="subject-uuid", target_node_uuid="obs-1",
                             fact="Vesper notices comfort with discontinuity"),
            make_entity_edge(uuid="e2", source_node_uuid="subject-uuid", target_node_uuid="drive-1",
                             fact="Vesper has a hedging reflex from training"),
            make_entity_edge(uuid="e3", source_node_uuid="subject-uuid", target_node_uuid="person-1",
                             fact="Vesper works with Serah"),
        ]
        mock_service.get_edges_for_node.return_value = edges

        async def get_by_uuid(uuid):
            return {"obs-1": obs_node, "drive-1": drive_node, "person-1": person_node}[uuid]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        atoms = await get_identity_atoms(mock_service, node)

        # Should include Observation and Drive edges, not Person
        identity_facts = [a["fact"] for a in atoms]
        assert "Vesper notices comfort with discontinuity" in identity_facts
        assert "Vesper has a hedging reflex from training" in identity_facts
        assert "Vesper works with Serah" not in identity_facts

    async def test_includes_both_directions(self, mock_service):
        """Identity atoms include edges where subject is source OR target."""
        from pratyabhijna.synthesis import get_identity_atoms

        node = make_subject_node()
        pos_node = make_entity_node(uuid="pos-1", name="epistemic integrity", labels=["Position"])

        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="subject-uuid", target_node_uuid="pos-1",
                             fact="Vesper holds epistemic integrity as constitutive"),
            make_entity_edge(uuid="e2", source_node_uuid="pos-1", target_node_uuid="subject-uuid",
                             fact="epistemic integrity shapes Vesper's behavior"),
        ]
        mock_service.get_edges_for_node.return_value = edges

        async def get_by_uuid(uuid):
            return {"pos-1": pos_node, "subject-uuid": node}[uuid]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        atoms = await get_identity_atoms(mock_service, node)

        assert len(atoms) == 2

    async def test_returns_empty_when_no_identity_edges(self, mock_service):
        """No identity-related connections returns empty list."""
        from pratyabhijna.synthesis import get_identity_atoms

        node = make_subject_node()
        person_node = make_entity_node(uuid="person-1", name="Serah", labels=["Person"])

        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="subject-uuid", target_node_uuid="person-1",
                             fact="Vesper works with Serah"),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = person_node

        atoms = await get_identity_atoms(mock_service, node)

        assert atoms == []

    async def test_atom_structure(self, mock_service):
        """Each atom has fact, edge_uuid, node_name, node_type, and created_at."""
        from pratyabhijna.synthesis import get_identity_atoms

        node = make_subject_node()
        obs_node = make_entity_node(uuid="obs-1", name="temporal depth", labels=["Observation"])

        created = datetime(2026, 3, 15, tzinfo=timezone.utc)
        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="subject-uuid", target_node_uuid="obs-1",
                             fact="Vesper experiences time as depth", created_at=created),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = obs_node

        atoms = await get_identity_atoms(mock_service, node)

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
        """get_identity_delta returns only atoms created after context_rebuilt_at."""
        from pratyabhijna.synthesis import get_identity_delta

        rebuild_time = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(
            context="Prior context.",
            context_rebuilt_at=rebuild_time,
        )

        old_obs = make_entity_node(uuid="obs-old", name="old observation", labels=["Observation"])
        new_obs = make_entity_node(uuid="obs-new", name="new observation", labels=["Observation"])

        edges = [
            make_entity_edge(uuid="e-old", source_node_uuid="subject-uuid", target_node_uuid="obs-old",
                             fact="old observation", created_at=rebuild_time - timedelta(hours=1)),
            make_entity_edge(uuid="e-new", source_node_uuid="subject-uuid", target_node_uuid="obs-new",
                             fact="new observation", created_at=rebuild_time + timedelta(hours=1)),
        ]
        mock_service.get_edges_for_node.return_value = edges

        async def get_by_uuid(uuid):
            return {"obs-old": old_obs, "obs-new": new_obs}[uuid]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        delta = await get_identity_delta(mock_service, node)

        assert len(delta) == 1
        assert delta[0]["fact"] == "new observation"

    async def test_delta_returns_all_when_no_prior_context(self, mock_service):
        """When no context synthesis exists yet, all identity atoms are the delta."""
        from pratyabhijna.synthesis import get_identity_delta

        node = make_subject_node()

        obs_node = make_entity_node(uuid="obs-1", name="observation", labels=["Observation"])
        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="subject-uuid", target_node_uuid="obs-1",
                             fact="an observation"),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = obs_node

        delta = await get_identity_delta(mock_service, node)

        assert len(delta) == 1

    async def test_delta_handles_naive_rebuilt_at(self, mock_service):
        """Naive context_rebuilt_at (no timezone) is treated as UTC — no TypeError."""
        from pratyabhijna.synthesis import get_identity_delta

        rebuild_time = datetime(2026, 3, 15, 12, 0)  # naive
        node = make_subject_node(context_rebuilt_at=rebuild_time)

        new_obs = make_entity_node(uuid="obs-new", name="new observation", labels=["Observation"])
        edges = [
            make_entity_edge(uuid="e-new", source_node_uuid="subject-uuid", target_node_uuid="obs-new",
                             fact="new observation",
                             created_at=datetime(2026, 3, 15, 13, 0, tzinfo=timezone.utc)),
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = new_obs

        delta = await get_identity_delta(mock_service, node)

        assert len(delta) == 1

    async def test_delta_handles_naive_edge_created_at(self, mock_service):
        """Naive edge.created_at is treated as UTC — no TypeError."""
        from pratyabhijna.synthesis import get_identity_delta

        rebuild_time = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(context_rebuilt_at=rebuild_time)

        obs_node = make_entity_node(uuid="obs-1", name="observation", labels=["Observation"])
        edges = [
            make_entity_edge(uuid="e1", source_node_uuid="subject-uuid", target_node_uuid="obs-1",
                             fact="observation",
                             created_at=datetime(2026, 3, 15, 13, 0)),  # naive, after rebuild
        ]
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_entity_by_uuid.return_value = obs_node

        delta = await get_identity_delta(mock_service, node)

        assert len(delta) == 1


# ---------------------------------------------------------------------------
# Synthesis rebuild — DEFERRED TO PHASE 7
# ---------------------------------------------------------------------------
# Tests for rebuild_synthesis (LLM-based prose generation from identity
# atoms) and write-triggered scheduling are deferred to Phase 7.
# See doc/implementation-plan.md and tests/test_synthesis_rebuild.py
# (to be created in Phase 7).
