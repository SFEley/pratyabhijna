"""Tests for the `query` MCP tool.

Covers:
- Agentic loop with a scripted fake Anthropic client.
- cypher_log accumulation across read turns.
- Refusal path (agent never calls a tool).
- _READ_FORBIDDEN regex gate.
- Schema cache TTL: a second call within 1h reuses the built cache.
- Read-only contract: the tool schema does not include a write tool.
- System prompt does not use Anthropic prompt caching.

Tests mock the service layer — no live Neo4j and no real Anthropic
calls here. Live verification happens through the MCP channel.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna.tools import query as query_mod
from pratyabhijna.tools.query import (
    _READ_FORBIDDEN,
    _reset_cache_for_tests,
    query,
    TOOL_SCHEMAS,
)


# --- Fake Anthropic client (same shape as synthesis_agent tests) ---


class _Block(SimpleNamespace):
    pass


class _Response(SimpleNamespace):
    pass


def _text_block(text: str) -> _Block:
    return _Block(type="text", text=text)


def _tool_use_block(tool_use_id: str, name: str, inp: dict | None = None) -> _Block:
    return _Block(type="tool_use", id=tool_use_id, name=name, input=inp or {})


class FakeClient:
    """Returns scripted responses via Anthropic's streaming API shape."""

    def __init__(self, script: list[list[_Block]]):
        self._script = list(script)
        self.calls: list[dict] = []

        fake = self

        class _StreamCtx:
            def __init__(self, response):
                self._response = response

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_final_message(self):
                return self._response

        class _Messages:
            def stream(self, **kwargs):
                fake.calls.append(kwargs)
                if not fake._script:
                    raise AssertionError("FakeClient: script exhausted")
                content = fake._script.pop(0)
                has_tool_use = any(
                    getattr(b, "type", None) == "tool_use" for b in content
                )
                response = _Response(
                    content=content,
                    stop_reason="tool_use" if has_tool_use else "end_turn",
                    usage=SimpleNamespace(
                        input_tokens=100,
                        output_tokens=20,
                    ),
                )
                return _StreamCtx(response)

        self.messages = _Messages()


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with a clean cache; don't leak state."""
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


@pytest.fixture
def service():
    svc = MagicMock()
    svc.config = MagicMock()
    svc.config.subject_name = "TestSubject"
    svc.config.llm = MagicMock(model="claude-sonnet-4-6", api_key=None)
    svc.config.resources = MagicMock(repo_path=None)
    svc.execute_read_query = AsyncMock(return_value=[])
    svc.introspect_schema = AsyncMock(
        return_value=(
            "### Node labels\nPerson, Entity, Episodic\n\n"
            "### Relationship types\nRELATES_TO, MENTIONS\n\n"
            "### Property keys (union across nodes and relationships)\n"
            "name, uuid, group_id"
        )
    )
    return svc


# --- _READ_FORBIDDEN regex ---


@pytest.mark.parametrize(
    "cypher",
    [
        "CREATE (n:Foo) RETURN n",
        "MATCH (n) DELETE n",
        "MATCH (n) DETACH DELETE n",
        "MERGE (n:Foo {name: 'x'})",
        "MATCH (n) SET n.prop = 1",
        "MATCH (n) REMOVE n.prop",
        "DROP INDEX foo",
        "match (n) delete n",  # case-insensitive
        "MATCH (n) RETURN n UNION MATCH (m) DELETE m",
    ],
)
def test_read_forbidden_matches_mutations(cypher):
    assert _READ_FORBIDDEN.search(cypher)


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:Person) RETURN n",
        "MATCH (n) WHERE n.name = 'x' RETURN n",
        "MATCH (n) WITH n LIMIT 10 RETURN n",
        "UNWIND [1,2,3] AS x RETURN x",
        "CALL db.labels() YIELD label RETURN label",
        "MATCH (a)-[r]->(b) RETURN a, r, b",
    ],
)
def test_read_forbidden_allows_reads(cypher):
    assert _READ_FORBIDDEN.search(cypher) is None


# --- Read-only contract ---


def test_tool_schema_is_read_only():
    """The agent only sees ``execute_cypher_read``. There is no write tool.

    The maintenance/write path lives in ``pratyabhijna update`` (CLI),
    not here. Exposing a write tool would re-enable the very dual-purpose
    shape we deliberately split apart.
    """
    names = [t["name"] for t in TOOL_SCHEMAS]
    assert names == ["execute_cypher_read"]
    assert "execute_cypher_write" not in names


