"""Audit sub-agent: batch-evaluate graph nodes for hygiene issues."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from pratyabhijna.log import get_logger
from pratyabhijna.tools.query import query as _call_query
from pratyabhijna.tools.recall import recall
from pratyabhijna.tools.remember import remember

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
    Otherwise, dispatch to the natural-language `query` sub-agent and parse
    its response strictly: only the leading block of UUID-only lines counts
    as the cohort. Once a non-UUID line appears, parsing stops and the
    remainder is logged at WARNING. Order preserved, duplicates removed.
    """
    if is_uuid_list(input_str):
        return parse_uuid_list(input_str)
    augmented = (
        f"{input_str}\n\n"
        "Begin your response with the matching node UUIDs, one UUID per line, "
        "and nothing else before them. After the UUID list you may add prose "
        "explanation if useful, but only the leading block is read; anything "
        "after the first non-UUID line is ignored."
    )
    result = await _call_query(service, augmented)
    response = result.get("response", "") or ""
    seen: set[str] = set()
    uuids: list[str] = []
    lines = response.splitlines()
    cutoff = 0
    for cutoff, line in enumerate(lines):
        token = line.strip()
        if not token:
            # Blank line ends the leading block too — anything after is prose.
            break
        if not UUID_RE.fullmatch(token):
            break
        normalized = token.lower()
        if normalized not in seen:
            seen.add(normalized)
            uuids.append(normalized)
    else:
        # Loop completed without break — every line was a UUID, no tail.
        cutoff = len(lines)
    tail = "\n".join(lines[cutoff:]).strip()
    if tail:
        _log.warning("Trailing prose from query agent ignored: %s", tail)
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

The `uuid` field in your response must exactly match the UUID of the node
you received in the user message. Do not substitute a UUID from the recall
results, an edge target, or anywhere else — only the audit-target node's
own UUID. Mismatches are treated as errors.

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


async def process_results(
    client,
    batch_id: str,
    *,
    service,
    queue,
    node_names: dict[str, str],
) -> list[dict]:
    """Iterate batch results, dispatch side effects, return list of structured outcomes.

    Each outcome dict carries the model's verdict plus injected `name` (from
    `node_names`). Side effects:
    - Valid: INFO log only
    - Update: INFO log; the result dict is consumed downstream by `update --input`
    - Unfixable: WARNING log + Thread enqueue via `remember()`
    - Errored: WARNING log; outcome dict has status "Error"
    On completion: stamp audited_at + audit_revision on every node we processed
    via a single batched Cypher MERGE.
    """
    outcomes: list[dict] = []
    audited_uuids: list[str] = []

    # AsyncBatches.results() returns a coroutine that resolves to an async
    # iterator — must await before iterating.
    results_iter = await client.messages.batches.results(batch_id)
    async for result in results_iter:
        if result.result.type != "succeeded":
            error_str = str(getattr(result.result, "error", "unknown"))
            uuid = _uuid_from_custom_id(result.custom_id)
            _log.warning("Audit request %s failed: %s", result.custom_id, error_str)
            outcomes.append({
                "custom_id": result.custom_id,
                "uuid": uuid,
                "name": node_names.get(uuid, ""),
                "status": "Error",
                "error": error_str,
            })
            continue

        text = next(
            b.text for b in result.result.message.content if b.type == "text"
        )
        parsed = json.loads(text)
        expected_uuid = _uuid_from_custom_id(result.custom_id)
        # Belt-and-braces against model echoing a wrong UUID (e.g. one from
        # recall results or an edge target). The instructions pin this, but
        # if the model misbehaves we want to see it, not silently miss the
        # audited_at stamp on the original node.
        if parsed.get("uuid") != expected_uuid:
            _log.error(
                "Audit response UUID mismatch: expected=%s got=%s — "
                "treating as error, original node will not be stamped",
                expected_uuid,
                parsed.get("uuid"),
            )
            outcomes.append({
                "custom_id": result.custom_id,
                "uuid": expected_uuid,
                "name": node_names.get(expected_uuid, ""),
                "status": "Error",
                "error": (
                    f"Audit response UUID mismatch: expected={expected_uuid} "
                    f"got={parsed.get('uuid')}"
                ),
            })
            continue
        # Inject name from input node (model no longer echoes it back)
        parsed["name"] = node_names.get(parsed["uuid"], "")
        outcomes.append(parsed)
        audited_uuids.append(parsed["uuid"])

        if parsed["status"] == "Valid":
            _log.info("Audit Valid: %s (%s)", parsed["name"], parsed["uuid"])
        elif parsed["status"] == "Update":
            _log.info(
                "Audit Update: %s (%s) — %s",
                parsed["name"],
                parsed["uuid"],
                parsed.get("request", "<no request provided>"),
            )
        elif parsed["status"] == "Unfixable":
            await _handle_unfixable(parsed, queue=queue)

    if audited_uuids:
        await _stamp_audited_at(service, audited_uuids)

    update_outcomes = [o for o in outcomes if o.get("status") == "Update"]
    if update_outcomes:
        await _append_update_notes(service, update_outcomes)

    return outcomes


def _uuid_from_custom_id(custom_id: str) -> str:
    """Strip the `audit-` prefix to recover the bare UUID."""
    return custom_id.removeprefix("audit-")


