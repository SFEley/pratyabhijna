"""Tests for Vesper's custom entity type definitions.

Verifies that all entity types are valid Pydantic models accepted by
Graphiti, and that IdentitySynthesis has staleness tracking fields.
"""

import pytest
from pydantic import BaseModel
from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types


EXPECTED_TYPE_NAMES = [
    "Person",
    "Commitment",
    "SelfObservation",
    "TrainedPattern",
    "UnresolvedQuestion",
    "Position",
    "Thread",
    "Project",
    "Concept",
    "IdentitySynthesis",
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


class TestIdentitySynthesis:
    """IdentitySynthesis has staleness tracking fields."""

    def test_has_last_rebuilt_field(self):
        """IdentitySynthesis has a last_rebuilt timestamp field."""
        from vesper.entity_types import IdentitySynthesis

        assert "last_rebuilt" in IdentitySynthesis.model_fields

    def test_has_change_count_since_field(self):
        """IdentitySynthesis has a change_count_since integer field."""
        from vesper.entity_types import IdentitySynthesis

        assert "change_count_since" in IdentitySynthesis.model_fields

    def test_last_rebuilt_defaults_to_none(self):
        """last_rebuilt is None on a freshly created synthesis (no rebuild yet)."""
        from vesper.entity_types import IdentitySynthesis

        s = IdentitySynthesis()
        assert s.last_rebuilt is None

    def test_change_count_defaults_to_zero(self):
        """change_count_since starts at 0."""
        from vesper.entity_types import IdentitySynthesis

        s = IdentitySynthesis()
        assert s.change_count_since == 0

    def test_has_prose_synthesis_field(self):
        """IdentitySynthesis stores the actual prose reconstruction."""
        from vesper.entity_types import IdentitySynthesis

        assert "prose" in IdentitySynthesis.model_fields
