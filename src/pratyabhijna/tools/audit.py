"""Audit sub-agent: batch-evaluate any UUID-addressable graph object for hygiene issues.

Object kinds supported: entity nodes, episodic nodes, community nodes, saga
nodes, entity edges (RELATES_TO), episodic edges (MENTIONS), community edges
(HAS_MEMBER), and saga edges (HAS_EPISODE / NEXT_EPISODE).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from graphiti_core.edges import (
    CommunityEdge,
    EpisodicEdge,
    HasEpisodeEdge,
    NextEpisodeEdge,
)

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


async def resolve_uuid_list(input_str: str, *, service) -> list[str]:
    """Resolve an audit input string into a list of UUIDs (any object kind).

    If the input is whitespace-separated UUIDs, return them directly.
    Otherwise, dispatch to the natural-language `query` sub-agent and parse
    its response strictly: only the leading block of UUID-only lines counts
    as the cohort. Once a non-UUID line appears, parsing stops and the
    remainder is logged at WARNING. Order preserved, duplicates removed.

    Kind detection (node vs edge, which subclass) happens per-UUID after
    resolution, in `detect_kind`.
    """
    if is_uuid_list(input_str):
        return parse_uuid_list(input_str)
    augmented = (
        f"{input_str}\n\n"
        "Begin your response with the matching UUIDs (nodes or edges), one "
        "UUID per line, and nothing else before them. After the UUID list "
        "you may add prose explanation if useful, but only the leading block "
        "is read; anything after the first non-UUID line is ignored."
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


# ─────────────────────────────────────────────────────────────────────────────
# Object kind detection
# ─────────────────────────────────────────────────────────────────────────────

KIND_ENTITY_NODE = "entity_node"
KIND_EPISODIC_NODE = "episodic_node"
KIND_COMMUNITY_NODE = "community_node"
KIND_SAGA_NODE = "saga_node"
KIND_ENTITY_EDGE = "entity_edge"
KIND_EPISODIC_EDGE = "episodic_edge"
KIND_COMMUNITY_EDGE = "community_edge"
KIND_HAS_EPISODE_EDGE = "has_episode_edge"
KIND_NEXT_EPISODE_EDGE = "next_episode_edge"
KIND_UNKNOWN = "unknown"

NODE_KINDS = {KIND_ENTITY_NODE, KIND_EPISODIC_NODE, KIND_COMMUNITY_NODE, KIND_SAGA_NODE}
EDGE_KINDS = {
    KIND_ENTITY_EDGE, KIND_EPISODIC_EDGE, KIND_COMMUNITY_EDGE,
    KIND_HAS_EPISODE_EDGE, KIND_NEXT_EPISODE_EDGE,
}

_NODE_LABEL_TO_KIND = {
    "Episodic": KIND_EPISODIC_NODE,
    "Community": KIND_COMMUNITY_NODE,
    "Saga": KIND_SAGA_NODE,
    # Entity is the catch-all default for nodes — applied last when no other
    # label matches, so EntityNodes labeled like ['Entity', 'Person'] still
    # land here.
    "Entity": KIND_ENTITY_NODE,
}

_EDGE_TYPE_TO_KIND = {
    "RELATES_TO": KIND_ENTITY_EDGE,
    "MENTIONS": KIND_EPISODIC_EDGE,
    "HAS_MEMBER": KIND_COMMUNITY_EDGE,
    "HAS_EPISODE": KIND_HAS_EPISODE_EDGE,
    "NEXT_EPISODE": KIND_NEXT_EPISODE_EDGE,
}


async def detect_kind(service, uuid: str) -> str:
    """Return the object kind for a UUID, or KIND_UNKNOWN if it isn't found.

    Single Cypher query that checks both node and edge tables. The first match
    wins: a UUID can only be one kind in a sane graph.
    """
    cypher = """
    OPTIONAL MATCH (n {uuid: $uuid})
    OPTIONAL MATCH ()-[r {uuid: $uuid}]-()
    RETURN labels(n) AS node_labels, type(r) AS edge_type
    LIMIT 1
    """
    records, _, _ = await service._graphiti.driver.execute_query(
        cypher, uuid=uuid, routing_="r",
    )
    if not records:
        return KIND_UNKNOWN
    row = records[0]
    node_labels = row.get("node_labels") or []
    edge_type = row.get("edge_type")
    if node_labels:
        # Prefer the more specific kind (Episodic/Community/Saga) over the
        # generic Entity label.
        for label in ("Episodic", "Community", "Saga"):
            if label in node_labels:
                return _NODE_LABEL_TO_KIND[label]
        if "Entity" in node_labels:
            return KIND_ENTITY_NODE
    if edge_type and edge_type in _EDGE_TYPE_TO_KIND:
        return _EDGE_TYPE_TO_KIND[edge_type]
    return KIND_UNKNOWN


PARTIAL_BOOTSTRAP_FRAMING = """\
You are the subject (Vesper) acting on a Pratyabhijna memory-server request.
The following two sections are your SOUL (foundational commitments) and your
IDENTITY (current self-portrait). Read them as your own — they ARE you.

