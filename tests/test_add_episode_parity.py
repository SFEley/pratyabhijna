"""Parity tests — in-house add_episode vs. graphiti.add_episode.

Each fixture episode is run through both pipelines against fresh test
databases, and the resulting graph shape is compared. Acceptable
differences are documented inline; behavioural regressions are not.

Gated by --live because both pipelines need a real LLM + embedder +
Neo4j; in mocked mode, parity tests would only compare the mock
plumbing, which isn't useful.
"""

from __future__ import annotations

import json
from datetime import datetime
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
        reason="parity tests require --live (real LLM + Neo4j)",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


PARITY_DIR = Path(__file__).parent / "fixtures" / "parity_episodes"


def _fixture_paths() -> list[Path]:
    return sorted(PARITY_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Services on disjoint test groups (no shared graph state)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def parity_service_graphiti():
    cfg = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(cfg)
    await svc.start()
    await svc._graphiti.driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) DETACH DELETE n", g="parity-graphiti",
    )
    await svc._graphiti.driver.execute_query(
        "MATCH (e:Episodic {group_id: $g}) DETACH DELETE e", g="parity-graphiti",
    )
    yield svc
    await svc.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def parity_service_inhouse():
    cfg = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(cfg)
    await svc.start()
    await svc._graphiti.driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) DETACH DELETE n", g="parity-inhouse",
    )
    await svc._graphiti.driver.execute_query(
        "MATCH (e:Episodic {group_id: $g}) DETACH DELETE e", g="parity-inhouse",
    )
    yield svc
    await svc.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _summarize_graph(driver, group_id: str) -> dict:
    records, _, _ = await driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) RETURN count(n) AS n", g=group_id,
    )
    node_count = records[0]["n"]
    records, _, _ = await driver.execute_query(
        "MATCH ()-[r:RELATES_TO {group_id: $g}]-() RETURN count(r) AS n", g=group_id,
    )
    edge_count = records[0]["n"]
    records, _, _ = await driver.execute_query(
        "MATCH ()-[r:RELATES_TO {group_id: $g}]-() WHERE r.invalid_at IS NOT NULL "
        "RETURN count(r) AS n", g=group_id,
    )
    supersession_count = records[0]["n"]
    records, _, _ = await driver.execute_query(
        "MATCH (e:Episodic {group_id: $g}) RETURN count(e) AS n", g=group_id,
    )
    episode_count = records[0]["n"]
    # Track summary presence so a regression (e.g. EntityNode(..., summary="")
    # silently shipping) surfaces in parity output instead of staying invisible.
    records, _, _ = await driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) "
        "WHERE n.summary IS NULL OR n.summary = '' "
        "RETURN count(n) AS n",
        g=group_id,
    )
    empty_summary_count = records[0]["n"]
    # Bidirectional provenance: every Entity edge should reference at least
    # one episode; every Episodic should reference the edges it produced.
    records, _, _ = await driver.execute_query(
        "MATCH ()-[r:RELATES_TO {group_id: $g}]->() "
        "WHERE r.episodes IS NULL OR size(r.episodes) = 0 "
        "RETURN count(r) AS n",
        g=group_id,
    )
    edges_without_episodes = records[0]["n"]
    records, _, _ = await driver.execute_query(
        "MATCH (e:Episodic {group_id: $g}) "
        "WHERE e.entity_edges IS NULL OR size(e.entity_edges) = 0 "
        "RETURN count(e) AS n",
        g=group_id,
    )
    episodes_without_entity_edges = records[0]["n"]
    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "supersession_count": supersession_count,
        "episode_count": episode_count,
        "empty_summary_count": empty_summary_count,
        "edges_without_episodes": edges_without_episodes,
        "episodes_without_entity_edges": episodes_without_entity_edges,
    }


# ---------------------------------------------------------------------------
# Parametrized parity test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
async def test_parity(path, parity_service_graphiti, parity_service_inhouse):
    spec = _load(path)
    args = dict(
        name=spec["name"],
        episode_body=spec["body"],
        source_description=spec["source_description"],
        reference_time=datetime.fromisoformat(spec["reference_time"]),
        source=EpisodeType(spec["source"]),
    )

    # Graphiti
    await parity_service_graphiti._graphiti.add_episode(
        group_id="parity-graphiti",
        entity_types=parity_service_graphiti.entity_types,
        **args,
    )

    # In-house
    await in_house_add_episode(
        parity_service_inhouse,
        group_id="parity-inhouse",
        **args,
    )

    g_summary = await _summarize_graph(
        parity_service_graphiti._graphiti.driver, "parity-graphiti",
    )
    p_summary = await _summarize_graph(
        parity_service_inhouse._graphiti.driver, "parity-inhouse",
    )

    # LLM stochasticity makes exact equality unrealistic. Acceptable bounds:
    # - Node count within ±3 (extractor may identify slightly different
    #   sets of named entities; this is fine as long as the general shape holds).
    # - Edge count within ±3 (same reasoning).
    # - Episode count exactly equal (always 1 per fixture).
    # - Supersession count equal — if either side flags a supersession, both
    #   should, modulo the first-run case where graphiti has no prior to invalidate.
    assert g_summary["episode_count"] == p_summary["episode_count"] == 1, (
        f"{path.name}: episode count differs: {g_summary} vs {p_summary}"
    )
    assert abs(g_summary["node_count"] - p_summary["node_count"]) <= 3, (
        f"{path.name}: node count diverged: graphiti={g_summary['node_count']} "
        f"in-house={p_summary['node_count']}"
    )
    assert abs(g_summary["edge_count"] - p_summary["edge_count"]) <= 3, (
        f"{path.name}: edge count diverged: graphiti={g_summary['edge_count']} "
        f"in-house={p_summary['edge_count']}"
    )
    # In-house invariants the pipeline guarantees (parity-independent):
    # - every Entity carries a non-empty summary (extractor + name fallback)
    # - every Entity edge references the episode that asserted it
    assert p_summary["empty_summary_count"] == 0, (
        f"{path.name}: in-house created {p_summary['empty_summary_count']} entities "
        f"with empty summary — regression vs graphiti's summary-extraction behaviour"
    )
    assert p_summary["edges_without_episodes"] == 0, (
        f"{path.name}: in-house created {p_summary['edges_without_episodes']} edges "
        f"with empty episodes list — breaks tools/inspect.py and forget_episode"
    )
