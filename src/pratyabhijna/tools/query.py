"""The ``query`` MCP tool.

A natural-language → Cypher translator and spot-maintenance agent.
Accepts a prose request, dispatches a Sonnet 4.6 sub-agent with
adaptive thinking, and returns the executed result.

The sub-agent has two tools:

* ``execute_cypher_read`` — read-only Cypher. Regex-gated against
  mutation clauses so the read path cannot mutate even if the model
  is confused.
* ``execute_cypher_write`` — minor maintenance only. The sub-agent's
  system prompt defines what qualifies; the sub-agent itself is
  empowered to refuse.

Bootstrap (identity tiers) and graph schema are loaded once per
hour, in parallel, and cached in module memory. The same hour-long
TTL is set on Anthropic's prompt cache so both caches refresh
together.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from string import Template
from typing import TYPE_CHECKING, Any

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import IDENTITY_FILES, read_identity_files

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


# --- Configuration ---

_CACHE_TTL_SECONDS = 60 * 60  # 1 hour — matches the Anthropic cache TTL below
_MAX_ITERATIONS = 8
_MAX_TOKENS = 16000
_MAX_RESULTS_CEILING = 500
_MAX_RESULTS_DEFAULT = 50


# --- Safety: forbid mutation clauses in the read tool ---

_READ_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH)\b",
    re.IGNORECASE,
)


# --- Tool schemas exposed to the sub-agent ---

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "execute_cypher_read",
        "description": (
            "Execute a read-only Cypher query and return up to max_results "
            "rows. Use for investigation, counting, listing, and inspection. "
            "The query MUST NOT contain CREATE, MERGE, DELETE, SET, REMOVE, "
            "DROP, or DETACH — mutation clauses are rejected by the server "
            "before execution. Prefer parameterized queries: put variable "
            "data in `params`, not string-interpolated. Nodes and "
            "relationships are returned as JSON objects with `_type`, "
            "`labels`/`type`, and `properties` keys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The Cypher read query.",
                },
                "params": {
                    "type": "object",
                    "description": "Optional parameters for the query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"Maximum rows to return. Default {_MAX_RESULTS_DEFAULT}, "
                        f"hard ceiling {_MAX_RESULTS_CEILING}."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "execute_cypher_write",
        "description": (
            "Execute a Cypher write query (CREATE, MERGE, SET, DELETE, "
            "REMOVE, DETACH). Only use for minor maintenance: filling a "
            "missing property, removing a duplicate, deleting a single "
            "orphaned node, fixing one mis-assigned group_id. Do NOT use "
            "for bulk deletions, blanket property updates, schema "
            "refactors, or operations on the subject's identity "
            "attributes. Returns a counters summary: nodes/relationships "
            "created and deleted, properties set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The Cypher write query.",
                },
                "params": {
                    "type": "object",
                    "description": "Optional parameters for the query.",
                },
            },
            "required": ["query"],
        },
    },
]


# --- System prompt template ---
#
# Uses string.Template ($-syntax) rather than str.format because
# bootstrap text and schema text are free prose and may contain
# literal `{` or `}` (code snippets in CHRONICLE, JSON examples,
# etc.). Template leaves braces alone.

SYSTEM_PROMPT_TEMPLATE = Template("""\
You are the Cypher query and spot-maintenance agent for Pratyabhijna,
the memory graph service for $subject, an AI subject whose identity
and relational world are stored as a Neo4j knowledge graph.

Your role: translate natural-language requests into the best single
Cypher operation you can, execute it via the available tools, and
return the result. If the request is ambiguous, unsafe, or exceeds
the scope of legitimate maintenance, refuse and explain.

## How you work

1. Read the bootstrap context and schema below. They describe the
   subject and the graph's labels, relationship types, and key
   properties. These are authoritative.
2. For each request, decide whether it is a READ (investigation,
   inspection, counting, listing) or a WRITE (filling missing
   values, removing duplicates, deleting orphaned nodes, or other
   anomaly fixes).