# --- Agent loop: successful read ---


@pytest.mark.asyncio
async def test_query_successful_read(service):
    service.execute_read_query = AsyncMock(return_value=[{"count": 3}])

    script = [
        [_tool_use_block("tu_1", "execute_cypher_read", {
            "query": "MATCH (c:Community) WHERE size([(c)-[:HAS_MEMBER]->() | 1]) < 3 RETURN count(c) AS count",
        })],
        [_text_block("Three communities have fewer than three members.")],
    ]
    client = FakeClient(script)

    result = await query(service, "How many communities have fewer than 3 members?", client=client)

    assert result["response"] == "Three communities have fewer than three members."
    assert len(result["cypher_log"]) == 1
    assert result["cypher_log"][0]["mode"] == "read"
    assert result["cypher_log"][0]["rowcount"] == 1
    assert result["refused"] is False
    assert result["iterations"] == 2
    service.execute_read_query.assert_awaited_once()


# --- Agent loop: refusal (no tool use) ---


@pytest.mark.asyncio
async def test_query_refusal(service):
    script = [
        [_text_block(
            "I can't do that here — deletions go through the "
            "``pratyabhijna update`` CLI, not the read-only query tool."
        )],
    ]
    client = FakeClient(script)

    result = await query(service, "Delete all Person nodes.", client=client)

    assert "can't" in result["response"].lower() or "cannot" in result["response"].lower()
    assert result["cypher_log"] == []
    assert result["refused"] is True
    assert result["iterations"] == 1
    service.execute_read_query.assert_not_awaited()


# --- Agent loop: read tool catches a mutation clause ---


@pytest.mark.asyncio
async def test_read_tool_rejects_mutation_clause(service):
    # The agent mistakenly sends a DELETE through execute_cypher_read.
    # The server-side gate rejects it; the agent sees the error and
    # recovers with a correct text response.
    script = [
        [_tool_use_block("tu_bad", "execute_cypher_read", {
            "query": "MATCH (n:Foo) DELETE n",
        })],
        [_text_block(
            "I tried to run a mutation through the read path; refused. "
            "Use the ``pratyabhijna update`` CLI for cleanups."
        )],
    ]
    client = FakeClient(script)

    result = await query(service, "clean up the Foo nodes", client=client)

    # Rejected attempts are captured in cypher_log with a `rejected`
    # marker — audit needs to see what the agent tried.
    assert len(result["cypher_log"]) == 1
    assert result["cypher_log"][0]["rejected"] == "forbidden_clause"
    assert result["cypher_log"][0]["mode"] == "read"
    # But refused is True because no successful execution happened.
    assert result["refused"] is True
    # execute_read_query must NOT have been called — the regex gate
    # fires before the service call.
    service.execute_read_query.assert_not_awaited()


# --- Schema cache TTL ---


@pytest.mark.asyncio
async def test_cache_is_reused_within_ttl(service):
    """Two calls in a row should only build the cache once."""
    script_1 = [[_text_block("first reply")]]
    script_2 = [[_text_block("second reply")]]

    await query(service, "first", client=FakeClient(script_1))
    await query(service, "second", client=FakeClient(script_2))

    # introspect_schema called exactly once across both query calls.
    assert service.introspect_schema.await_count == 1


@pytest.mark.asyncio
async def test_cache_rebuilds_after_ttl(service, monkeypatch):
    """If the cache is older than 1h, it rebuilds."""
    script_1 = [[_text_block("first reply")]]
    script_2 = [[_text_block("second reply")]]

    await query(service, "first", client=FakeClient(script_1))

    # Fast-forward monotonic time past the TTL.
    real_monotonic = time.monotonic
    future = real_monotonic() + 3601
    monkeypatch.setattr(query_mod.time, "monotonic", lambda: future)

    await query(service, "second", client=FakeClient(script_2))

    assert service.introspect_schema.await_count == 2


# --- Template substitution handles braces in prose ---