You are being asked to evaluate a single object from your knowledge graph
for hygiene issues. The object may be a node (Entity, Episodic, Community,
or Saga) or an edge (RELATES_TO, MENTIONS, HAS_MEMBER, HAS_EPISODE,
NEXT_EPISODE). The user message tells you which kind. You have full
authority over what stays and what changes.

# SOUL

{soul}

# IDENTITY

{identity}
"""

AUDIT_INSTRUCTIONS = """\
For the object you receive, decide one of three verdicts:

- **Valid** — the object is fine as-is. No structural problems, no obvious
  type errors, no missing properties that the source supplied. Default to
  Valid; don't flag minor stylistic concerns.
- **Update** — there is a clear, fixable problem. Provide a `request` field:
  a natural-language instruction that the update worker can execute. Be
  specific and operational.
- **Unfixable** — there is a problem you can identify but cannot confidently
  resolve from the available context, OR the object is noise that should be
  deleted. Provide your full analysis in the `analysis` field; a human will
  review. When recommending deletion, say so explicitly in the analysis.

Always provide an `analysis` field (1-3 sentences). Always respond as JSON
matching the schema — no prose outside the JSON.

The `uuid` field in your response must exactly match the UUID of the object
you received. Do not substitute a UUID from recall results, an edge endpoint,
or anywhere else — only the audit-target object's own UUID. Mismatches are
treated as errors.

# Per-kind guidance

## Entity nodes

Update for: wrong entity_type label, missing property the source episode
supplied, deprecated label (e.g. Position; should be Observation), broken
edges, group_id != "Vesper".

### Bare Entity nodes (special handling)

A node labeled only as `Entity` — with no secondary type from {Person, Event,
Place, Project, Artifact, Observation, Drive, Concept, Question, Thread} —
is suspect by default. The ten typed labels are designed to cover everything
that belongs in the graph: Person/Event/Place/Project/Artifact for the world,
Observation/Drive/Concept/Question/Thread for thought. If a candidate doesn't
fit any of them, the more likely diagnosis is that the extractor was
overenthusiastic — the candidate isn't a real entity — than that the taxonomy
has a gap.

Examine the source episode: does it refer to a stable, distinct referent that
future memories will reasonably connect to? If yes, classify (Update). If no
— generic noun phrase, throwaway reference, no meaningful edges, no clear
referent — verdict **Unfixable**, with an analysis explicitly recommending
deletion. Do not force a typed label onto extractor noise just because Update
requires one.

## Episodic nodes

The episode IS the source content; it can't be wrong about itself. Default
strongly to Valid. Update only for: malformed `episode_metadata`, wrong
`source` enum, wrong `group_id`. Unfixable→delete only for empty/broken
episodes that hold no recoverable content.

## Community nodes

Default to Valid. Update for: stale `summary` that no longer reflects the
member set, wrong `group_id`. Unfixable→delete for collapsed communities
(few or no members), since communities are auto-rebuilt and stale clusters
serve no purpose.

## Saga nodes

Default to Valid. Update for: stale `last_summarized_at`, wrong
`first_episode_uuid` / `last_episode_uuid` after episode deletions, wrong
`group_id`. Unfixable→delete for sagas with no member episodes.

## Edges (any kind)

Treat the edge's endpoints as load-bearing — the edge connects two specific
objects, and a wrong endpoint makes the relationship meaningless.

Update for: wrong `group_id`, fact text that contradicts the source episode,
deprecated relationship semantics, timestamps inconsistent with surrounding
edges.

Unfixable→delete for: edge points at a superseded/duplicate node, the
relationship is itself a duplicate (an equivalent edge already exists between
the same endpoints with the same semantics), the source episode no longer
exists, or the edge was created with bad metadata before a known fix and is
now redundant.

Do not enqueue a Thread for edge issues — Threads are for human-noticeable
identity-relevant findings, which are node-level. Edge-level findings live
in the JSON output and (for Update verdicts) in the edge's `notes` field.

# Inputs you receive

For nodes: (1) source episodes or related objects, (2) recall on the node's
name, (3) the node itself.
For edges: (1) the source episode(s) that produced the edge, (2) the edge's
source and target nodes as JSON, (3) the edge itself with all properties.

