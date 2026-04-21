"""Tests for pratyabhijna.communities — pure-function unit tests.

All tests here cover label_propagation and _select_representative_members,
both of which are synchronous and require no mocking.
"""

import time

import pytest
from graphiti_core.utils.maintenance.community_operations import Neighbor

from pratyabhijna.communities import _select_representative_members, label_propagation
from helpers import make_entity_node


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_projection(*edges: tuple[str, str, int]) -> dict[str, list[Neighbor]]:
    """Build a symmetric projection from (src, tgt, edge_count) triples."""
    projection: dict[str, list[Neighbor]] = {}
    for src, tgt, count in edges:
        projection.setdefault(src, []).append(Neighbor(node_uuid=tgt, edge_count=count))
        projection.setdefault(tgt, []).append(Neighbor(node_uuid=src, edge_count=count))
    return projection


def _assert_partition(clusters: list[list[str]], all_nodes: set[str]) -> None:
    """Every node appears exactly once; no cluster is empty."""
    found = {n for cluster in clusters for n in cluster}
    assert found == all_nodes, f"partition missing nodes: {all_nodes - found}, extra: {found - all_nodes}"
    for cluster in clusters:
        assert len(cluster) > 0, "empty cluster in partition"


# ---------------------------------------------------------------------------
# label_propagation
# ---------------------------------------------------------------------------


class TestLabelPropagation:
    def test_empty_projection_returns_empty(self):
        assert label_propagation({}) == []

    def test_single_isolated_node(self):
        projection = {"a": []}
        clusters = label_propagation(projection)
        _assert_partition(clusters, {"a"})
        assert len(clusters) == 1

    def test_two_disconnected_triangles(self):
        projection = _make_projection(
            ("a", "b", 1), ("b", "c", 1), ("a", "c", 1),
            ("x", "y", 1), ("y", "z", 1), ("x", "z", 1),
        )
        clusters = label_propagation(projection)
        _assert_partition(clusters, {"a", "b", "c", "x", "y", "z"})
        assert len(clusters) == 2

    def test_complete_graph_collapses_to_one_community(self):
        nodes = [f"n{i}" for i in range(8)]
        edges = [(u, v, 1) for i, u in enumerate(nodes) for v in nodes[i + 1:]]
        projection = _make_projection(*edges)
        clusters = label_propagation(projection)
        _assert_partition(clusters, set(nodes))
        assert len(clusters) == 1

    def test_hub_with_leaves_converges(self):
        """Regression: central hub connected to many leaves must not oscillate."""
        edges = [(f"leaf{i}", "hub", 1) for i in range(20)]
        projection = _make_projection(*edges)
        # seed isolated hub entry so it appears even with no outbound-only nodes
        projection.setdefault("hub", [])
        start = time.monotonic()
        clusters = label_propagation(projection)
        elapsed = time.monotonic() - start
        all_nodes = {"hub"} | {f"leaf{i}" for i in range(20)}
        _assert_partition(clusters, all_nodes)
        assert elapsed < 1.0, f"hub graph should converge in <1 s; took {elapsed:.2f}s"

    def test_two_stars_joined_by_bridge(self):
        """Two hub+leaf clusters connected by one bridge should form two communities."""
        edges = [
            *[(f"a{i}", "hub_a", 3) for i in range(8)],
            *[(f"b{i}", "hub_b", 3) for i in range(8)],
            ("hub_a", "hub_b", 1),
        ]
        projection = _make_projection(*edges)
        clusters = label_propagation(projection)
        all_nodes = {"hub_a", "hub_b"} | {f"a{i}" for i in range(8)} | {f"b{i}" for i in range(8)}
        _assert_partition(clusters, all_nodes)
        assert len(clusters) == 2

    def test_real_world_pathological_graph_converges(self):
        """Minimised reproduction of the 48-node production failure.

        Hub connected to 14 peers caused infinite oscillation in the synchronous
        batch-update LPA. Async LPA must terminate in under 1 second.
        """
        hub = "hub"
        heavy = [f"heavy{i}" for i in range(4)]
        light = [f"light{i}" for i in range(10)]
        dyads = [(f"dyad{i}a", f"dyad{i}b") for i in range(2)]

        edges: list[tuple[str, str, int]] = []
        for h in heavy:
            edges.append((hub, h, 5))
            for h2 in heavy:
                if h < h2:
                    edges.append((h, h2, 2))
        for l in light:
            edges.append((hub, l, 1))
        for da, db in dyads:
            edges.append((da, db, 3))

        projection = _make_projection(*edges)
        for da, db in dyads:
            projection.setdefault(da, [])
            projection.setdefault(db, [])

        start = time.monotonic()
        clusters = label_propagation(projection)
        elapsed = time.monotonic() - start

        all_nodes = {hub} | set(heavy) | set(light) | {n for pair in dyads for n in pair}
        _assert_partition(clusters, all_nodes)
        assert elapsed < 1.0, f"pathological graph should converge in <1 s; took {elapsed:.2f}s"

    def test_deterministic_under_seed(self):
        """Two calls with the same RNG seed produce identical partitions."""
        edges = [
            ("a", "b", 2), ("b", "c", 2), ("c", "a", 2),
            ("d", "e", 2), ("e", "f", 2), ("f", "d", 2),
            ("c", "d", 1),
        ]
        projection = _make_projection(*edges)
        first = [sorted(c) for c in label_propagation(projection)]
        second = [sorted(c) for c in label_propagation(projection)]
        assert sorted(first) == sorted(second)

    @pytest.mark.parametrize("size", [50, 200])
    def test_ring_graph_of_varying_sizes(self, size):
        """Ring graphs converge regardless of size."""
        nodes = [f"n{i}" for i in range(size)]
        edges = [(nodes[i], nodes[(i + 1) % size], 1) for i in range(size)]
        projection = _make_projection(*edges)
        start = time.monotonic()
        clusters = label_propagation(projection)
        elapsed = time.monotonic() - start
        _assert_partition(clusters, set(nodes))
        assert elapsed < 2.0, f"ring-{size} took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# _select_representative_members
