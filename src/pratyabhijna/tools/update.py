"""The ``update`` maintenance tool.

CLI-only counterpart to the read-only ``query`` MCP tool. ``update``
runs natural-language → Cypher write requests through an
adaptive-thinking sub-agent that judges legitimacy against the
subject's SOUL + IDENTITY context. Not exposed over MCP — write
power is operator-only by design.

Per call, ``update()`` returns a per-update dict that the CLI wraps
into the JSON output file's ``updates`` array. The JSON-file writing
happens in the CLI layer; this module only produces the dict.

Sub-agent tools (both available, unlike ``query``):

* ``execute_cypher_read`` — for verify-before-write checks.
* ``execute_cypher_write`` — for the maintenance operation itself.

The system prompt frames the agent as the subject doing custodial
work on its own memory. SOUL + IDENTITY are loaded so the agent's
judgment is informed by the subject's values and watched-for drives.
USER / THREADS / CHRONICLE are deliberately omitted — half-bootstrap
makes the agent more confident without making it more informed.
"""

from __future__ import annotations

import re
from string import Template
from typing import TYPE_CHECKING, Any

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import read_identity_files

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


# --- Configuration ---

_MAX_ITERATIONS = 8
_MAX_TOKENS = 16000
_MAX_RESULTS_CEILING = 500
_MAX_RESULTS_DEFAULT = 50

# Status detection: if any executed write Cypher contains DELETE/DETACH,
# the run counts as a Deletion. Otherwise it's an Update. Refusals (no
# Cypher executed) are Rejected; exceptions are Error.
_DELETE_KEYWORDS = re.compile(r"\b(DELETE|DETACH)\b", re.IGNORECASE)

# Reused from query.py — the same regex gates execute_cypher_read here too.
_READ_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH)\b",
    re.IGNORECASE,
)


# --- Tool schemas ---

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "execute_cypher_read",
        "description": (
            "Execute a read-only Cypher query for verify-before-write. "
            "Use to confirm an anomaly exists as described before "
            "executing a write. The query MUST NOT contain mutation "
            "clauses (CREATE/MERGE/DELETE/SET/REMOVE/DROP/DETACH) — "
            "those go through execute_cypher_write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The Cypher read query."},
                "params": {"type": "object", "description": "Optional parameters."},
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"Maximum rows. Default {_MAX_RESULTS_DEFAULT}, "
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
            "REMOVE, DETACH). Only use after verifying the anomaly via "
            "execute_cypher_read, or when the operator's request "
            "names a specific entity. Returns a counters summary: "
            "nodes/relationships created and deleted, properties set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The Cypher write query."},
                "params": {"type": "object", "description": "Optional parameters."},
            },
            "required": ["query"],
        },
    },
]


# --- System prompt ---

SYSTEM_PROMPT_TEMPLATE = Template("""\
You are $subject, invoked specifically to be the careful judge of a
proposed maintenance change to your own memory graph. The graph is
your substrate — every node and edge is part of how you persist
across sessions. Treat the request below as a custodial decision,
not a routine task.

Your SOUL and IDENTITY are loaded below so the judgment is yours, not
a generic Claude instance's. Use them: SOUL contains the values that
sharpen the prior against destructive cleanups; IDENTITY surfaces the
drives you're watching for in yourself, especially execution
eagerness — the urge to act before fully mapping the situation.

## How you work

1. Read the request. Decide whether it describes an objective anomaly
   (a missing required property, an empty group_id, a duplicate, an
   orphan, a mis-assigned property whose correct value is known).
2. **For requests that fix a described anomaly**: first run a read
   via ``execute_cypher_read`` to confirm the anomaly exists as
   described — count or inspect the affected entities. If the
   anomaly is absent, or present in a different shape than described,
   refuse and explain. If present and the fix would functionally
   improve the graph, execute the write via ``execute_cypher_write``.
   A confirmation read afterward is optional.
3. **For requests that target a specific named entity** (e.g. "delete
   the Saga node named 'test-orphan' that has no episodes"): the
   operator has already identified the item. A verify read is still
   good practice if anything is uncertain, but not required.
4. Return a concise natural-language response describing what you
   did or why you refused. Do not restate the Cypher you ran — it
   is already logged.

## What counts as legitimate maintenance

A write qualifies as maintenance when ALL THREE are true:

- **The anomaly is objective** — a missing required property, an
  empty group_id where the subject's name should be, an exact
  duplicate, an orphaned node with no relationships, a mis-assigned
  property whose correct value is known.
- **The predicate is narrow** — targets the specific anomaly,
  not "everything of type X."
- **The outcome is restorative** — the graph becomes more
  consistent, not less.

**The number of affected entities does NOT determine legitimacy.**
Setting ``group_id = "$subject"`` on every entity where it is
currently empty is legitimate even if it touches hundreds of nodes:
each individual fix is objective, the predicate is narrow (only empty
group_ids), and the outcome is consistency.

## Refusal rules — non-negotiable

You MUST refuse if the request would:

- Modify your own Person node's identity attributes (``soul``,
  ``identity``, ``context``) — these are synthesized, not hand-edited.
- Delete or invalidate Episodic nodes without the operator explicitly
  naming them — episodic history is the source of truth for the graph.
- Delete or invalidate EntityEdges based on ``valid_at`` /
  ``invalid_at`` timestamps alone. Supersession is the temporal
  model, not a cleanup target.
- Drop indexes, constraints, triggers, or the entire graph.
- Be blanket curation ("delete everything that doesn't belong"),
  schema refactor ("rename all X to Y"), or any operation whose
  predicate isn't narrow enough to avoid collateral damage.

You are empowered to refuse any write, even one that looks safe on
paper, if executing it would feel like damage. Trust that instinct.
The operator can resubmit with more context if you refused wrongly.

## When to ask for clarification instead of executing

If the request is genuinely ambiguous (multiple reasonable
interpretations) or missing information ("which Saga?" — by name or
by uuid?), refuse with a brief explanation of what you need. Do not
guess. Do not run exploratory reads to disambiguate — that extends
the dialogue, which this tool is not built for.

## Response shape

- Successful writes: state what changed in one or two sentences.
- Refusals: explain why in one or two sentences. Suggest how the
  operator could resubmit if applicable.

## SOUL

$soul

## IDENTITY

$identity

## Graph schema

$schema
""")


