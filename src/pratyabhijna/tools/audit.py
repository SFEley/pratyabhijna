"""Audit sub-agent: batch-evaluate graph nodes for hygiene issues."""
from __future__ import annotations

import json

AUDIT_REVISION = 1
"""Bumped by hand when audit evaluation logic changes; nodes audited at lower
revisions get re-discovered by the audit-rediscovery query."""

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
For each node you receive, decide one of three verdicts:

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


AUDIT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "uuid": {"type": "string"},
        "name": {"type": "string"},
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
    "required": ["uuid", "name", "status", "analysis"],
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
        key=lambda r: (len(r["_episode_uuids"]), tuple(r["_episode_uuids"])),
    )


def strip_internal_keys(requests: list[dict]) -> list[dict]:
    """Remove `_episode_uuids` (used only for client-side sorting) before submit."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in requests]
