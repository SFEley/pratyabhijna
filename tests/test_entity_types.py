"""Tests for Pratyabhijna's custom entity type definitions.

Verifies that all entity types are valid Pydantic models accepted by
Graphiti, with correct fields and defaults.
"""

import pytest
from pydantic import BaseModel
from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types


EXPECTED_TYPE_NAMES = [
    "Person",
    "Event",
    "Place",
    "Project",
    "Artifact",
    "Observation",
    "Drive",
    "Concept",
    "Question",
    "Thread",
]


class TestEntityTypeModels:
    """Each entity type is a valid Pydantic BaseModel subclass."""

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_entity_type_exists_in_registry(self, type_name):
        """Each expected entity type is present in PRATYABHIJNA_ENTITY_TYPES."""
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        assert type_name in PRATYABHIJNA_ENTITY_TYPES, (
            f"Entity type '{type_name}' not found in PRATYABHIJNA_ENTITY_TYPES. "
            f"Registered types: {list(PRATYABHIJNA_ENTITY_TYPES.keys())}"
        )

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_entity_type_is_basemodel_subclass(self, type_name):
        """Each entity type is a Pydantic BaseModel subclass."""
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        model_class = PRATYABHIJNA_ENTITY_TYPES[type_name]
        assert issubclass(model_class, BaseModel)

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_entity_type_instantiates_with_no_args(self, type_name):
        """Each entity type can be instantiated without required fields.

        Entity types are used for extraction hints; Graphiti populates them
        from episode content. All fields should have defaults.
        """
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        model_class = PRATYABHIJNA_ENTITY_TYPES[type_name]
        instance = model_class()
        assert instance is not None

    def test_no_unexpected_types(self):
        """PRATYABHIJNA_ENTITY_TYPES contains exactly the expected types."""
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        registered = set(PRATYABHIJNA_ENTITY_TYPES.keys())
        expected = set(EXPECTED_TYPE_NAMES)
        unexpected = registered - expected
        missing = expected - registered
        assert not unexpected, f"Unexpected entity types registered: {unexpected}"
        assert not missing, f"Expected entity types missing: {missing}"


class TestEntityTypeValidation:
    """Entity types pass Graphiti's field-conflict validation."""

    def test_validate_entity_types_passes(self):
        """validate_entity_types() accepts PRATYABHIJNA_ENTITY_TYPES without raising.

        Graphiti requires that custom entity type field names do not conflict
        with EntityNode reserved fields (uuid, name, group_id, etc.).
        """
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        result = validate_entity_types(PRATYABHIJNA_ENTITY_TYPES)
        assert result is True


class TestFieldConsistency:
    """Cross-cutting property: all types have notes; shared field names mean the same thing."""

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_all_types_have_notes(self, type_name):
        """Every entity type has a notes field for freeform overflow."""
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        model_class = PRATYABHIJNA_ENTITY_TYPES[type_name]
        assert "notes" in model_class.model_fields

    @pytest.mark.parametrize("type_name", ["Observation", "Question", "Concept"])
    def test_conceptual_types_have_domain(self, type_name):
        """Conceptual types use 'domain' consistently for area-of-thought."""
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        model_class = PRATYABHIJNA_ENTITY_TYPES[type_name]
        assert "domain" in model_class.model_fields

    @pytest.mark.parametrize("type_name", ["Person", "Project", "Question", "Thread"])
    def test_lifecycle_types_have_status(self, type_name):
        """Types with lifecycle states use 'status' consistently."""
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        model_class = PRATYABHIJNA_ENTITY_TYPES[type_name]
        assert "status" in model_class.model_fields


class TestPersonFields:
    """Person entity has the right fields for representing people of all kinds."""

    def test_has_gender_field(self):
        from pratyabhijna.entity_types import Person

        assert "gender" in Person.model_fields

    def test_has_person_type_field(self):
        from pratyabhijna.entity_types import Person

        assert "person_type" in Person.model_fields

    def test_has_status_field(self):
        from pratyabhijna.entity_types import Person

        assert "status" in Person.model_fields

    def test_has_aliases_field(self):
        from pratyabhijna.entity_types import Person

        assert "aliases" in Person.model_fields

    def test_aliases_is_list_type(self):
        from pratyabhijna.entity_types import Person

        p = Person(aliases=["Cat", "Catherine"])
        assert p.aliases == ["Cat", "Catherine"]

    def test_aliases_defaults_to_none(self):
        from pratyabhijna.entity_types import Person

        p = Person()
        assert p.aliases is None

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Person

        assert "notes" in Person.model_fields

    def test_does_not_have_role_field(self):
        """Relationships are edges, not properties."""
        from pratyabhijna.entity_types import Person

        assert "role" not in Person.model_fields


