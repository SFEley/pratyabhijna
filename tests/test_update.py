"""Tests for the `update` maintenance CLI tool.

`update` is the write-side counterpart to the read-only `query` MCP tool.
It is invoked from the CLI (`pratyabhijna update "description"`), not
exposed over MCP — write power is operator-only.

Per call, `update` returns a per-update dict that the CLI wraps into
the JSON output file's `updates` array.

Tests mock the service layer and Anthropic client. Live verification
happens through the CLI in real operator usage.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- Fake Anthropic client (same shape as test_query.py) ---


class _Block(SimpleNamespace):
    pass


class _Response(SimpleNamespace):
    pass


def _text_block(text: str) -> _Block:
    return _Block(type="text", text=text)


def _thinking_block(content: str, display: str | None = None) -> _Block:
    blk = _Block(type="thinking", thinking=content)
    if display is not None:
        blk.display = display
    return blk


def _tool_use_block(tool_use_id: str, name: str, inp: dict | None = None) -> _Block:
    return _Block(type="tool_use", id=tool_use_id, name=name, input=inp or {})


class FakeClient:
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
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=50,
                        output_tokens=20,
                    ),
                )
                return _StreamCtx(response)

        self.messages = _Messages()


# --- Fixtures ---


@pytest.fixture
def service(tmp_path):
    """A mock service with a tiny on-disk repo for SOUL/IDENTITY loading."""
    repo = tmp_path / "vesper"
    memory = repo / "memory"
    memory.mkdir(parents=True)
    (memory / "SOUL.md").write_text("# SOUL\n\nI am the subject.\n")
    (memory / "IDENTITY.md").write_text("# IDENTITY\n\nDrives I'm watching for...\n")
    # USER/THREADS/CHRONICLE present but should not be loaded for update
    (memory / "USER.md").write_text("# USER (should not be loaded)")
    (memory / "THREADS.md").write_text("# THREADS (should not be loaded)")
    (memory / "CHRONICLE.md").write_text("# CHRONICLE (should not be loaded)")

    svc = MagicMock()
    svc.config = MagicMock()
    svc.config.subject_name = "TestSubject"
    svc.config.llm = MagicMock(query_model="claude-sonnet-4-6", api_key=None)
    svc.config.resources = MagicMock(repo_path=str(repo))
    svc.execute_read_query = AsyncMock(return_value=[])
    svc.execute_write_query = AsyncMock(return_value={})
    svc.introspect_schema = AsyncMock(
        return_value="### Node labels\nPerson, Entity\n"
    )
    return svc


# --- Per-update return shape ---


class TestUpdateReturnShape:
    @pytest.mark.asyncio
    async def test_successful_write_returns_updated_status(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={
            "nodes_created": 0, "properties_set": 5,
        })
        script = [
            [_tool_use_block("tu_w", "execute_cypher_write", {
                "query": "MATCH (n:Foo) WHERE n.bar IS NULL SET n.bar = 'x'",
            })],
            [_text_block("Filled missing bar property on 5 Foo nodes.")],
        ]
        result = await update(
            service, "Fill missing bar on Foo nodes", client=FakeClient(script),
        )
        assert result["status"] == "Updated"

    @pytest.mark.asyncio
    async def test_successful_delete_returns_deleted_status(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={"nodes_deleted": 1})
        script = [
            [_tool_use_block("tu_d", "execute_cypher_write", {
                "query": "MATCH (s:Saga {name: 'orphan'}) DELETE s",
            })],
            [_text_block("Deleted orphan Saga.")],
        ]
        result = await update(
            service, "Delete the orphan Saga", client=FakeClient(script),
        )
        assert result["status"] == "Deleted"

    @pytest.mark.asyncio
    async def test_refusal_returns_rejected_status(self, service):
        from pratyabhijna.tools.update import update

        script = [
            [_text_block("That looks like blanket curation, not maintenance. Refused.")],
        ]
        result = await update(
            service, "Drop everything", client=FakeClient(script),
        )
        assert result["status"] == "Rejected"
        assert "curation" in result["response"].lower()

    @pytest.mark.asyncio
    async def test_returns_request_and_response_text(self, service):
        from pratyabhijna.tools.update import update

        script = [[_text_block("Refused: too vague.")]]
        result = await update(
            service, "Just clean it up", client=FakeClient(script),
        )
        assert result["request"] == "Just clean it up"
        assert result["response"] == "Refused: too vague."

    @pytest.mark.asyncio
    async def test_empty_warnings_and_errors_by_default(self, service):
        from pratyabhijna.tools.update import update

        script = [[_text_block("ok refused")]]
        result = await update(service, "x", client=FakeClient(script))
        assert result["warnings"] == []
        assert result["errors"] == []


# --- Per-turn queries array ---


class TestQueriesArray:
    @pytest.mark.asyncio
    async def test_each_turn_recorded_with_turn_number(self, service):
        from pratyabhijna.tools.update import update

        service.execute_read_query = AsyncMock(return_value=[{"count": 5}])
        service.execute_write_query = AsyncMock(return_value={"properties_set": 5})
        script = [
            [_tool_use_block("tu_r", "execute_cypher_read", {
                "query": "MATCH (n:Foo) WHERE n.bar IS NULL RETURN count(n) AS count",
            })],
            [_tool_use_block("tu_w", "execute_cypher_write", {
                "query": "MATCH (n:Foo) WHERE n.bar IS NULL SET n.bar = 'x'",
            })],
            [_text_block("Filled.")],
        ]
        result = await update(
            service, "Fill missing bar on Foo nodes", client=FakeClient(script),
        )

        assert len(result["queries"]) == 2
        assert result["queries"][0]["turn"] == 1
        assert result["queries"][0]["mode"] == "read"
        assert "count" in result["queries"][0]["cypher"]
        assert result["queries"][1]["turn"] == 2
        assert result["queries"][1]["mode"] == "write"

    @pytest.mark.asyncio
    async def test_thinking_blocks_captured_per_turn(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={"properties_set": 1})
        script = [
            [
                _thinking_block("Considering whether this fix is legitimate...", display="summarized"),
                _tool_use_block("tu_w", "execute_cypher_write", {
                    "query": "MATCH (n:Foo) SET n.bar = 'x'",
                }),
            ],
            [_text_block("Done.")],
        ]
        result = await update(service, "x", client=FakeClient(script))

        assert result["queries"][0]["thinking"] == [
            {"content": "Considering whether this fix is legitimate...", "display": "summarized"},
        ]

    @pytest.mark.asyncio
    async def test_thinking_empty_when_model_did_not_think(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={})
        script = [
            [_tool_use_block("tu_w", "execute_cypher_write", {"query": "MATCH (n) SET n.x = 1"})],
            [_text_block("Done.")],
        ]
        result = await update(service, "x", client=FakeClient(script))

        assert result["queries"][0]["thinking"] == []

    @pytest.mark.asyncio
    async def test_cypher_output_in_query_record(self, service):
        from pratyabhijna.tools.update import update

        service.execute_read_query = AsyncMock(return_value=[{"count": 7}])
        script = [
            [_tool_use_block("tu_r", "execute_cypher_read", {
                "query": "MATCH (n) RETURN count(n) AS count",
            })],
            [_text_block("Found 7.")],
        ]
        result = await update(service, "count things", client=FakeClient(script))

        assert result["queries"][0]["cypher_output"] == [{"count": 7}]


# --- Cache flag ---


class TestCacheFlag:
    @pytest.mark.asyncio
    async def test_cache_false_omits_cache_control(self, service):
        from pratyabhijna.tools.update import update

        script = [[_text_block("ok")]]
        client = FakeClient(script)
        await update(service, "x", cache=False, client=client)

        for block in client.calls[0]["system"]:
            assert "cache_control" not in block

    @pytest.mark.asyncio
    async def test_cache_true_sets_1h_cache_control(self, service):
        from pratyabhijna.tools.update import update

        script = [[_text_block("ok")]]
        client = FakeClient(script)
        await update(service, "x", cache=True, client=client)

        # System gets cache_control with 1h TTL
        sys_blocks = client.calls[0]["system"]
        assert any(
            b.get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
            for b in sys_blocks
        )


# --- Bootstrap loading ---


class TestBootstrapLoading:
    @pytest.mark.asyncio
    async def test_loads_soul_and_identity_only(self, service):
        from pratyabhijna.tools.update import update

        script = [[_text_block("ok")]]
        client = FakeClient(script)
        await update(service, "x", client=client)

        system_text = "\n".join(b["text"] for b in client.calls[0]["system"])
        assert "I am the subject." in system_text  # SOUL content
        assert "Drives I'm watching for" in system_text  # IDENTITY content
        # USER/THREADS/CHRONICLE content must NOT appear
        assert "should not be loaded" not in system_text

    @pytest.mark.asyncio
    async def test_handles_missing_files_gracefully(self, tmp_path, service):
        from pratyabhijna.tools.update import update

        # Empty repo — no SOUL/IDENTITY
        empty = tmp_path / "empty"
        (empty / "memory").mkdir(parents=True)
        service.config.resources = MagicMock(repo_path=str(empty))

        script = [[_text_block("ok")]]
        client = FakeClient(script)
        # Should not raise
        result = await update(service, "x", client=client)
        assert result["response"] == "ok"


# --- Adaptive thinking config ---


class TestThinkingConfig:
    @pytest.mark.asyncio
    async def test_adaptive_thinking_with_summarized_display_requested(self, service):
        from pratyabhijna.tools.update import update

        script = [[_text_block("ok")]]
        client = FakeClient(script)
        await update(service, "x", client=client)

        call = client.calls[0]
        # Adaptive thinking with summarized display so we capture readable
        # reasoning in the JSON output.
        assert call["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert call["output_config"] == {"effort": "high"}


# --- Status detection edge cases ---


class TestStatusDetection:
    @pytest.mark.asyncio
    async def test_detach_delete_counts_as_deleted(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={"nodes_deleted": 2})
        script = [
            [_tool_use_block("tu_w", "execute_cypher_write", {
                "query": "MATCH (n:X) DETACH DELETE n",
            })],
            [_text_block("Removed.")],
        ]
        result = await update(service, "remove", client=FakeClient(script))
        assert result["status"] == "Deleted"

    @pytest.mark.asyncio
    async def test_lowercase_delete_counts_as_deleted(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={"nodes_deleted": 1})
        script = [
            [_tool_use_block("tu_w", "execute_cypher_write", {
                "query": "match (n:X) delete n",
            })],
            [_text_block("ok")],
        ]
        result = await update(service, "x", client=FakeClient(script))
        assert result["status"] == "Deleted"

    @pytest.mark.asyncio
    async def test_create_counts_as_updated(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(return_value={"nodes_created": 1})
        script = [
            [_tool_use_block("tu_w", "execute_cypher_write", {
                "query": "CREATE (n:X {name: 'y'}) RETURN n",
            })],
            [_text_block("Created.")],
        ]
        result = await update(service, "create", client=FakeClient(script))
        assert result["status"] == "Updated"


# --- Errors ---


class TestErrors:
    @pytest.mark.asyncio
    async def test_cypher_exception_recorded_as_error(self, service):
        from pratyabhijna.tools.update import update

        service.execute_write_query = AsyncMock(
            side_effect=RuntimeError("constraint violation")
        )
        script = [
            [_tool_use_block("tu_w", "execute_cypher_write", {
                "query": "CREATE (n:X)",
            })],
            [_text_block("Constraint violation; nothing changed.")],
        ]
        result = await update(service, "create", client=FakeClient(script))
        assert result["status"] == "Error"
        assert any("constraint violation" in e for e in result["errors"])