The user message names the object kind explicitly so you don't have to infer
it from the structure.
"""


def build_system_prompt(
    *,
    soul: str,
    identity: str,
    instructions: str = AUDIT_INSTRUCTIONS,
    guidance: str | None = None,
) -> list[dict]:
    """Two-block system prompt: SOUL+IDENTITY (1h cache) + instructions (1h cache).

    Both blocks use 1h TTL because the audit + downstream update share the same
    prefix and run as a paired flow, often with minutes between them. 5min would
    expire mid-run for any batch over a few hundred nodes; 2x write cost is a
    rounding error against the read savings on cohorts of even a few requests.
    """
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
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    return [bootstrap_block, instructions_block]


_KIND_HEADERS = {
    KIND_ENTITY_NODE: "Entity node",
    KIND_EPISODIC_NODE: "Episodic node",
    KIND_COMMUNITY_NODE: "Community node",
    KIND_SAGA_NODE: "Saga node",
    KIND_ENTITY_EDGE: "Entity edge (RELATES_TO)",
    KIND_EPISODIC_EDGE: "Episodic edge (MENTIONS)",
    KIND_COMMUNITY_EDGE: "Community edge (HAS_MEMBER)",
    KIND_HAS_EPISODE_EDGE: "Saga edge (HAS_EPISODE)",
    KIND_NEXT_EPISODE_EDGE: "Saga edge (NEXT_EPISODE)",
}


def _format_episodes(episodes: list[dict], *, kind: str) -> str:
    if kind in EDGE_KINDS:
        header = "# Source episodes (the prose that produced this edge)"
    elif kind == KIND_EPISODIC_NODE:
        header = "# Source episodes (none — the object IS the source episode)"
    elif kind == KIND_COMMUNITY_NODE:
        header = "# Member nodes (this community's current membership)"
    elif kind == KIND_SAGA_NODE:
        header = "# Endpoint episodes (first and last in the saga)"
    else:
        header = "# Source episodes (the prose that produced this node)"
    parts = [header]
    for ep in episodes:
        parts.append(f"\n## Episode {ep['uuid']}\n\n{ep.get('content', '')}")
    return "\n".join(parts)


def _format_recall(recall_results: dict, *, kind: str) -> str:
    nodes = recall_results.get("nodes", [])
    edges = recall_results.get("edges", [])
    if kind in EDGE_KINDS:
        header = "# Endpoints and recall context"
    else:
        header = "# Recall on this object's name (up to 5 results)"
    return (
        f"{header}\n\n"
        f"Nodes:\n{json.dumps(nodes, indent=2, default=str)}\n\n"
        f"Edges:\n{json.dumps(edges, indent=2, default=str)}"
    )


def _format_object(obj: dict, *, kind: str) -> str:
    label = _KIND_HEADERS.get(kind, "Object")
    return (
        f"# The {label.lower()} under audit\n\n"
        f"```json\n{json.dumps(obj, indent=2, default=str)}\n```"
    )


def build_user_message(
    *,
    obj: dict,
    episodes: list[dict],
    recall_results: dict,
    kind: str,
) -> dict:
    """Three-block user message: episodes/context (cached) + recall + object.

    `episodes` is overloaded by kind — for an edge it's source episodes; for
    a community node it's the member nodes; for a saga node it's the first
    and last episodes. The block header reflects the kind. Kept as a single
    cached block per request so cache-clustering by `_episode_uuids` still
    works for the dominant kinds (entity nodes and entity edges).
    """
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": _format_episodes(episodes, kind=kind),
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
            {
                "type": "text",
                "text": _format_recall(recall_results, kind=kind),
            },
            {
                "type": "text",
                "text": _format_object(obj, kind=kind),
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
    obj: dict,
    episodes: list[dict],
    recall_results: dict,
    kind: str,
    soul: str,
    identity: str,
    guidance: str | None,
    model: str,
    max_tokens: int = 4096,
) -> dict:
    """A single Anthropic Messages.batches request for one object audit.

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
                obj=obj, episodes=episodes, recall_results=recall_results,
                kind=kind,
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
    """Strip internal keys and submit. Returns batch ID.

    Caller is expected to have sorted the requests by episode tuple already
    (typically by `sort_requests_by_episodes`), so cohorts of same-episode
    requests cluster adjacently — the Batches API has no ordering SLA, but
    adjacency helps the per-episode cache breakpoint hit.
    """
    cleaned = strip_internal_keys(requests)
    _log.info("Submitting audit batch with %d requests", len(cleaned))
    batch = await client.messages.batches.create(requests=cleaned)
    _log.info("Batch submitted: id=%s", batch.id)
    return batch.id