# ---------------------------------------------------------------------------


class TestSelectRepresentativeMembers:
    def test_returns_all_members_when_cluster_smaller_than_k(self):
        members = [make_entity_node(uuid=f"e{i}", name=f"Entity{i}") for i in range(3)]
        result = _select_representative_members(members, projection=None, sample_size=10)
        assert len(result) == 3

    def test_returns_all_members_when_cluster_equal_to_k(self):
        members = [make_entity_node(uuid=f"e{i}", name=f"Entity{i}") for i in range(5)]
        result = _select_representative_members(members, projection=None, sample_size=5)
        assert len(result) == 5

    def test_prefers_higher_in_community_degree(self):
        members = [make_entity_node(uuid=f"e{i}", name=f"Entity{i}") for i in range(5)]
        # e0 is the hub with 3 in-community edges; e4 is isolated
        projection: dict[str, list[Neighbor]] = {
            "e0": [
                Neighbor(node_uuid="e1", edge_count=5),
                Neighbor(node_uuid="e2", edge_count=5),
                Neighbor(node_uuid="e3", edge_count=5),
            ],
            "e1": [Neighbor(node_uuid="e0", edge_count=5)],
            "e2": [Neighbor(node_uuid="e0", edge_count=5)],
            "e3": [Neighbor(node_uuid="e0", edge_count=5)],
            "e4": [],
        }
        result = _select_representative_members(members, projection=projection, sample_size=2)
        assert len(result) == 2
        assert result[0].uuid == "e0"

    def test_falls_back_to_summary_length_without_projection(self):
        members = [
            make_entity_node(uuid="short", name="Short", summary="x" * 10),
            make_entity_node(uuid="long", name="Long", summary="x" * 200),
        ]
        result = _select_representative_members(members, projection=None, sample_size=1)
        assert result[0].uuid == "long"

    def test_falls_back_to_summary_length_with_empty_projection(self):
        members = [
            make_entity_node(uuid="short", name="Short", summary="x" * 10),
            make_entity_node(uuid="long", name="Long", summary="x" * 200),
        ]
        projection: dict[str, list[Neighbor]] = {"short": [], "long": []}
        result = _select_representative_members(members, projection=projection, sample_size=1)
        assert result[0].uuid == "long"

    def test_deterministic_on_ties(self):
        """Identical degree and summary length: result must be stable across calls."""
        members = [
            make_entity_node(uuid=f"e{i}", name=f"Entity{i}", summary="x" * 50)
            for i in range(6)
        ]
        projection: dict[str, list[Neighbor]] = {m.uuid: [] for m in members}
        first = _select_representative_members(members, projection=projection, sample_size=3)
        second = _select_representative_members(members, projection=projection, sample_size=3)
        assert [m.uuid for m in first] == [m.uuid for m in second]

    def test_only_counts_in_community_edges(self):
        """Edges to nodes outside the cluster must not contribute to degree."""
        members = [
            make_entity_node(uuid="insider", name="Insider"),
            make_entity_node(uuid="insider2", name="Insider2"),
        ]
        projection: dict[str, list[Neighbor]] = {
            "insider": [
                Neighbor(node_uuid="outsider_a", edge_count=100),
                Neighbor(node_uuid="outsider_b", edge_count=100),
                Neighbor(node_uuid="insider2", edge_count=1),
            ],
            "insider2": [Neighbor(node_uuid="insider", edge_count=1)],
        }
        result = _select_representative_members(members, projection=projection, sample_size=1)
        # insider has 100+100 edges outside, only 1 inside; insider2 has 1 inside
        # both have 1 in-community degree; summary length equal; name "Insider2" > "Insider"
        assert result[0].uuid == "insider2"

    def test_summary_length_breaks_degree_ties(self):
        """When in-community degree is equal, richer summaries win."""
        members = [
            make_entity_node(uuid="a", name="A", summary="x" * 10),
            make_entity_node(uuid="b", name="B", summary="x" * 200),
        ]
        projection: dict[str, list[Neighbor]] = {
            "a": [Neighbor(node_uuid="b", edge_count=1)],
            "b": [Neighbor(node_uuid="a", edge_count=1)],
        }
        result = _select_representative_members(members, projection=projection, sample_size=1)
        assert result[0].uuid == "b"
