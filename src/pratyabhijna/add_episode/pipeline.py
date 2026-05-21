"""Orchestrator for the in-house add_episode pipeline.

Sequences Stages 0 through 5 and emits INFO logging at each boundary.
Implemented incrementally across Tasks 5-16 of the plan; this file
currently carries the public dataclass and a NotImplementedError stub.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AddEpisodeResult:
    """Outcome of one add_episode invocation.

    Attributes:
        episode_uuid: UUID of the Episodic node (either freshly created
            or an existing one if Stage 0 short-circuited).
        nodes_created: Count of Entity nodes inserted in this run.
        nodes_updated: Count of existing Entity nodes whose attributes
            were merged with new values from this episode.
        edges_created: Count of EntityEdge relationships inserted.
        supersessions: Count of prior edges marked invalid by this run.
        short_circuited: True if Stage 0 found an existing Episodic node
            with the same hash and skipped all downstream stages.
    """

    episode_uuid: str
    nodes_created: int
    nodes_updated: int
    edges_created: int
    supersessions: int
    short_circuited: bool


async def add_episode(*args, **kwargs) -> AddEpisodeResult:  # pragma: no cover
    """In-house replacement for graphiti.add_episode.

    Implemented across Tasks 5-16. See the plan document for stage breakdown.
    """
    raise NotImplementedError("Pipeline body lands in Task 16")