async def run_primer_audit(
    client,
    request: dict,
    *,
    queue,
    node_names: dict[str, str],
    object_kinds: dict[str, str],
) -> tuple[dict, str | None, str | None]:
    """Run a single audit request synchronously to pre-warm the prompt cache.

    Submitting N parallel batch requests with identical 1h-cached prefixes
    causes most of them to race the cache write — none can read what the
    others are still writing, so each pays the 2x write premium. Running
    one request synchronously *first* commits the cache before the batch
    is submitted; the rest then read at 0.1x. Cohorts of one skip the
    batch entirely and just use this path.

    Returns ``(outcome, audited_node_uuid, audited_edge_uuid)`` matching
    `_process_one_audit_result`. The caller stamps audited_at and
    appends notes via `_stamp_and_annotate` so the primer's side effects
    land on the graph the same way batch results do.
    """
    custom_id = request["custom_id"]
    params = {k: v for k, v in request["params"].items()}
    try:
        message = await client.messages.create(**params)
    except Exception as exc:
        _log.exception("Primer audit %s failed before response", custom_id)
        return await _process_one_audit_result(
            custom_id,
            succeeded=False,
            message_or_error=f"{type(exc).__name__}: {exc}",
            queue=queue,
            node_names=node_names,
            object_kinds=object_kinds,
        )
    return await _process_one_audit_result(
        custom_id,
        succeeded=True,
        message_or_error=message,
        queue=queue,
        node_names=node_names,
        object_kinds=object_kinds,
    )


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


async def _process_one_audit_result(
    custom_id: str,
    *,
    succeeded: bool,
    message_or_error,
    queue,
    node_names: dict[str, str],
    object_kinds: dict[str, str],
) -> tuple[dict, str | None, str | None]:
    """Process one audit API result (from batch or primer). Shared logic so
    the synchronous primer and the batched cohort go through the same
    UUID-mismatch check, status dispatch, and Thread enqueue.

    Returns ``(outcome, audited_node_uuid, audited_edge_uuid)`` where the two
    UUID slots are mutually exclusive — at most one is non-None, and the
    caller stamps audited_at against the matching kind. Both are None when
    the outcome is an Error (no stamping for failures).
    """
    if not succeeded:
        error_str = str(message_or_error) if message_or_error is not None else "unknown"
        uuid = _uuid_from_custom_id(custom_id)
        _log.warning("Audit request %s failed: %s", custom_id, error_str)
        return (
            {
                "custom_id": custom_id,
                "uuid": uuid,
                "name": node_names.get(uuid, ""),
                "kind": object_kinds.get(uuid, KIND_UNKNOWN),
                "status": "Error",
                "error": error_str,
            },
            None,
            None,
        )

    message = message_or_error
    text = next(b.text for b in message.content if b.type == "text")
    parsed = json.loads(text)
    expected_uuid = _uuid_from_custom_id(custom_id)
    # Belt-and-braces against model echoing a wrong UUID (e.g. one from
    # recall results or an edge endpoint). The instructions pin this, but
    # if the model misbehaves we want to see it, not silently miss the
    # audited_at stamp on the original object.
    if parsed.get("uuid") != expected_uuid:
        _log.error(
            "Audit response UUID mismatch: expected=%s got=%s — "
            "treating as error, original object will not be stamped",
            expected_uuid,
            parsed.get("uuid"),
        )
        return (
            {
                "custom_id": custom_id,
                "uuid": expected_uuid,
                "name": node_names.get(expected_uuid, ""),
                "kind": object_kinds.get(expected_uuid, KIND_UNKNOWN),
                "status": "Error",
                "error": (
                    f"Audit response UUID mismatch: expected={expected_uuid} "
                    f"got={parsed.get('uuid')}"
                ),
            },
            None,
            None,
        )

    # Inject name and kind from input (model doesn't echo them back).
    parsed["name"] = node_names.get(parsed["uuid"], "")
    parsed["kind"] = object_kinds.get(parsed["uuid"], KIND_ENTITY_NODE)
    is_edge = parsed["kind"] in EDGE_KINDS

    if parsed["status"] == "Valid":
        _log.info("Audit Valid: %s (%s, %s)", parsed["name"], parsed["uuid"], parsed["kind"])
    elif parsed["status"] == "Update":
        _log.info(
            "Audit Update: %s (%s, %s) — %s",
            parsed["name"],
            parsed["uuid"],
            parsed["kind"],
            parsed.get("request", "<no request provided>"),
        )
    elif parsed["status"] == "Unfixable":
        # Threads are for node-level findings. Edges land in JSON only.
        if parsed["kind"] in NODE_KINDS:
            await _handle_unfixable(parsed, queue=queue)
        else:
            _log.warning(
                "Audit Unfixable (edge, no Thread): %s (%s, %s) — %s",
                parsed["name"], parsed["uuid"], parsed["kind"],
                parsed["analysis"],
            )

    return (
        parsed,
        parsed["uuid"] if not is_edge else None,
        parsed["uuid"] if is_edge else None,
    )


