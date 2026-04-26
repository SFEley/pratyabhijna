"""Audit sub-agent: batch-evaluate graph nodes for hygiene issues."""
from __future__ import annotations

import asyncio
import json
import re

from pratyabhijna.log import get_logger
from pratyabhijna.tools.query import query as _call_query

_log = get_logger(__name__)

AUDIT_REVISION = 1
"""Bumped by hand when audit evaluation logic changes; nodes audited at lower
revisions get re-discovered by the audit-rediscovery query."""

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def is_uuid_list(text: str) -> bool:
    """True if the input is whitespace-separated canonical UUIDs only.

    Empty / whitespace-only input returns False.
    """
    tokens = text.split()
    if not tokens:
        return False
    return all(UUID_RE.fullmatch(tok) for tok in tokens)


def parse_uuid_list(text: str) -> list[str]:
    """Split a UUID list on any whitespace. Order preserved, case normalized
    to lowercase to match the natural-language resolution path."""
    return [tok.lower() for tok in text.split()]


async def resolve_node_list(input_str: str, *, service) -> list[str]:
    """Resolve an audit input string into a list of node UUIDs.

    If the input is whitespace-separated UUIDs, return them directly.
    Otherwise, dispatch to the natural-language `query` sub-agent and extract
    UUIDs from its response text. Order preserved, duplicates removed.
    """
    if is_uuid_list(input_str):
        return parse_uuid_list(input_str)
    augmented = (
        f"{input_str}\n\n"
        "List the matching node UUIDs in your response."
    )
    result = await _call_query(service, augmented)
    seen: set[str] = set()
    uuids: list[str] = []
    # Extraction is text-only — any UUID-shaped string in the agent's prose is
    # accepted, including ones the agent mentioned in refusal or error contexts.
    # Tasks 4-5 should treat the returned list as candidates, not confirmed matches.
    for match in UUID_RE.findall(result.get("response", "")):
        normalized = match.lower()
        if normalized not in seen:
            seen.add(normalized)
            uuids.append(normalized)
    return uuids


PARTIAL_BOOTSTRAP_FRAMING = """\
You are the subject (Vesper) acting on a Pratyabhijna memory-server request.
The following two sections are your SOUL (foundational commitments) and your
IDENTITY (current self-portrait). Read them as your own — they ARE you.

You are being asked to evaluate a single node from your knowledge graph for
hygiene issues. You have full authority over what stays and what changes.

# SOUL

{soul}

# IDENTITY

{identity}
"""

AUDIT_INSTRUCTIONS = """\
For the node you receive, decide one of three verdicts:

- **Valid** — the node is fine as-is. No structural problems, no obvious type
  errors, no missing properties that the source episode supplied. Default to
  Valid; don't flag minor stylistic concerns.
- **Update** — there is a clear, fixable problem (wrong entity_type, missing
  property the source supplied, broken edge, deprecated label, etc.). Provide
  a `request` field: a natural-language instruction that the update worker
  can execute. Be specific and operational.
- **Unfixable** — there is a problem you can identify but cannot confidently
  resolve from the available context. Provide your full analysis in the
  `analysis` field; a human will review.

Always provide an `analysis` field (1-3 sentences). Always respond as JSON
matching the schema — no prose outside the JSON.

You will receive: (1) the node's source episode(s), (2) recall results on
the node's name (up to 5), (3) the node itself with properties and edges.
"""