async def _handle_unfixable(parsed: dict, *, queue) -> None:
    """WARNING log + enqueue a Thread alerting the subject."""
    _log.warning(
        "Audit Unfixable: %s (%s) — %s",
        parsed.get("name", ""),
        parsed["uuid"],
        parsed["analysis"],
    )
    thread_content = (
        f"Audit found an unfixable issue with node '{parsed.get('name', '')}' "
        f"({parsed['uuid']}): {parsed['analysis']}. Needs human review."
    )
    await remember(
        queue,
        thread_content,
        memory_type="Thread",
        source="audit",
    )


async def _append_update_notes(service, updates: list[dict]) -> None:
    """Append an audit note to the notes field of each Update node.

    Preserves any existing notes content by appending with a blank-line
    separator. The note format is ``Audit {timestamp}: {request}`` so
    the pending fix survives even if update --input is never run.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = [
        {"uuid": u["uuid"], "note": f"Audit {now}: {u['request']}"}
        for u in updates
    ]
    cypher = """
    UNWIND $updates AS u
    MATCH (n:Entity {uuid: u.uuid})
    SET n.notes = CASE
        WHEN n.notes IS NULL OR n.notes = '' THEN u.note
        ELSE n.notes + '\\n\\n' + u.note
    END
    """
    await service.execute_write_query(cypher, {"updates": params})
    _log.info("Appended audit notes to %d Update nodes", len(updates))


async def _stamp_audited_at(service, uuids: list[str]) -> None:
    """Single batched MERGE setting audited_at + audit_revision on each UUID."""
    now = datetime.now(timezone.utc).isoformat()
    cypher = """
    UNWIND $uuids AS u
    MATCH (n:Entity {uuid: u})
    SET n.audited_at = $now, n.audit_revision = $rev
    """
    await service.execute_write_query(
        cypher,
        {"uuids": uuids, "now": now, "rev": AUDIT_REVISION},
    )
    _log.info("Stamped audited_at on %d nodes", len(uuids))


def _entity_to_dict(node) -> dict:
    """Project an EntityNode to a dict for the audit request body."""
    return {
        "uuid": node.uuid,
        "name": node.name,
        "labels": list(getattr(node, "labels", []) or []),
        "summary": getattr(node, "summary", "") or "",
        "attributes": dict(getattr(node, "attributes", {}) or {}),
        "created_at": getattr(node, "created_at", None),
        "group_id": getattr(node, "group_id", ""),
    }


def _episode_to_dict(ep) -> dict:
    """Project an EpisodicNode to a dict for the audit request body."""
    return {
        "uuid": ep.uuid,
        "name": getattr(ep, "name", ""),
        "content": getattr(ep, "content", ""),
        "source": getattr(ep, "source", ""),
        "source_description": getattr(ep, "source_description", ""),
        "valid_at": getattr(ep, "valid_at", None),
    }


async def run_audit_run(
    service,
    queue,
    input_str: str,
    *,
    guidance: str | None,
    anthropic_client,
) -> dict:
    """Resolve UUIDs, build batch requests, submit, poll, process. Returns
    a dict with `results` (list of outcome dicts), `started_at`, `completed_at`,
    `cohort_size`, `audit_revision`, `model`, `guidance`."""
    from pratyabhijna.synthesis import read_identity_files

    started_at = datetime.now(timezone.utc).isoformat()

    # Partial bootstrap — only SOUL + IDENTITY (Decision: partial bootstrap shape)
    tiers = read_identity_files(service.config.resources.repo_path)
    soul = tiers.get("soul") or ""
    identity = tiers.get("identity") or ""

    uuids = await resolve_node_list(input_str, service=service)
    _log.info("Audit cohort: %d nodes", len(uuids))

    model = service.config.llm.audit_model
    requests: list[dict] = []
    node_names: dict[str, str] = {}

    for uuid in uuids:
        node = await service.get_entity_by_uuid(uuid)
        node_names[uuid] = node.name
        node_dict = _entity_to_dict(node)
        episodes = await service.get_episodes_for_node(uuid)
        episode_dicts = [_episode_to_dict(e) for e in episodes]
        recall_result = await recall(service, query=node.name, limit=5)
        requests.append(build_audit_request(
            custom_id=f"audit-{uuid}",
            node=node_dict,
            episodes=episode_dicts,
            recall_results=recall_result,
            soul=soul, identity=identity,
            guidance=guidance,
            model=model,
        ))

    if not requests:
        completed_at = datetime.now(timezone.utc).isoformat()
        return {
            "results": [],
            "started_at": started_at,
            "completed_at": completed_at,
            "cohort_size": 0,
            "audit_revision": AUDIT_REVISION,
            "model": model,
            "guidance": guidance,
        }

    batch_id = await submit_audit_batch(anthropic_client, requests)
    await poll_batch(anthropic_client, batch_id, interval=60.0)
    results = await process_results(
        anthropic_client, batch_id,
        service=service, queue=queue,
        node_names=node_names,
    )

    completed_at = datetime.now(timezone.utc).isoformat()
    return {
        "results": results,
        "started_at": started_at,
        "completed_at": completed_at,
        "cohort_size": len(uuids),
        "audit_revision": AUDIT_REVISION,
        "model": model,
        "guidance": guidance,
    }