async def _stamp_and_annotate(
    service,
    outcomes: list[dict],
    audited_node_uuids: list[str],
    audited_edge_uuids: list[str],
) -> None:
    """Apply audited_at stamps and notes appends for a set of outcomes.

    Shared by `process_results` (batch path) and the primer path in
    `run_audit_run`. Notes only land on entity nodes (`attributes.notes`)
    and entity edges (free `notes` property on RELATES_TO).
    """
    if audited_node_uuids:
        await _stamp_audited_at_nodes(service, audited_node_uuids)
    if audited_edge_uuids:
        await _stamp_audited_at_edges(service, audited_edge_uuids)

    entity_node_updates = [
        o for o in outcomes
        if o.get("status") == "Update" and o.get("kind") == KIND_ENTITY_NODE
    ]
    entity_edge_updates = [
        o for o in outcomes
        if o.get("status") == "Update" and o.get("kind") == KIND_ENTITY_EDGE
    ]
    if entity_node_updates:
        await _append_update_notes_nodes(service, entity_node_updates)
    if entity_edge_updates:
        await _append_update_notes_edges(service, entity_edge_updates)


async def process_results(
    client,
    batch_id: str,
    *,
    service,
    queue,
    node_names: dict[str, str],
    object_kinds: dict[str, str] | None = None,
) -> list[dict]:
    """Iterate batch results, dispatch per-result side effects, stamp
    audited_at, append notes. Returns the list of outcome dicts.

    Per-result handling lives in `_process_one_audit_result`, which is also
    called by the synchronous primer path so the two share the UUID-mismatch
    check, status dispatch, and Thread enqueue. After-the-loop stamping and
    notes-appending live in `_stamp_and_annotate` for the same reason.
    """
    object_kinds = object_kinds or {}
    outcomes: list[dict] = []
    audited_node_uuids: list[str] = []
    audited_edge_uuids: list[str] = []

    # AsyncBatches.results() returns a coroutine that resolves to an async
    # iterator — must await before iterating.
    results_iter = await client.messages.batches.results(batch_id)
    async for result in results_iter:
        succeeded = result.result.type == "succeeded"
        message_or_error = (
            result.result.message if succeeded
            else getattr(result.result, "error", None)
        )
        outcome, node_uuid, edge_uuid = await _process_one_audit_result(
            result.custom_id,
            succeeded=succeeded,
            message_or_error=message_or_error,
            queue=queue,
            node_names=node_names,
            object_kinds=object_kinds,
        )
        outcomes.append(outcome)
        if node_uuid:
            audited_node_uuids.append(node_uuid)
        if edge_uuid:
            audited_edge_uuids.append(edge_uuid)

    await _stamp_and_annotate(
        service, outcomes, audited_node_uuids, audited_edge_uuids,
    )
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


def _format_audit_note(now: str, request: str) -> str:
    """Format the audit note that gets appended to notes fields."""
    return f"Audit {now}: {request}"


async def _append_update_notes_nodes(service, updates: list[dict]) -> None:
    """Append an audit note to the notes field of each Update Entity node.

    Preserves any existing notes content by appending with a blank-line
    separator. The note format is ``Audit {timestamp}: {request}`` so
    the pending fix survives even if update --input is never run.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = [
        {"uuid": u["uuid"], "note": _format_audit_note(now, u["request"])}
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


async def _append_update_notes_edges(service, updates: list[dict]) -> None:
    """Append an audit note to the notes property of each Update Entity edge.

    Edges don't have a typed `notes` field on the Python model, but Neo4j
    accepts custom properties freely; we set `r.notes` directly. Read-back
    via graphiti surfaces it in `EntityEdge.attributes`.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = [
        {"uuid": u["uuid"], "note": _format_audit_note(now, u["request"])}
        for u in updates
    ]
    # Directed arrow is required: an undirected pattern with two wildcard
    # endpoints binds the same relationship twice (once per direction), so
    # the non-idempotent SET would append `u.note` to `r.notes` twice.
    cypher = """
    UNWIND $updates AS u
    MATCH ()-[r:RELATES_TO {uuid: u.uuid}]->()
    SET r.notes = CASE
        WHEN r.notes IS NULL OR r.notes = '' THEN u.note
        ELSE r.notes + '\\n\\n' + u.note
    END
    """
    await service.execute_write_query(cypher, {"updates": params})
    _log.info("Appended audit notes to %d Update edges", len(updates))