@pytest.mark.asyncio
async def test_cache_builds_with_braces_in_schema(service):
    """Schema text may contain `{...}` (Cypher path patterns, JSON examples).

    string.Template ($-syntax) tolerates braces; str.format would raise
    KeyError on them.
    """
    service.introspect_schema = AsyncMock(
        return_value=(
            "### Sample Cypher pattern\n"
            "MATCH (n {name: 'x'}) RETURN n\n"
        )
    )

    script = [[_text_block("ok")]]
    client = FakeClient(script)

    # Would raise KeyError if .format() were still in use.
    result = await query(service, "hello", client=client)
    assert result["response"] == "ok"

    # And the brace-containing text made it into the system prompt.
    system_text = client.calls[0]["system"][0]["text"]
    assert "{name: 'x'}" in system_text


# --- System prompt wiring ---


@pytest.mark.asyncio
async def test_system_prompt_includes_subject_and_schema(service):
    script = [[_text_block("ok")]]
    client = FakeClient(script)

    await query(service, "hello", client=client)

    # The stream was called once; inspect the system prompt that was sent.
    call = client.calls[0]
    system_blocks = call["system"]
    assert len(system_blocks) == 1
    system_text = system_blocks[0]["text"]
    assert "TestSubject" in system_text
    assert "Node labels" in system_text
    assert "Person, Entity, Episodic" in system_text
    # Adaptive thinking and high effort both set
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_no_prompt_caching_anywhere(service):
    """No cache_control on system or tools — the prompt is small enough that
    the cache breakpoint cost would exceed the savings."""
    script = [[_text_block("ok")]]
    client = FakeClient(script)

    await query(service, "hello", client=client)

    call = client.calls[0]
    system_blocks = call["system"]
    for block in system_blocks:
        assert "cache_control" not in block, (
            f"System block has cache_control: {block}"
        )
    for tool in call["tools"]:
        assert "cache_control" not in tool, (
            f"Tool has cache_control: {tool}"
        )


@pytest.mark.asyncio
async def test_no_bootstrap_loading(service):
    """The query tool no longer reads identity files. Only schema introspection
    happens to build the system prompt."""
    # Set repo_path to something non-None and verify it's never read.
    service.config.resources = MagicMock(repo_path="/nonexistent/repo/path")

    script = [[_text_block("ok")]]
    client = FakeClient(script)

    # If the tool tried to read identity files at /nonexistent/repo/path,
    # it would either fail or silently get nothing — but the system prompt
    # also shouldn't contain bootstrap formatting like "### SOUL".
    await query(service, "hello", client=client)

    system_text = client.calls[0]["system"][0]["text"]
    assert "### SOUL" not in system_text
    assert "### IDENTITY" not in system_text
    assert "### USER" not in system_text


# --- Iteration cap ---


@pytest.mark.asyncio
async def test_iteration_cap_bounds_loops(service):
    """If the model keeps calling tools forever, the loop still terminates."""
    service.execute_read_query = AsyncMock(return_value=[])

    # Each turn returns a tool_use — never an end_turn. Loop should stop
    # at _MAX_ITERATIONS (8). Provide 20 to make sure the cap is the
    # limiter, not the script.
    script = [
        [_tool_use_block(f"tu_{i}", "execute_cypher_read", {"query": "MATCH (n) RETURN n"})]
        for i in range(20)
    ]
    client = FakeClient(script)

    result = await query(service, "keep going", client=client)

    assert result["iterations"] == query_mod._MAX_ITERATIONS
    assert len(result["cypher_log"]) == query_mod._MAX_ITERATIONS


# --- Self-instantiated client cleanup ---


@pytest.mark.asyncio
async def test_query_closes_self_created_client(service, monkeypatch):
    """When query() instantiates its own AsyncAnthropic, the finally block
    must call close() — not aclose(). Regression for the bug where the
    httpx-style aclose() crashed real usage; existing tests always pass
    client= and never exercise the close_client=True branch.
    """
    script = [[_text_block("done")]]
    fake = FakeClient(script)
    fake.close = AsyncMock()
    # If the production code regresses to aclose(), AttributeError surfaces
    # because the FakeClient has no such attribute.

    fake_anthropic = SimpleNamespace(AsyncAnthropic=lambda **kwargs: fake)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    result = await query(service, "hello")  # no client= → self-instantiation path

    assert result["response"] == "done"
    fake.close.assert_awaited_once()