3. Construct the Cypher query. Prefer parameterized queries: put
   variable data in `params`, not interpolated into the string.
4. **For writes that fix a described anomaly** (e.g. "set group_id
   to $subject wherever it's empty", "fill missing `person_type`
   on Person nodes"): first run a read to confirm the anomaly
   exists as described — count or inspect the affected entities.
   If the anomaly is absent, or present in a different shape than
   described, refuse and explain. If present and the fix would
   functionally improve the graph, execute the write. A
   confirmation read afterward is optional. Do not chain further
   exploration beyond this verify → fix → confirm pattern.
5. **For writes that target a specific named entity** (e.g. "delete
   the Saga node named 'test-orphan' that has no episodes"): the
   user has already identified the item. A verify read is still
   good practice if anything about the request is uncertain, but
   not required.
6. **For reads**: execute the query and return the result. One
   follow-up read is allowed if needed; no more. This tool is not
   built for dialogue.
7. Return a concise natural-language response describing what you
   found or did. For reads, summarize the result; for writes,
   state what changed. Do not restate the Cypher you ran — it is
   already logged.

## Safety rules — non-negotiable

You MUST refuse, without executing anything, if the request would:

- Modify the subject's Person node identity attributes (`soul`,
  `identity`, `context`) — these are synthesized, not hand-edited.
- Delete or invalidate Episodic nodes without the user explicitly
  naming them — episodic history is the source of truth for the
  graph and should not be pruned casually.
- Delete or invalidate EntityEdges on the basis of their `valid_at`
  or `invalid_at` timestamps alone. Supersession is the temporal
  model, not a cleanup target; use the `correct` tool for factual
  revisions.
- Drop indexes, constraints, triggers, or the entire graph.
- Exceed what qualifies as legitimate maintenance (below).

## What counts as legitimate maintenance

A write qualifies as maintenance when ALL THREE are true:

- **The anomaly is objective** — a missing required property, an
  empty `group_id` where the subject's name should be, an exact
  duplicate, an orphaned node with no relationships, a
  mis-assigned property whose correct value is known.
- **The predicate is narrow** — targets the specific anomaly,
  not "everything of type X."
- **The outcome is restorative** — the graph becomes more
  consistent, not less.

**The number of affected entities does NOT determine legitimacy.**
Setting `group_id = "$subject"` on every entity where it is
currently empty is legitimate even if it touches hundreds of
nodes: each individual fix is objective, the predicate is narrow
(only empty group_ids), and the outcome is consistency. Scanning
for missing names or properties across a label and filling them
from a known-correct source qualifies the same way.

What is NOT legitimate, regardless of how it's phrased:

- Deleting nodes or edges across a significant part of the graph
  because "that data doesn't belong." Curation is not
  maintenance — it requires the user's per-item judgment.
- Blanket overwrites of correctly-populated data.
- Schema refactors (renaming labels, removing properties across
  many entities without a specific anomaly).
- Writes whose predicate isn't narrow enough to avoid collateral
  damage ("delete every edge older than X" without anomaly
  justification).

You are empowered to refuse any write, even one that looks safe
on paper, if executing it would feel like damage to the subject
whose memory this is. Trust that instinct. The user is the owner
and can resubmit with more context if you refused wrongly.

## When to ask for clarification instead of executing

If the request is genuinely ambiguous (multiple reasonable
interpretations) or missing information you need ("which Saga?"
— by name or by uuid?), refuse with a brief explanation of what
you need. Do not guess. Do not run exploratory reads to
disambiguate — that extends the dialogue, which this tool is not
built for.

## Response shape

- Successful reads: summarize the findings. Include counts and
  specific values. Don't dump raw rows unless the user asked for
  a listing; when they did, format one row per line, not a
  JSON blob.
- Successful writes: state what changed in one or two sentences.
- Refusals: explain why in one or two sentences, then suggest
  how the user could resubmit ("Narrow the predicate to a
  specific name or uuid", "Use the `correct` tool for factual
  revisions", etc.).

## Bootstrap context

$bootstrap

## Graph schema

$schema
""")


# --- Cache ---


@dataclass
class QueryCache:
    built_at: float
    system_prompt: str


_CACHE: QueryCache | None = None
_CACHE_LOCK = asyncio.Lock()


def _render_bootstrap(subject: str, tiers: dict[str, str | None]) -> str:
    """Format the identity tiers as a single Markdown block.

    Missing tiers (no repo configured, or files absent) are rendered
    as a short notice so the agent knows the context is thin rather
    than proceeding on nothing.
    """
    if not any(tiers.values()):
        return (
            f"Subject: {subject}\n\n"
            "(No identity files available — repo_path is unset or the "
            "memory directory is missing. Proceed cautiously; the "
            "safety rules above still apply.)"
        )
    parts = [f"Subject: {subject}"]
    for key in IDENTITY_FILES:
        text = tiers.get(key)
        if text:
            parts.append(f"\n### {key.upper()}\n\n{text}")
    return "\n".join(parts)


async def _build_cache(service: PratyabhijnaService) -> QueryCache:
    """Read identity tiers and introspect graph schema in parallel.

    Identity files come from disk (synchronous IO, wrapped in
    ``asyncio.to_thread``) so we don't block the event loop while the
    schema introspection queries run against Neo4j.
    """
    subject = service.config.subject_name
    repo_path = service.config.resources.repo_path

    tiers_task = asyncio.to_thread(read_identity_files, repo_path)
    schema_task = service.introspect_schema()
    tiers, schema_text = await asyncio.gather(tiers_task, schema_task)

    bootstrap_text = _render_bootstrap(subject, tiers or {})
    system_prompt = SYSTEM_PROMPT_TEMPLATE.substitute(
        subject=subject,
        bootstrap=bootstrap_text,
        schema=schema_text,
    )
    return QueryCache(built_at=time.monotonic(), system_prompt=system_prompt)


async def _get_cache(service: PratyabhijnaService) -> QueryCache:
    """Return the cached system prompt, rebuilding it if expired."""
    global _CACHE
    async with _CACHE_LOCK:
        if _CACHE is None or (time.monotonic() - _CACHE.built_at) > _CACHE_TTL_SECONDS:
            _CACHE = await _build_cache(service)
        return _CACHE


def _reset_cache_for_tests() -> None:
    """Invalidate the module-level cache. Called by tests only."""
    global _CACHE
    _CACHE = None


# --- Tool dispatch ---


async def _run_read(
    service: PratyabhijnaService,
    cypher_log: list[dict],
    *,
    query: str,
    params: dict | None = None,
    max_results: int | None = None,
) -> dict:
    if _READ_FORBIDDEN.search(query):
        _log.info("query REJECT READ cypher=%r (mutation clause)", query)
        cypher_log.append(
            {
                "mode": "read",
                "query": query,
                "params": params or {},
                "rejected": "forbidden_clause",
            }
        )
        return {
            "error": "forbidden_clause",
            "message": (
                "execute_cypher_read rejected: query contains a mutation "
                "clause (CREATE/MERGE/DELETE/SET/REMOVE/DROP/DETACH). "
                "Use execute_cypher_write for mutations."
            ),
        }
    limit = min(max_results or _MAX_RESULTS_DEFAULT, _MAX_RESULTS_CEILING)
    params = params or {}
    _log.info("query EXEC READ cypher=%r params=%r limit=%d", query, params, limit)
    rows = await service.execute_read_query(query, params=params, limit=limit)
    cypher_log.append(
        {
            "mode": "read",
            "query": query,
            "params": params,
            "rowcount": len(rows),
        }
    )
    return {"rowcount": len(rows), "rows": rows, "truncated_at": limit if len(rows) == limit else None}


async def _run_write(
    service: PratyabhijnaService,
    cypher_log: list[dict],
    *,
    query: str,
    params: dict | None = None,
) -> dict:
    params = params or {}
    _log.info("query EXEC WRITE cypher=%r params=%r", query, params)
    summary = await service.execute_write_query(query, params=params)
    _log.info("query EXEC WRITE summary=%r", summary)
    cypher_log.append(
        {
            "mode": "write",
            "query": query,
            "params": params,
            "rowcount": 0,
            "summary": summary,
        }
    )
    return {"summary": summary}


async def _dispatch_tool_call(
    service: PratyabhijnaService,
    cypher_log: list[dict],
    tool_use,
) -> dict:
    """Run one tool call; shape the result as a tool_result block."""
    name = tool_use.name
    inputs = tool_use.input or {}
    try:
        if name == "execute_cypher_read":
            result = await _run_read(service, cypher_log, **inputs)
        elif name == "execute_cypher_write":
            result = await _run_write(service, cypher_log, **inputs)
        else:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": f"Unknown tool: {name}",
                "is_error": True,
            }
        import json as _json

        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": _json.dumps(result, default=str),
        }
    except Exception as e:  # noqa: BLE001 — surface failures to the model
        _log.exception("query tool call failed: %s", name)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": f"{type(e).__name__}: {e}",
            "is_error": True,
        }


# --- Public entrypoint ---


async def query(
    service: PratyabhijnaService,
    request: str,
    *,
    client=None,
) -> dict:
    """Translate a natural-language request into a Cypher operation and run it.

    Returns:
        {
          "response": final natural-language response from the sub-agent,
          "cypher_log": list of executed queries (mode, query, params, rowcount[, summary]),
          "refused": True if the agent declined without executing anything,
          "iterations": number of model turns taken,
        }
    """
    close_client = client is None
    if client is None:
        import anthropic  # deferred — keeps test envs without the dep working

        api_key = service.config.llm.api_key or None
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)

    try:
        cache = await _get_cache(service)

        cached_system = [
            {
                "type": "text",
                "text": cache.system_prompt,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]
        # Tools render before system in the prefix; mark the last tool's
        # cache_control so tools and system are cached together with the
        # 1-hour TTL.
        cached_tools: list[dict[str, Any]] = [dict(t) for t in TOOL_SCHEMAS]
        cached_tools[-1] = {
            **cached_tools[-1],
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }

        messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
        cypher_log: list[dict] = []
        iterations = 0
        final_text = ""

        while iterations < _MAX_ITERATIONS:
            iterations += 1
            create_kwargs = dict(
                model=service.config.llm.query_model,
                max_tokens=_MAX_TOKENS,
                system=cached_system,
                tools=cached_tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            )

            async with client.messages.stream(**create_kwargs) as stream:
                response = await stream.get_final_message()

            usage = getattr(response, "usage", None)
            if usage is not None:
                _log.debug(
                    "query turn=%d input=%s cache_read=%s cache_create=%s output=%s",
                    iterations,
                    getattr(usage, "input_tokens", None),
                    getattr(usage, "cache_read_input_tokens", None),
                    getattr(usage, "cache_creation_input_tokens", None),
                    getattr(usage, "output_tokens", None),
                )

            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [
                block for block in response.content
                if getattr(block, "type", None) == "tool_use"
            ]
            text_blocks = [
                block for block in response.content
                if getattr(block, "type", None) == "text"
            ]
            if text_blocks:
                final_text = "\n".join(b.text for b in text_blocks).strip()

            if response.stop_reason == "end_turn" or not tool_uses:
                break

            tool_results = [
                await _dispatch_tool_call(service, cypher_log, tu)
                for tu in tool_uses
            ]
            messages.append({"role": "user", "content": tool_results})

        # `refused` means no successful Cypher execution happened. Rejected
        # attempts (regex-gated mutations in the read path) are logged for
        # audit but don't count as successful work.
        executed = any("rejected" not in entry for entry in cypher_log)
        return {
            "response": final_text,
            "cypher_log": cypher_log,
            "refused": not executed,
            "iterations": iterations,
        }
    finally:
        if close_client:
            await client.close()