def build_system_prompt(
    *,
    soul: str,
    identity: str,
    instructions: str = AUDIT_INSTRUCTIONS,
    guidance: str | None = None,
) -> list[dict]:
    """Two-block system prompt: SOUL+IDENTITY (1h cache) + instructions (5min)."""
    bootstrap_block = {
        "type": "text",
        "text": PARTIAL_BOOTSTRAP_FRAMING.format(soul=soul, identity=identity),
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    instructions_text = instructions
    if guidance:
        instructions_text = (
            f"{instructions}\n\n# Additional guidance for this run\n\n{guidance}"
        )
    instructions_block = {
        "type": "text",
        "text": instructions_text,
        "cache_control": {"type": "ephemeral"},  # default 5min TTL
    }
    return [bootstrap_block, instructions_block]


def _format_episodes(episodes: list[dict]) -> str:
    parts = ["# Source episodes (the prose that produced this node)"]
    for ep in episodes:
        parts.append(f"\n## Episode {ep['uuid']}\n\n{ep.get('content', '')}")
    return "\n".join(parts)


def _format_recall(recall_results: dict) -> str:
    nodes = recall_results.get("nodes", [])
    edges = recall_results.get("edges", [])
    return (
        "# Recall on this node's name (up to 5 results)\n\n"
        f"Nodes:\n{json.dumps(nodes, indent=2, default=str)}\n\n"
        f"Edges:\n{json.dumps(edges, indent=2, default=str)}"
    )


def _format_node(node: dict) -> str:
    return (
        "# The node under audit\n\n"
        f"```json\n{json.dumps(node, indent=2, default=str)}\n```"
    )


def build_user_message(
    *,
    node: dict,
    episodes: list[dict],
    recall_results: dict,
) -> dict:
    """Three-block user message: episodes (cached) + recall + node."""
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": _format_episodes(episodes),
                "cache_control": {"type": "ephemeral"},  # 5min
            },
            {
                "type": "text",
                "text": _format_recall(recall_results),
            },
            {
                "type": "text",
                "text": _format_node(node),
            },
        ],
    }


# `request` is required by the prompt when status=Update but cannot be declared
# conditionally required in JSON Schema without if/then (not supported by all
# backends). Task 5 result handlers must guard with `entry.get("request", "")`.
AUDIT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "uuid": {"type": "string"},
        "status": {"type": "string", "enum": ["Valid", "Update", "Unfixable"]},
        "analysis": {"type": "string"},
        "request": {
            "type": "string",
            "description": (
                "Natural-language instruction for the update worker. "
                "Required when status=Update."
            ),
        },
    },
    "required": ["uuid", "status", "analysis"],
    "additionalProperties": False,
}


def build_audit_request(
    *,
    custom_id: str,
    node: dict,
    episodes: list[dict],
    recall_results: dict,
    soul: str,
    identity: str,
    guidance: str | None,
    model: str,
    max_tokens: int = 4096,
) -> dict:
    """A single Anthropic Messages.batches request for one node audit.

    Returns a dict with `custom_id`, `params`, and `_episode_uuids` (an internal
    key used by `sort_requests_by_episodes` for cache-clustering — strip before
    sending to Anthropic).
    """
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "system": build_system_prompt(
                soul=soul, identity=identity, guidance=guidance,
            ),
            "messages": [build_user_message(
                node=node, episodes=episodes, recall_results=recall_results,
            )],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": AUDIT_RESPONSE_SCHEMA,
                },
            },
        },
        "_episode_uuids": [ep["uuid"] for ep in episodes],
    }


def sort_requests_by_episodes(requests: list[dict]) -> list[dict]:
    """Cluster requests sharing the same source-episode tuple to maximize
    Episode-cache hit probability under the Batches API (no ordering SLA)."""
    return sorted(
        requests,
        key=lambda r: tuple(r["_episode_uuids"]),
    )


def strip_internal_keys(requests: list[dict]) -> list[dict]:
    """Remove `_episode_uuids` (used only for client-side sorting) before submit."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in requests]


async def submit_audit_batch(client, requests: list[dict]) -> str:
    """Sort by episode, strip internal keys, submit. Returns batch ID.

    Sorting clusters same-episode requests adjacently to maximize cache-hit
    probability under the Batches API (no ordering SLA, but adjacency helps).
    """
    sorted_reqs = sort_requests_by_episodes(requests)
    cleaned = strip_internal_keys(sorted_reqs)
    _log.info("Submitting audit batch with %d requests", len(cleaned))
    batch = await client.messages.batches.create(requests=cleaned)
    _log.info("Batch submitted: id=%s", batch.id)
    return batch.id


async def poll_batch(client, batch_id: str, *, interval: float = 60.0):
    """Poll until processing_status='ended'. INFO-log progress on each poll
    that's still in progress; INFO-log a summary on completion."""
    while True:
        batch = await client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            _log.info(
                "Batch %s ended: succeeded=%d errored=%d",
                batch_id,
                batch.request_counts.succeeded,
                batch.request_counts.errored,
            )
            return batch
        _log.info(
            "Batch %s status=%s processing=%d succeeded=%d errored=%d",
            batch_id,
            batch.processing_status,
            batch.request_counts.processing,
            batch.request_counts.succeeded,
            batch.request_counts.errored,
        )
        await asyncio.sleep(interval)
