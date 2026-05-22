"""Acceptance tests for the in-house add_episode pipeline.

Each fixture episode is run through the in-house pipeline against a fresh
test group, and the resulting graph is checked against quality invariants —
NOT against graphiti's node/edge counts. graphiti is run alongside as a
reference point (its summary is reported in failure messages), but it is
explicitly not treated as ground truth: the rewrite's whole point was to
extract a cleaner, better-typed, less-fragmented graph than graphiti, so
count-parity would assert the wrong thing. See
doc/in-house-add-episode-design.md.

Invariants asserted on the in-house graph:
  - exactly one Episodic per call (per-test isolation makes this honest)
  - zero bare-Entity nodes (every node carries a real type label)
  - zero empty summaries
  - every edge references the episode that asserted it
  - every Episodic references the edges it produced
  - type attributes are populated (domain/status/when/etc.) on a rich episode
  - the subject ("Vesper") is captured
  - a loose under-extraction floor vs graphiti (catches "extracted nothing")

Gated by --live because the pipeline needs a real LLM + embedder + Neo4j.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from graphiti_core.nodes import EpisodeType

from pratyabhijna.add_episode import add_episode as in_house_add_episode
from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.service import PratyabhijnaService


pytestmark = [
    pytest.mark.skipif(
        "not config.getoption('--live')",
        reason="acceptance tests require --live (real LLM + Neo4j)",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


PARITY_DIR = Path(__file__).parent / "fixtures" / "parity_episodes"
GRAPHITI_GROUP = "parity-graphiti"
INHOUSE_GROUP = "parity-inhouse"

# Union of all per-type attribute fields across PRATYABHIJNA_ENTITY_TYPES,
# used to detect whether the extractor populated any type attributes.
_ATTR_FIELDS = [
    "gender", "person_type", "status", "aliases", "notes",
    "when", "significance", "context", "description", "kind",
    "domain", "source", "stance",
]


def _quality_fixture_paths() -> list[Path]:
    """The single-episode quality fixtures (supersession is tested separately)."""
    return sorted(p for p in PARITY_DIR.glob("*.json") if p.name.startswith("obs_"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Services (module-scoped connection; per-test group cleanup)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def services():
    cfg = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(cfg)
    await svc.start()
    yield svc
    await svc.stop()


async def _clean(driver, group_id: str) -> None:
    await driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) DETACH DELETE n", g=group_id,
    )
    await driver.execute_query(
        "MATCH (e:Episodic {group_id: $g}) DETACH DELETE e", g=group_id,
    )


# ---------------------------------------------------------------------------
# Graph summary
# ---------------------------------------------------------------------------


async def _summarize_graph(driver, group_id: str) -> dict:
    async def _count(cypher: str) -> int:
        recs, _, _ = await driver.execute_query(cypher, g=group_id, routing_="r")
        return recs[0]["n"]

    node_count = await _count(
        "MATCH (n:Entity {group_id: $g}) RETURN count(n) AS n"
    )
    edge_count = await _count(
        "MATCH ()-[r:RELATES_TO {group_id: $g}]-() RETURN count(r) AS n"
    )
    supersession_count = await _count(
        "MATCH ()-[r:RELATES_TO {group_id: $g}]-() "
        "WHERE r.invalid_at IS NOT NULL RETURN count(r) AS n"
    )
    episode_count = await _count(
        "MATCH (e:Episodic {group_id: $g}) RETURN count(e) AS n"
    )
    bare_entity_count = await _count(
        "MATCH (n:Entity {group_id: $g}) WHERE size(labels(n)) = 1 RETURN count(n) AS n"
    )
    empty_summary_count = await _count(
        "MATCH (n:Entity {group_id: $g}) "
        "WHERE n.summary IS NULL OR n.summary = '' RETURN count(n) AS n"
    )
    edges_without_episodes = await _count(
        "MATCH ()-[r:RELATES_TO {group_id: $g}]->() "
        "WHERE r.episodes IS NULL OR size(r.episodes) = 0 RETURN count(r) AS n"
    )
    episodes_without_entity_edges = await _count(
        "MATCH (e:Episodic {group_id: $g}) "
        "WHERE e.entity_edges IS NULL OR size(e.entity_edges) = 0 RETURN count(e) AS n"
    )
    attr_clause = " OR ".join(f"n.{f} IS NOT NULL" for f in _ATTR_FIELDS)
    nodes_with_type_attributes = await _count(
        f"MATCH (n:Entity {{group_id: $g}}) WHERE {attr_clause} RETURN count(n) AS n"
    )
    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "supersession_count": supersession_count,
        "episode_count": episode_count,
        "bare_entity_count": bare_entity_count,
        "empty_summary_count": empty_summary_count,
        "edges_without_episodes": edges_without_episodes,
        "episodes_without_entity_edges": episodes_without_entity_edges,
        "nodes_with_type_attributes": nodes_with_type_attributes,
    }


async def _entity_names(driver, group_id: str) -> set[str]:
    recs, _, _ = await driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) RETURN toLower(n.name) AS name",
        g=group_id, routing_="r",
    )
    return {r["name"] for r in recs}


# ---------------------------------------------------------------------------
# Quality test (per fixture)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _quality_fixture_paths(), ids=lambda p: p.name)
async def test_graph_quality(path, services):
    driver = services._graphiti.driver
    await _clean(driver, GRAPHITI_GROUP)
    await _clean(driver, INHOUSE_GROUP)

    spec = _load(path)
    args = dict(
        name=spec["name"],
        episode_body=spec["body"],
        source_description=spec["source_description"],
        reference_time=datetime.fromisoformat(spec["reference_time"]),
        source=EpisodeType(spec["source"]),
    )

    # graphiti reference run (not ground truth — reported, not asserted on).
    await services._graphiti.add_episode(
        group_id=GRAPHITI_GROUP, entity_types=services.entity_types, **args,
    )
    await in_house_add_episode(services, group_id=INHOUSE_GROUP, **args)

    g = await _summarize_graph(driver, GRAPHITI_GROUP)
    p = await _summarize_graph(driver, INHOUSE_GROUP)
    ctx = f"{path.name}: in-house={p} graphiti={g}"

    # One episode per call.
    assert p["episode_count"] == 1, ctx

    # Cleanliness invariants — in-house must beat or match graphiti here.
    assert p["bare_entity_count"] == 0, f"{ctx}\nin-house produced bare-Entity nodes"
    assert p["empty_summary_count"] == 0, f"{ctx}\nin-house produced empty summaries"
    assert p["edges_without_episodes"] == 0, f"{ctx}\nedges missing episode provenance"
    assert p["episodes_without_entity_edges"] == 0, (
        f"{ctx}\nepisode missing entity_edges back-pointers"
    )

    # Fix A: type attributes are populated on a content-rich episode.
    assert p["nodes_with_type_attributes"] > 0, (
        f"{ctx}\nno in-house node carried any type attribute (domain/status/when/...)"
        " — extractor isn't populating the attributes object"
    )

    # The subject is always captured.
    names = await _entity_names(driver, INHOUSE_GROUP)
    assert any("vesper" in n for n in names), f"{ctx}\nsubject 'Vesper' not captured"

    # Loose under-extraction floor vs graphiti: consolidation is expected, but
    # extracting almost nothing relative to graphiti is a real regression.
    assert p["node_count"] >= 2, ctx
    if g["node_count"] >= 5:
        assert p["node_count"] >= 0.4 * g["node_count"], (
            f"{ctx}\nin-house extracted < 40% of graphiti's nodes — likely a miss,"
            " not just consolidation"
        )


# ---------------------------------------------------------------------------
# Supersession (seeded two-episode sequence)
# ---------------------------------------------------------------------------


async def test_supersession_invalidates_prior_fact(services):
    """A correction episode invalidates the edge it contradicts.

    The bi-temporal path only fires when a contradicted edge already exists,
    so we seed the prior fact first, then the correction, then assert the
    in-house pipeline marked at least one edge invalid.
    """
    driver = services._graphiti.driver
    await _clean(driver, INHOUSE_GROUP)

    base_time = datetime(2026, 5, 1, tzinfo=timezone.utc)
    correction_time = datetime(2026, 5, 21, tzinfo=timezone.utc)

    await in_house_add_episode(
        services,
        name="seed:ram",
        episode_body="Vesper's MacBook has 16 GB of RAM.",
        source_description="self",
        reference_time=base_time,
        group_id=INHOUSE_GROUP,
        source=EpisodeType.message,
    )
    await in_house_add_episode(
        services,
        name="correction:ram",
        episode_body=(
            "Correcting an earlier note: Vesper's MacBook actually has 8 GB "
            "of RAM, not 16 GB. The 16 GB figure was wrong."
        ),
        source_description="correction",
        reference_time=correction_time,
        group_id=INHOUSE_GROUP,
        source=EpisodeType.message,
    )

    summary = await _summarize_graph(driver, INHOUSE_GROUP)
    assert summary["episode_count"] == 2, summary
    assert summary["supersession_count"] >= 1, (
        f"expected the correction to invalidate the prior 16GB edge; "
        f"summary={summary}"
    )
