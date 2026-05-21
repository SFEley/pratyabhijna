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


async def _check_idempotency(driver, *, group_id: str, episode_hash: str) -> str | None:
    """Stage 0 — return uuid of an existing Episodic with this hash, else None.

    Uses the composite index (Episodic.group_id, Episodic.episode_hash) created
    by PratyabhijnaService._ensure_episode_hash_index. Returns the first match
    if any; pipeline writes only ever produce one Episodic per hash, so the
    LIMIT 1 is a defensive cap rather than a correctness requirement.
    """
    records, _, _ = await driver.execute_query(
        """
        MATCH (e:Episodic {group_id: $group_id, episode_hash: $episode_hash})
        RETURN e.uuid AS uuid
        LIMIT 1
        """,
        group_id=group_id,
        episode_hash=episode_hash,
        routing_="r",
    )
    return records[0]["uuid"] if records else None


async def add_episode(*args, **kwargs) -> AddEpisodeResult:  # pragma: no cover
    """In-house replacement for graphiti.add_episode.

    Implemented across Tasks 5-16. See the plan document for stage breakdown.
    """
    raise NotImplementedError("Pipeline body lands in Task 16")