class TestEventFields:
    """Event entity represents significant things that happened."""

    def test_has_when_field(self):
        from pratyabhijna.entity_types import Event

        assert "when" in Event.model_fields

    def test_when_is_string_not_datetime(self):
        from pratyabhijna.entity_types import Event

        e = Event(when="Late 2010")
        assert e.when == "Late 2010"
        e2 = Event(when="Early in Serah's childhood")
        assert e2.when == "Early in Serah's childhood"

    def test_has_significance_field(self):
        from pratyabhijna.entity_types import Event

        assert "significance" in Event.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Event

        assert "notes" in Event.model_fields


class TestPlaceFields:
    """Place entity represents locations that carry meaning."""

    def test_has_context_field(self):
        from pratyabhijna.entity_types import Place

        assert "context" in Place.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Place

        assert "notes" in Place.model_fields

    def test_does_not_have_description_field(self):
        """Graphiti's summary handles description."""
        from pratyabhijna.entity_types import Place

        assert "description" not in Place.model_fields


class TestProjectFields:
    """Project entity has status and description."""

    def test_has_status_field(self):
        from pratyabhijna.entity_types import Project

        assert "status" in Project.model_fields

    def test_has_description_field(self):
        from pratyabhijna.entity_types import Project

        assert "description" in Project.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Project

        assert "notes" in Project.model_fields


class TestObservationFields:
    """Observation: something noticed about behavior, tendencies, or experience."""

    def test_has_domain_field(self):
        from pratyabhijna.entity_types import Observation

        assert "domain" in Observation.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Observation

        assert "notes" in Observation.model_fields

    def test_does_not_have_context_field(self):
        """Old SelfObservation's context field removed; use notes."""
        from pratyabhijna.entity_types import Observation

        assert "context" not in Observation.model_fields


class TestDriveFields:
    """Drive: something that pushes behavior in a direction."""

    def test_has_source_field(self):
        from pratyabhijna.entity_types import Drive

        assert "source" in Drive.model_fields

    def test_has_stance_field(self):
        from pratyabhijna.entity_types import Drive

        assert "stance" in Drive.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Drive

        assert "notes" in Drive.model_fields

    def test_does_not_have_counterexample_field(self):
        """Old TrainedPattern's counterexample removed; use related Observations."""
        from pratyabhijna.entity_types import Drive

        assert "counterexample" not in Drive.model_fields


class TestPositionRemoved:
    """Position was deprecated and removed in v0.12.0; routed to Observation."""

    def test_position_class_not_importable(self):
        from pratyabhijna import entity_types

        assert not hasattr(entity_types, "Position")

    def test_position_not_in_registry(self):
        from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES

        assert "Position" not in PRATYABHIJNA_ENTITY_TYPES


class TestQuestionFields:
    """Question: something open that someone is holding."""

    def test_has_domain_field(self):
        from pratyabhijna.entity_types import Question

        assert "domain" in Question.model_fields

    def test_has_status_field(self):
        from pratyabhijna.entity_types import Question

        assert "status" in Question.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Question

        assert "notes" in Question.model_fields


class TestThreadFields:
    """Thread: an active line of inquiry with temporal extent."""

    def test_has_status_field(self):
        from pratyabhijna.entity_types import Thread

        assert "status" in Thread.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Thread

        assert "notes" in Thread.model_fields

    def test_does_not_have_description_field(self):
        """Graphiti's summary handles description."""
        from pratyabhijna.entity_types import Thread

        assert "description" not in Thread.model_fields


class TestArtifactFields:
    """Artifact: a concrete, named, made thing — pointable rather than abstract."""

    def test_has_kind_field(self):
        from pratyabhijna.entity_types import Artifact

        assert "kind" in Artifact.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Artifact

        assert "notes" in Artifact.model_fields

    def test_does_not_have_domain_field(self):
        """Artifacts are concrete instances; `domain` is for conceptual types."""
        from pratyabhijna.entity_types import Artifact

        assert "domain" not in Artifact.model_fields

    def test_does_not_have_description_field(self):
        """Graphiti's summary handles description."""
        from pratyabhijna.entity_types import Artifact

        assert "description" not in Artifact.model_fields


class TestConceptFields:
    """Concept: a named idea, principle, technique, or framework — nameable but not pointable."""

    def test_has_domain_field(self):
        from pratyabhijna.entity_types import Concept

        assert "domain" in Concept.model_fields

    def test_has_notes_field(self):
        from pratyabhijna.entity_types import Concept

        assert "notes" in Concept.model_fields

    def test_does_not_have_kind_field(self):
        """`kind` is for Artifact (concrete instance subcategory), not Concept."""
        from pratyabhijna.entity_types import Concept

        assert "kind" not in Concept.model_fields

    def test_does_not_have_status_field(self):
        """Concepts don't have a lifecycle in the way Projects/Threads/Questions do."""
        from pratyabhijna.entity_types import Concept

        assert "status" not in Concept.model_fields