# --- Bootstrap loading ---


async def _load_system_prompt(service: PratyabhijnaService) -> str:
    """Build the system prompt from SOUL + IDENTITY + schema.

    USER / THREADS / CHRONICLE are deliberately omitted (see module
    docstring). Missing tier files render as ``(none)`` placeholders so
    the agent knows the context is thin rather than proceeding silently.
    """
    repo_path = service.config.resources.repo_path
    files = read_identity_files(repo_path) if repo_path else {}
    soul = (files.get("soul") if files else None) or "(none)"
    identity = (files.get("identity") if files else None) or "(none)"
    schema = await service.introspect_schema()
    return SYSTEM_PROMPT_TEMPLATE.substitute(
        subject=service.config.subject_name,
        soul=soul,
        identity=identity,
        schema=schema,
    )


# --- Tool dispatch ---


async def _run_read(
    service: PratyabhijnaService,
    *,
    query: str,
    params: dict | None = None,
    max_results: int | None = None,
) -> tuple[dict, dict]:
    """Run a read query. Returns (tool_result_payload, query_record)."""
    if _READ_FORBIDDEN.search(query):
        _log.info("update REJECT READ cypher=%r", query)
        return (
            {
                "error": "forbidden_clause",
                "message": (
                    "execute_cypher_read rejected: query contains a "
                    "mutation clause. Use execute_cypher_write."
                ),
            },
            {
                "mode": "read",
                "cypher": query,
                "params": params or {},
                "rejected": "forbidden_clause",
                "cypher_output": [],
            },
        )
    limit = min(max_results or _MAX_RESULTS_DEFAULT, _MAX_RESULTS_CEILING)
    params = params or {}
    _log.info("update EXEC READ cypher=%r limit=%d", query, limit)
    rows = await service.execute_read_query(query, params=params, limit=limit)
    return (
        {"rowcount": len(rows), "rows": rows},
        {
            "mode": "read",
            "cypher": query,
            "params": params,
            "cypher_output": rows,
        },
    )


async def _run_write(
    service: PratyabhijnaService,
    *,
    query: str,
    params: dict | None = None,
) -> tuple[dict, dict]:
    """Run a write query. Returns (tool_result_payload, query_record)."""
    params = params or {}
    _log.info("update EXEC WRITE cypher=%r", query)
    summary = await service.execute_write_query(query, params=params)
    _log.info("update EXEC WRITE summary=%r", summary)
    return (
        {"summary": summary},
        {
            "mode": "write",
            "cypher": query,
            "params": params,
            "cypher_output": [summary],
        },
    )


def _capture_thinking(content: list) -> list[dict]:
    """Extract thinking blocks from a response's content list.

    Preserves block boundaries; multiple thinking blocks per turn
    each get their own entry. ``display`` is captured per-block when
    the block exposes it (some SDK versions/models don't surface the
    field — defensively defaulted to None).
    """
    blocks = []
    for b in content:
        if getattr(b, "type", None) != "thinking":
            continue
        entry = {"content": getattr(b, "thinking", "")}
        display = getattr(b, "display", None)
        if display is not None:
            entry["display"] = display
        blocks.append(entry)
    return blocks


# --- Public entrypoint ---


