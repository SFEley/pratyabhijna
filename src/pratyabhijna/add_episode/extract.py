"""Stage 2 — extract nodes and edges from the episode."""

from __future__ import annotations

from datetime import datetime

from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.prompts.models import Message

from pratyabhijna.add_episode.prompts import (
    build_extract_system_prompt,
    build_extract_user_message,
)
from pratyabhijna.add_episode.schemas import ExtractResponse


async def extract(
    *,
    llm_client: LLMClient,
    episode_name: str,
    source: EpisodeType,
    source_description: str,
    reference_time: datetime,
    body: str,
    previous_episodes: list[EpisodicNode],
    custom_instructions: str | None = None,
) -> ExtractResponse:
    """Single tool-use call that returns the extracted nodes and edges.

    `custom_instructions`, when provided, is prepended to the user message
    as a steering hint. Used by the `correct` flow to focus extraction on
    search terms relevant to the contradicted facts.
    """
    user_content = build_extract_user_message(
        episode_name=episode_name,
        source=source,
        source_description=source_description,
        reference_time=reference_time,
        body=body,
        previous_episodes=previous_episodes,
    )
    if custom_instructions:
        user_content = f"## Additional Instructions\n\n{custom_instructions}\n\n{user_content}"

    messages = [
        Message(role="system", content=build_extract_system_prompt()),
        Message(role="user", content=user_content),
    ]
    raw = await llm_client.generate_response(
        messages,
        response_model=ExtractResponse,
        prompt_name="pratyabhijna.add_episode.extract",
    )
    return ExtractResponse(**raw) if isinstance(raw, dict) else raw