async def _stamp_audited_at_nodes(service, uuids: list[str]) -> None:
    """Stamp audited_at + audit_revision on every node in `uuids`. Matches any
    node label, not just :Entity, so Episodic / Community / Saga get stamped too.
    """
    now = datetime.now(timezone.utc).isoformat()
    cypher = """
    UNWIND $uuids AS u
    MATCH (n {uuid: u})
    SET n.audited_at = $now, n.audit_revision = $rev
    """
    await service.execute_write_query(
        cypher,
        {"uuids": uuids, "now": now, "rev": AUDIT_REVISION},
    )
    _log.info("Stamped audited_at on %d nodes", len(uuids))


async def _stamp_audited_at_edges(service, uuids: list[str]) -> None:
    """Stamp audited_at + audit_revision on every edge in `uuids`. Matches any
    relationship type, not just RELATES_TO.
    """
    now = datetime.now(timezone.utc).isoformat()
    cypher = """
    UNWIND $uuids AS u
    MATCH ()-[r {uuid: u}]->()
    SET r.audited_at = $now, r.audit_revision = $rev
    """
    await service.execute_write_query(
        cypher,
        {"uuids": uuids, "now": now, "rev": AUDIT_REVISION},
    )
    _log.info("Stamped audited_at on %d edges", len(uuids))


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
        "group_id": getattr(ep, "group_id", ""),
    }


def _community_to_dict(node) -> dict:
    """Project a CommunityNode to a dict for the audit request body."""
    return {
        "uuid": node.uuid,
        "name": node.name,
        "summary": getattr(node, "summary", "") or "",
        "created_at": getattr(node, "created_at", None),
        "group_id": getattr(node, "group_id", ""),
    }


def _saga_to_dict(node) -> dict:
    """Project a SagaNode to a dict for the audit request body."""
    return {
        "uuid": node.uuid,
        "name": node.name,
        "summary": getattr(node, "summary", "") or "",
        "first_episode_uuid": getattr(node, "first_episode_uuid", None),
        "last_episode_uuid": getattr(node, "last_episode_uuid", None),
        "last_summarized_at": getattr(node, "last_summarized_at", None),
        "created_at": getattr(node, "created_at", None),
        "group_id": getattr(node, "group_id", ""),
    }


def _edge_to_dict(edge, *, edge_type: str) -> dict:
    """Project any edge class to a dict for the audit request body.

    `edge_type` is the Neo4j relationship type ("RELATES_TO" / "MENTIONS" /
    "HAS_MEMBER" / "HAS_EPISODE" / "NEXT_EPISODE"); not all fields are present
    on all kinds, so we use getattr defensively.
    """
    return {
        "uuid": edge.uuid,
        "edge_type": edge_type,
        "source_node_uuid": getattr(edge, "source_node_uuid", None),
        "target_node_uuid": getattr(edge, "target_node_uuid", None),
        "name": getattr(edge, "name", "") or "",
        "fact": getattr(edge, "fact", "") or "",
        "valid_at": getattr(edge, "valid_at", None),
        "invalid_at": getattr(edge, "invalid_at", None),
        "expired_at": getattr(edge, "expired_at", None),
        "episodes": list(getattr(edge, "episodes", []) or []),
        "attributes": dict(getattr(edge, "attributes", {}) or {}),
        "created_at": getattr(edge, "created_at", None),
        "group_id": getattr(edge, "group_id", ""),
    }


async def _load_endpoint_node(service, uuid: str) -> dict:
    """Best-effort fetch of an edge endpoint as a dict. Endpoint nodes can be
    any kind; we try Entity, then Episodic, then Community, then Saga. Returns
    a minimal dict with kind + uuid + name on success, or a stub if not found.
    """
    for kind, fetch, project in (
        (KIND_ENTITY_NODE, service.get_entity_by_uuid, _entity_to_dict),
        (KIND_EPISODIC_NODE, service.get_episode_by_uuid, _episode_to_dict),
        (KIND_COMMUNITY_NODE, service.get_community_by_uuid, _community_to_dict),
        (KIND_SAGA_NODE, service.get_saga_by_uuid, _saga_to_dict),
    ):
        try:
            node = await fetch(uuid)
            d = project(node)
            d["_kind"] = kind
            return d
        except Exception:
            continue
    return {"_kind": KIND_UNKNOWN, "uuid": uuid, "name": "<endpoint not found>"}


