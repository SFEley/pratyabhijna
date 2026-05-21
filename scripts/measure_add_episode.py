"""Measure graphiti.add_episode stage-by-stage.

Wraps graphiti's internal stage functions with timestamp logging, runs a
single add_episode through against a real Neo4j + Anthropic + Voyage stack,
and prints a per-stage breakdown. The output validates (or invalidates) the
cost diagnosis in doc/in-house-add-episode-design.md.

Usage:
    PRATYABHIJNA_ENV=dev uv run python scripts/measure_add_episode.py
    PRATYABHIJNA_ENV=dev uv run python scripts/measure_add_episode.py "custom body"

Notes:
    - This script makes real API calls. It will cost money.
    - Run against the dev environment, not prod.
    - Output goes to stdout; redirect with `> measure.log` if you want to keep it.
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from graphiti_core.utils.maintenance import edge_operations, node_operations

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.service import PratyabhijnaService


STAGE_TIMINGS: dict[str, list[float]] = {}


@contextmanager
def time_stage(name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        STAGE_TIMINGS.setdefault(name, []).append(elapsed)
        print(f"[stage] {name}: {elapsed * 1000:.0f}ms", flush=True)


def wrap(module, attr: str, name: str) -> None:
    """Monkey-patch a coroutine to record timing under ``name``."""
    orig = getattr(module, attr)

    async def wrapped(*args, **kwargs):
        with time_stage(name):
            return await orig(*args, **kwargs)

    setattr(module, attr, wrapped)


SAMPLE_BODY = (
    "Vesper spent today thinking about the cost of remember(). Per the May 18 "
    "log, a single 1095-char Observation cost roughly 22 Anthropic calls plus "
    "50 Voyage embedding calls and took 7m44s. The trace shows two pathologies: "
    "per-edge serial dedup in graphiti's resolve_extracted_edges, and at least "
    "one pathologically long Anthropic POST (a 4m20s gap in the log between "
    "two consecutive calls). The design proposes a three-LLM-call pipeline "
    "with batched Voyage embeddings as the replacement. This measurement run "
    "confirms which stage actually carries the cost."
)


async def main(body: str) -> None:
    wrap(node_operations, "extract_nodes", "extract_nodes")
    wrap(node_operations, "resolve_extracted_nodes", "resolve_extracted_nodes")
    wrap(node_operations, "extract_attributes_from_nodes", "extract_attributes_from_nodes")
    wrap(edge_operations, "extract_edges", "extract_edges")
    wrap(edge_operations, "resolve_extracted_edges", "resolve_extracted_edges")
    wrap(edge_operations, "resolve_extracted_edge", "resolve_extracted_edge_per_call")

    config = PratyabhijnaConfig.from_env()
    service = PratyabhijnaService(config)
    await service.start()
    try:
        with time_stage("TOTAL"):
            await service._graphiti.add_episode(
                name=f"measurement:{datetime.now(timezone.utc).isoformat()}",
                episode_body=body,
                source_description="measurement",
                reference_time=datetime.now(timezone.utc),
                group_id=config.subject_name or "measurement",
                entity_types=service.entity_types,
            )
    finally:
        await service.stop()

    print("\n=== Summary ===", flush=True)
    rows = [
        (name, sum(samples) * 1000, len(samples), (sum(samples) / len(samples)) * 1000)
        for name, samples in STAGE_TIMINGS.items()
    ]
    rows.sort(key=lambda r: -r[1])
    for name, total_ms, count, mean_ms in rows:
        print(
            f"  {name}: total={total_ms:.0f}ms calls={count} mean={mean_ms:.0f}ms",
            flush=True,
        )


if __name__ == "__main__":
    body = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_BODY
    asyncio.run(main(body))
