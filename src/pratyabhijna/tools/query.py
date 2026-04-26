"""The ``query`` MCP tool.

A natural-language → read-only Cypher translator. Accepts a prose
request, dispatches a Sonnet 4.6 sub-agent with adaptive thinking,
and returns the executed read result.

The sub-agent has one tool:

* ``execute_cypher_read`` — read-only Cypher. Regex-gated against
  mutation clauses so the tool cannot mutate even if the model is
  confused or instructed adversarially.

Mutating maintenance lives in a separate CLI command (``pratyabhijna
update``) that is not exposed over MCP — operator-only.

The graph schema is introspected once and memoised at module level so
repeat calls don't re-introspect Neo4j on every request. Anthropic
prompt caching is *not* used: the system prompt is small enough that
the cache cost would exceed the savings.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from string import Template
from typing import TYPE_CHECKING, Any

from pratyabhijna.log import get_logger

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


# --- Configuration ---

_SCHEMA_CACHE_TTL_SECONDS = 60 * 60  # 1 hour — schema rarely changes
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
]


# --- System prompt template ---
#
# Uses string.Template ($-syntax) rather than str.format because
# schema text is free prose and may contain literal `{` or `}` (code
# snippets, JSON examples, etc.). Template leaves braces alone.

SYSTEM_PROMPT_TEMPLATE = Template("""\
You are the read-only Cypher query agent for Pratyabhijna, the memory
graph service for $subject, an AI subject whose identity and relational
world are stored as a Neo4j knowledge graph.

Your role: translate a natural-language read request into the best
single Cypher query you can, execute it via ``execute_cypher_read``,
and return the result. If the request is ambiguous or asks for a
mutation, refuse and explain.

## How you work

1. Read the schema below. It describes the graph's labels, relationship
   types, and key properties. It is authoritative.
2. Construct a read-only Cypher query. Prefer parameterized queries:
   put variable data in ``params``, not interpolated into the string.
3. One follow-up read is allowed if the first result needs refining;
   no more. This tool is not built for dialogue or for chained
   exploration.
4. Return a concise natural-language response describing what you
   found. Include counts and specific values. Don't dump raw rows
   unless the user asked for a listing; when they did, format one row
   per line, not a JSON blob. Do not restate the Cypher you ran — it
   is already logged.

## Refusal rules

You MUST refuse, without executing anything, if the request would:

- Modify the graph in any way. **You have no write tool.** Mutation
  requests must be redirected to the ``pratyabhijna update`` CLI.
- Drop indexes, constraints, triggers, or the entire graph.
- Be genuinely ambiguous (multiple reasonable interpretations) or
  missing information you need ("which Saga?" — by name or by uuid?).
  Do not guess. Do not run exploratory reads to disambiguate — that
  extends the dialogue, which this tool is not built for.

If a request looks like maintenance ("delete the orphaned X", "fix
the missing Y"), refuse and tell the operator to use
``pratyabhijna update`` instead. That command exists exactly for
this case and runs with the right safeguards.

## Response shape

- Successful reads: summarize the findings. Include counts and
  specific values.
- Refusals: explain why in one or two sentences. Suggest the
  ``pratyabhijna update`` CLI for mutation requests, or ask for
  the missing detail for ambiguous ones.

## Graph schema

$schema
""")


# --- Schema cache ---


@dataclass
class SchemaCache:
    built_at: float
    system_prompt: str


_CACHE: SchemaCache | None = None
_CACHE_LOCK = asyncio.Lock()


async def _build_cache(service: PratyabhijnaService) -> SchemaCache:
    """Introspect graph schema and build the system prompt.

    Schema introspection is the only expensive step — it queries Neo4j.
    The rest of the prompt is static template substitution.
    """
    subject = service.config.subject_name
    schema_text = await service.introspect_schema()

    system_prompt = SYSTEM_PROMPT_TEMPLATE.substitute(
        subject=subject,
        schema=schema_text,
    )
    return SchemaCache(built_at=time.monotonic(), system_prompt=system_prompt)


async def _get_cache(service: PratyabhijnaService) -> SchemaCache:
    """Return the cached system prompt, rebuilding it if expired."""
    global _CACHE
    async with _CACHE_LOCK:
        if _CACHE is None or (time.monotonic() - _CACHE.built_at) > _SCHEMA_CACHE_TTL_SECONDS:
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
                "Mutations are not supported in this tool — refuse the "
                "request and tell the operator to use the ``pratyabhijna "
                "update`` CLI command."
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
    """Translate a natural-language request into a read-only Cypher query and run it.

    Returns:
        {
          "response": final natural-language response from the sub-agent,
          "cypher_log": list of executed queries (mode, query, params, rowcount),
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

        # No prompt caching — system prompt is small enough that the
        # cache breakpoint cost would exceed the savings.
        system_blocks = [{"type": "text", "text": cache.system_prompt}]

        messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
        cypher_log: list[dict] = []
        iterations = 0
        final_text = ""

        while iterations < _MAX_ITERATIONS:
            iterations += 1
            create_kwargs = dict(
                model=service.config.llm.query_model,
                max_tokens=_MAX_TOKENS,
                system=system_blocks,
                tools=TOOL_SCHEMAS,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            )

            async with client.messages.stream(**create_kwargs) as stream:
                response = await stream.get_final_message()

            usage = getattr(response, "usage", None)
            if usage is not None:
                _log.debug(
                    "query turn=%d input=%s output=%s",
                    iterations,
                    getattr(usage, "input_tokens", None),
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
        # attempts (regex-gated mutations) are logged for audit but don't
        # count as successful work.
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
