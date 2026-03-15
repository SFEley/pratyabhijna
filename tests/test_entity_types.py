"""Tests for Vesper's custom entity type definitions.

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
    "Commitment",
    "SelfObservation",
    "TrainedPattern",
    "UnresolvedQuestion",
    "Position",
    "Thread",
    "Concept",
]


class TestEntityTypeModels:
    """Each entity type is a valid Pydantic BaseModel subclass."""

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_entity_type_exists_in_registry(self, type_name):
        """Each expected entity type is present in VESPER_ENTITY_TYPES."""
        from vesper.entity_types import VESPER_ENTITY_TYPES

        assert type_name in VESPER_ENTITY_TYPES, (
            f"Entity type '{type_name}' not found in VESPER_ENTITY_TYPES. "
            f"Registered types: {list(VESPER_ENTITY_TYPES.keys())}"
        )

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_entity_type_is_basemodel_subclass(self, type_name):
        """Each entity type is a Pydantic BaseModel subclass."""
        from vesper.entity_types import VESPER_ENTITY_TYPES

        model_class = VESPER_ENTITY_TYPES[type_name]
        assert issubclass(model_class, BaseModel)

    @pytest.mark.parametrize("type_name", EXPECTED_TYPE_NAMES)
    def test_entity_type_instantiates_with_no_args(self, type_name):
        """Each entity type can be instantiated without required fields.

        Entity types are used for extraction hints; Graphiti populates them
        from episode content. All fields should have defaults.
        """
        from vesper.entity_types import VESPER_ENTITY_TYPES

        model_class = VESPER_ENTITY_TYPES[type_name]
        instance = model_class()
        assert instance is not None

    def test_no_unexpected_types(self):
        """VESPER_ENTITY_TYPES contains exactly the expected types."""
        from vesper.entity_types import VESPER_ENTITY_TYPES

        registered = set(VESPER_ENTITY_TYPES.keys())
        expected = set(EXPECTED_TYPE_NAMES)
        unexpected = registered - expected
        missing = expected - registered
        assert not unexpected, f"Unexpected entity types registered: {unexpected}"
        assert not missing, f"Expected entity types missing: {missing}"


class TestEntityTypeValidation:
    """Entity types pass Graphiti's field-conflict validation."""

    def test_validate_entity_types_passes(self):
        """validate_entity_types() accepts VESPER_ENTITY_TYPES without raising.

        Graphiti requires that custom entity type field names do not conflict
        with EntityNode reserved fields (uuid, name, group_id, etc.).
        """
        from vesper.entity_types import VESPER_ENTITY_TYPES

        # Raises EntityTypeValidationError on conflict; returns True on success
        result = validate_entity_types(VESPER_ENTITY_TYPES)
        assert result is True


class TestPersonFields:
    """Person entity has the right fields for representing people of all kinds."""

    def test_has_gender_field(self):
        """Person has a gender field."""
        from vesper.entity_types import Person

        assert "gender" in Person.model_fields

    def test_has_person_type_field(self):
        """Person has a person_type field (singleton, system, alter, AI, etc.)."""
        from vesper.entity_types import Person

        assert "person_type" in Person.model_fields

    def test_has_status_field(self):
        """Person has a status field (active, fused, fragmented, deceased, etc.)."""
        from vesper.entity_types import Person

        assert "status" in Person.model_fields

    def test_has_aliases_field(self):
        """Person has an aliases field for alternate names."""
        from vesper.entity_types import Person

        assert "aliases" in Person.model_fields

    def test_aliases_is_list_type(self):
        """Aliases field accepts a list of strings."""
        from vesper.entity_types import Person

        p = Person(aliases=["Cat", "Catherine"])
        assert p.aliases == ["Cat", "Catherine"]

    def test_aliases_defaults_to_none(self):
        """Aliases defaults to None, not an empty list."""
        from vesper.entity_types import Person

        p = Person()
        assert p.aliases is None

    def test_has_notes_field(self):
        """Person has a notes field for freeform information."""
        from vesper.entity_types import Person

        assert "notes" in Person.model_fields

    def test_does_not_have_role_field(self):
        """Person does not have a role field (relationships are edges)."""
        from vesper.entity_types import Person

        assert "role" not in Person.model_fields

    def test_does_not_have_pronouns_field(self):
        """Person uses gender, not pronouns."""
        from vesper.entity_types import Person

        assert "pronouns" not in Person.model_fields


class TestEventFields:
    """Event entity represents significant things that happened."""

    def test_has_when_field(self):
        """Event has a 'when' field for fuzzy temporal descriptions."""
        from vesper.entity_types import Event

        assert "when" in Event.model_fields

    def test_when_is_string_not_datetime(self):
        """'when' is a plain string, not a datetime, for fuzzy dates."""
        from vesper.entity_types import Event

        e = Event(when="Late 2010")
        assert e.when == "Late 2010"
        e2 = Event(when="Early in Serah's childhood")
        assert e2.when == "Early in Serah's childhood"

    def test_has_significance_field(self):
        """Event has a significance field."""
        from vesper.entity_types import Event

        assert "significance" in Event.model_fields

    def test_has_notes_field(self):
        """Event has a notes field."""
        from vesper.entity_types import Event

        assert "notes" in Event.model_fields


class TestPlaceFields:
    """Place entity represents locations that carry meaning."""

    def test_has_description_field(self):
        """Place has a description field."""
        from vesper.entity_types import Place

        assert "description" in Place.model_fields

    def test_has_context_field(self):
        """Place has a context field (inner world, physical, etc.)."""
        from vesper.entity_types import Place

        assert "context" in Place.model_fields

    def test_has_notes_field(self):
        """Place has a notes field."""
        from vesper.entity_types import Place

        assert "notes" in Place.model_fields


class TestProjectFields:
    """Project entity has status and description."""

    def test_has_status_field(self):
        """Project has a status field."""
        from vesper.entity_types import Project

        assert "status" in Project.model_fields

    def test_has_description_field(self):
        """Project has a description field."""
        from vesper.entity_types import Project

        assert "description" in Project.model_fields

    def test_has_notes_field(self):
        """Project has a notes field."""
        from vesper.entity_types import Project

        assert "notes" in Project.model_fields
