"""Tests for Stage 2 extraction (prompt builders + LLM call)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphiti_core.nodes import EpisodeType, EpisodicNode

from pratyabhijna.add_episode.extract import extract
from pratyabhijna.add_episode.prompts import (
    build_extract_system_prompt,
    build_extract_user_message,
)
from pratyabhijna.add_episode.schemas import ExtractResponse
from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def test_system_prompt_includes_all_entity_type_names():
    system = build_extract_system_prompt()
    for name in PRATYABHIJNA_ENTITY_TYPES:
        assert name in system, f"system prompt missing entity type {name}"


def test_system_prompt_includes_entity_type_docstrings():
    """Each type's docstring (the extractor's actual schema hint) is embedded."""
    system = build_extract_system_prompt()
    for name, cls in PRATYABHIJNA_ENTITY_TYPES.items():
        doc = (cls.__doc__ or "").strip()
        if not doc:
            continue
        first_sentence = doc.splitlines()[0].strip()
        assert first_sentence in system, (
            f"system prompt missing first-line docstring for {name}"
        )


def test_system_prompt_lists_attribute_fields_per_type():
    """The prompt guides per-type attribute population (else the LLM leaves
    `attributes` empty and in-house nodes lose domain/status/when/etc.)."""
    system = build_extract_system_prompt()
    assert "Attribute Fields" in system
    # Each type's actual model fields should appear in the guidance.
    for name, cls in PRATYABHIJNA_ENTITY_TYPES.items():
        for field in cls.model_fields:
            assert field in system, (
                f"attribute field {field!r} for {name} missing from extract prompt"
            )


def test_user_message_includes_episode_body_and_metadata():
    msg = build_extract_user_message(
        episode_name="obs:2026-05-20",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="The episode body content",
        previous_episodes=[],
    )
    assert "The episode body content" in msg
    assert "2026-05-20" in msg
    assert "obs:2026-05-20" in msg


def test_user_message_source_hint_varies_by_type():
    base = dict(
        episode_name="x",
        source_description="d",
        reference_time=datetime.now(timezone.utc),
        body="b",
        previous_episodes=[],
    )
    a = build_extract_user_message(source=EpisodeType.text, **base)
    b = build_extract_user_message(source=EpisodeType.message, **base)
    c = build_extract_user_message(source=EpisodeType.json, **base)
    assert a != b and b != c and a != c


def test_user_message_includes_previous_episodes_block():
    prev = [
        MagicMock(
            spec=EpisodicNode,
            valid_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
            content="Yesterday's episode content.",
        ),
    ]
    msg = build_extract_user_message(
        episode_name="x",
        source=EpisodeType.message,
        source_description="d",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="Today",
        previous_episodes=prev,
    )
    assert "Yesterday's episode content." in msg
    assert "Previous Episodes" in msg


def test_user_message_omits_previous_episodes_block_when_empty():
    msg = build_extract_user_message(
        episode_name="x",
        source=EpisodeType.message,
        source_description="d",
        reference_time=datetime.now(timezone.utc),
        body="Today",
        previous_episodes=[],
    )
    assert "Previous Episodes" not in msg


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_returns_validated_response():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={
        "nodes": [
            {"idx": 0, "name": "Serah", "type": "Person", "attributes": {}},
        ],
        "edges": [],
    })
    result = await extract(
        llm_client=llm,
        episode_name="x",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime.now(timezone.utc),
        body="Serah wrote this.",
        previous_episodes=[],
    )
    assert isinstance(result, ExtractResponse)
    assert result.nodes[0].name == "Serah"


@pytest.mark.asyncio
async def test_extract_raises_on_malformed_response():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={
        "nodes": [{"idx": 0, "name": "x", "type": "Bogus", "attributes": {}}],
        "edges": [],
    })
    with pytest.raises(Exception):  # pydantic ValidationError
        await extract(
            llm_client=llm,
            episode_name="x",
            source=EpisodeType.message,
            source_description="self",
            reference_time=datetime.now(timezone.utc),
            body="x",
            previous_episodes=[],
        )


@pytest.mark.asyncio
async def test_extract_passes_response_model_for_validation():
    """The generate_response call should request the ExtractResponse schema."""
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={"nodes": [], "edges": []})
    await extract(
        llm_client=llm,
        episode_name="x",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime.now(timezone.utc),
        body="x",
        previous_episodes=[],
    )
    kwargs = llm.generate_response.await_args.kwargs
    assert kwargs.get("response_model") is ExtractResponse