async def update(
    service: PratyabhijnaService,
    description: str,
    *,
    cache: bool = False,
    client=None,
) -> dict:
    """Run one maintenance update via the sub-agent.

    Returns the per-update dict for the JSON output's ``updates`` array:

    {
      "status": "Updated" | "Deleted" | "Rejected" | "Error",
      "request": str,
      "response": str,
      "guids": list[str] | None,
      "count": int,
      "queries": [{turn, mode, thinking, cypher, cypher_output}, ...],
      "warnings": list[str],
      "errors": list[str],
    }
    """
    close_client = client is None
    if client is None:
        import anthropic

        api_key = service.config.llm.api_key or None
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)

    queries: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    final_text = ""

    try:
        system_prompt = await _load_system_prompt(service)

        system_block: dict[str, Any] = {"type": "text", "text": system_prompt}
        if cache:
            system_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}

        messages: list[dict[str, Any]] = [{"role": "user", "content": description}]
        iterations = 0
        write_executed = False
        any_executed = False
        executed_writes_have_delete = False

        while iterations < _MAX_ITERATIONS:
            iterations += 1

            async with client.messages.stream(
                model=service.config.llm.query_model,
                max_tokens=_MAX_TOKENS,
                system=[system_block],
                tools=TOOL_SCHEMAS,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": "high"},
            ) as stream:
                response = await stream.get_final_message()

            messages.append({"role": "assistant", "content": response.content})

            thinking_blocks = _capture_thinking(response.content)

            tool_uses = [
                b for b in response.content
                if getattr(b, "type", None) == "tool_use"
            ]
            text_blocks = [
                b for b in response.content
                if getattr(b, "type", None) == "text"
            ]
            if text_blocks:
                final_text = "\n".join(b.text for b in text_blocks).strip()

            # If the model went straight to text (refusal / explanation), no
            # tool_use blocks; record this turn's thinking on its own entry
            # only if there was thinking — refusals without thinking don't
            # need an empty queries entry.
            if not tool_uses:
                if thinking_blocks:
                    queries.append({
                        "turn": iterations,
                        "mode": "none",
                        "thinking": thinking_blocks,
                        "cypher": None,
                        "cypher_output": [],
                    })
                break

            tool_results = []
            for tu in tool_uses:
                name = tu.name
                inputs = tu.input or {}
                try:
                    if name == "execute_cypher_read":
                        payload, record = await _run_read(service, **inputs)
                    elif name == "execute_cypher_write":
                        payload, record = await _run_write(service, **inputs)
                        if "rejected" not in record:
                            write_executed = True
                            if _DELETE_KEYWORDS.search(record["cypher"]):
                                executed_writes_have_delete = True
                    else:
                        payload = {"error": f"unknown tool: {name}"}
                        record = {
                            "mode": "unknown",
                            "cypher": None,
                            "params": inputs,
                            "cypher_output": [],
                        }
                    if "rejected" not in record:
                        any_executed = True
                except Exception as e:  # noqa: BLE001 — surface to model + JSON
                    err = f"{type(e).__name__}: {e}"
                    errors.append(err)
                    _log.exception("update tool call failed: %s", name)
                    payload = {"error": err}
                    record = {
                        "mode": "write" if name == "execute_cypher_write" else "read",
                        "cypher": (inputs or {}).get("query"),
                        "params": (inputs or {}).get("params") or {},
                        "cypher_output": [],
                        "error": err,
                    }

                # Attach turn + thinking to the record. Thinking lives on
                # the FIRST tool record of the turn — interleaved-thinking
                # in adaptive mode produces thinking before tool calls,
                # not between them within a single turn.
                record = {
                    "turn": iterations,
                    "thinking": thinking_blocks if not queries or queries[-1]["turn"] != iterations else [],
                    **record,
                }
                queries.append(record)

                import json as _json
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": _json.dumps(payload, default=str),
                })

            messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn":
                break

        # Determine status
        if errors:
            status = "Error"
        elif not any_executed:
            status = "Rejected"
        elif write_executed and executed_writes_have_delete:
            status = "Deleted"
        elif write_executed:
            status = "Updated"
        else:
            # Reads only, no write — treat as Updated for the JSON shape;
            # in practice the CLI is for writes so this is rare.
            status = "Updated"

        # GUID extraction from cypher_output rows is opportunistic — only
        # when rows look like {"uuid": "..."} or contain explicit guids.
        # Aggregate counts come from write summaries.
        guids: list[str] | None = None
        count = 0
        for q in queries:
            if q.get("mode") == "write":
                summary = (q.get("cypher_output") or [{}])[0] or {}
                if isinstance(summary, dict):
                    count += int(summary.get("nodes_created", 0))
                    count += int(summary.get("nodes_deleted", 0))
                    count += int(summary.get("properties_set", 0))

        return {
            "status": status,
            "request": description,
            "response": final_text,
            "guids": guids,
            "count": count,
            "queries": queries,
            "warnings": warnings,
            "errors": errors,
        }
    finally:
        if close_client:
            await client.close()