async def load_object(service, uuid: str, kind: str) -> tuple[dict, list[dict], dict, str]:
    """Fetch the object plus its prompt context. Returns (object_dict,
    context_episodes, recall_results, name).

    The middle two values feed `build_audit_request`'s `episodes` and
    `recall_results` parameters; the contents are kind-specific (the prompt
    headers explain what they are for each kind).
    """
    driver = service._graphiti.driver

    if kind == KIND_ENTITY_NODE:
        node = await service.get_entity_by_uuid(uuid)
        episodes = await service.get_episodes_for_node(uuid)
        recall_result = await recall(service, query=node.name, limit=5)
        return _entity_to_dict(node), [_episode_to_dict(e) for e in episodes], recall_result, node.name

    if kind == KIND_EPISODIC_NODE:
        ep = await service.get_episode_by_uuid(uuid)
        # The episode IS the source — no parent episodes. Recall on the first
        # ~80 chars of content gives the auditor topical context.
        snippet = (getattr(ep, "content", "") or "")[:80]
        recall_result = (
            await recall(service, query=snippet, limit=5)
            if snippet else {"nodes": [], "edges": []}
        )
        return _episode_to_dict(ep), [], recall_result, ep.name

    if kind == KIND_COMMUNITY_NODE:
        node = await service.get_community_by_uuid(uuid)
        recall_result = await recall(service, query=node.name, limit=5)
        # The "episodes" block carries member context for communities.
        members = await _community_members_dicts(service, uuid)
        return _community_to_dict(node), members, recall_result, node.name

    if kind == KIND_SAGA_NODE:
        node = await service.get_saga_by_uuid(uuid)
        endpoint_uuids = [
            u for u in (
                getattr(node, "first_episode_uuid", None),
                getattr(node, "last_episode_uuid", None),
            ) if u
        ]
        endpoints = (
            await service.get_episodes_by_uuids(endpoint_uuids)
            if endpoint_uuids else []
        )
        return _saga_to_dict(node), [_episode_to_dict(e) for e in endpoints], {"nodes": [], "edges": []}, node.name

    if kind == KIND_ENTITY_EDGE:
        edge = await service.get_edge(uuid)
        edge_dict = _edge_to_dict(edge, edge_type="RELATES_TO")
        episode_uuids = list(getattr(edge, "episodes", []) or [])
        episodes = (
            await service.get_episodes_by_uuids(episode_uuids)
            if episode_uuids else []
        )
        episode_dicts = [_episode_to_dict(e) for e in episodes]
        # For edges, the "recall" block carries the endpoints as JSON.
        endpoints = {
            "source": await _load_endpoint_node(service, edge.source_node_uuid),
            "target": await _load_endpoint_node(service, edge.target_node_uuid),
        }
        recall_result = {"nodes": [endpoints["source"], endpoints["target"]], "edges": []}
        name = (getattr(edge, "fact", None) or getattr(edge, "name", None) or "<edge>")
        return edge_dict, episode_dicts, recall_result, name

    if kind in (
        KIND_EPISODIC_EDGE,
        KIND_COMMUNITY_EDGE,
        KIND_HAS_EPISODE_EDGE,
        KIND_NEXT_EPISODE_EDGE,
    ):
        edge_class = {
            KIND_EPISODIC_EDGE: EpisodicEdge,
            KIND_COMMUNITY_EDGE: CommunityEdge,
            KIND_HAS_EPISODE_EDGE: HasEpisodeEdge,
            KIND_NEXT_EPISODE_EDGE: NextEpisodeEdge,
        }[kind]
        edge_type = {
            KIND_EPISODIC_EDGE: "MENTIONS",
            KIND_COMMUNITY_EDGE: "HAS_MEMBER",
            KIND_HAS_EPISODE_EDGE: "HAS_EPISODE",
            KIND_NEXT_EPISODE_EDGE: "NEXT_EPISODE",
        }[kind]
        edge = await edge_class.get_by_uuid(driver, uuid)
        edge_dict = _edge_to_dict(edge, edge_type=edge_type)
        endpoints = {
            "source": await _load_endpoint_node(service, edge.source_node_uuid),
            "target": await _load_endpoint_node(service, edge.target_node_uuid),
        }
        recall_result = {"nodes": [endpoints["source"], endpoints["target"]], "edges": []}
        # MENTIONS edges link an Episodic source to an Entity target; the
        # source IS the source episode, so include it directly.
        episodes: list[dict] = []
        if kind == KIND_EPISODIC_EDGE:
            src = endpoints["source"]
            if src.get("_kind") == KIND_EPISODIC_NODE:
                episodes = [src]
        name = f"{edge_type} {edge.uuid[:8]}"
        return edge_dict, episodes, recall_result, name

    raise ValueError(f"Unknown kind: {kind!r}")


async def _community_members_dicts(service, community_uuid: str) -> list[dict]:
    """Fetch the entity-node members of a community as projected dicts."""
    cypher = """
    MATCH (c:Community {uuid: $uuid})-[:HAS_MEMBER]->(m:Entity)
    RETURN m.uuid AS uuid, m.name AS name, labels(m) AS labels,
           m.summary AS summary, m.group_id AS group_id
    LIMIT 50
    """
    records, _, _ = await service._graphiti.driver.execute_query(
        cypher, uuid=community_uuid, routing_="r",
    )
    return [
        {
            "uuid": r["uuid"],
            "name": r["name"],
            "labels": list(r["labels"] or []),
            "summary": r["summary"] or "",
            "group_id": r["group_id"] or "",
        }
        for r in records
    ]


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

    uuids = await resolve_uuid_list(input_str, service=service)
    _log.info("Audit cohort: %d UUIDs", len(uuids))

    model = service.config.llm.audit_model
    requests: list[dict] = []
    object_names: dict[str, str] = {}
    object_kinds: dict[str, str] = {}
    skipped_unknown: list[str] = []

    for uuid in uuids:
        kind = await detect_kind(service, uuid)
        if kind == KIND_UNKNOWN:
            _log.warning("Audit: UUID %s not found in graph; skipping", uuid)
            skipped_unknown.append(uuid)
            continue
        try:
            obj_dict, episode_dicts, recall_result, name = await load_object(
                service, uuid, kind,
            )
        except Exception as exc:
            _log.exception("Audit: failed to load %s (%s)", uuid, kind)
            skipped_unknown.append(uuid)
            continue
        object_names[uuid] = name
        object_kinds[uuid] = kind
        requests.append(build_audit_request(
            custom_id=f"audit-{uuid}",
            obj=obj_dict,
            episodes=episode_dicts,
            recall_results=recall_result,
            kind=kind,
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

    # Sort by episode tuple so same-episode requests cluster. The first
    # sorted request becomes the synchronous primer — it warms both the
    # global SOUL+IDENTITY+instructions cache and the episode cache for
    # the leading batch group.
    sorted_requests = sort_requests_by_episodes(requests)
    primer_request = sorted_requests[0]
    batch_requests = sorted_requests[1:]

    primer_outcome, primer_node, primer_edge = await run_primer_audit(
        anthropic_client, primer_request,
        queue=queue,
        node_names=object_names,
        object_kinds=object_kinds,
    )

    if primer_outcome.get("status") == "Error":
        # Cache wasn't warmed — submitting the batch would burn the 2x
        # write premium on every request again. Abort and synthesize
        # Errors for the rest so the JSON file accounts for the cohort.
        _log.error(
            "Primer audit failed; skipping batch of %d to avoid uncached "
            "writes. Re-run when ready.", len(batch_requests),
        )
        results = [primer_outcome] + [
            {
                "custom_id": r["custom_id"],
                "uuid": _uuid_from_custom_id(r["custom_id"]),
                "name": object_names.get(_uuid_from_custom_id(r["custom_id"]), ""),
                "kind": object_kinds.get(_uuid_from_custom_id(r["custom_id"]), KIND_UNKNOWN),
                "status": "Error",
                "error": "Primer audit failed; batch not submitted",
            }
            for r in batch_requests
        ]
    else:
        # Stamp the primer's audited_at + notes before the batch starts
        # so a mid-batch failure still leaves the primer recorded on the
        # graph. Batch results get the same treatment via process_results.
        await _stamp_and_annotate(
            service,
            [primer_outcome],
            [primer_node] if primer_node else [],
            [primer_edge] if primer_edge else [],
        )

        if not batch_requests:
            results = [primer_outcome]
        else:
            batch_id = await submit_audit_batch(anthropic_client, batch_requests)
            await poll_batch(anthropic_client, batch_id, interval=60.0)
            batch_outcomes = await process_results(
                anthropic_client, batch_id,
                service=service, queue=queue,
                node_names=object_names,
                object_kinds=object_kinds,
            )
            results = [primer_outcome] + batch_outcomes
    # Synthetic Error outcomes for UUIDs we couldn't load — they show up in
    # the JSON output and the count summary so nothing silently disappears.
    for uuid in skipped_unknown:
        results.append({
            "uuid": uuid,
            "name": "<not found>",
            "status": "Error",
            "analysis": "UUID not found in graph or failed to load.",
            "error": "load_failed",
        })

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
