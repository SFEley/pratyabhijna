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


# ---------------------------------------------------------------------------
# build_update_request
# ---------------------------------------------------------------------------


class TestBuildUpdateRequest:
    def test_lean_user_message_and_cached_system_prompt(self):
        from pratyabhijna.tools.update import build_update_request

        req = build_update_request(
            custom_id="update-N1",
            node={"uuid": "N1", "name": "x"},
            request_text="Change entity_type to Observation",
            soul="S", identity="I",
            model="claude-sonnet-4-6",
        )
        assert req["custom_id"] == "update-N1"
        params = req["params"]
        assert any(t["name"] == "execute_cypher_write" for t in params["tools"])
        assert any(t["name"] == "execute_cypher_read" for t in params["tools"])
        # Cache lives entirely in the system prompt: both audit's
        # SOUL+IDENTITY block and the update instructions block carry 1h
        # cache_control so every call in the cohort reads the same prefix.
        assert params["system"][0]["cache_control"]["ttl"] == "1h"
        assert params["system"][1]["cache_control"]["ttl"] == "1h"
        # User message is a single uncached string (no per-call cache
        # breakpoints to fragment the cohort cache).
        msg = params["messages"][0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], str)
        assert "N1" in msg["content"]
        assert "Change entity_type to Observation" in msg["content"]
        # No `_episode_uuids` key — episode-cluster sorting is gone with
        # the per-node cache breakpoint.
        assert "_episode_uuids" not in req

    def test_uses_node_uuid_and_name(self):
        from pratyabhijna.tools.update import build_update_request

        req = build_update_request(
            custom_id="update-N2",
            node={"uuid": "abc-123", "name": "Position thing"},
            request_text="Reclassify as Concept",
            soul="S", identity="I",
            model="claude-sonnet-4-6",
        )
        content = req["params"]["messages"][0]["content"]
        assert "abc-123" in content
        assert "Position thing" in content
        assert "Reclassify as Concept" in content


# ---------------------------------------------------------------------------
# update_from_audit_file
# ---------------------------------------------------------------------------


async def _async_iter(items):
    for item in items:
        yield item


class TestUpdateFromAuditFile:
    async def test_filters_to_update_entries(self, tmp_path, monkeypatch):
        """Only status='Update' entries should be processed."""
        import json
        from unittest.mock import AsyncMock, MagicMock
        from pratyabhijna.tools.update import update_from_audit_file

        audit_json = tmp_path / "audit.json"
        audit_json.write_text(json.dumps({
            "run_metadata": {"audit_revision": 1, "model": "claude-sonnet-4-6"},
            "results": [
                {"uuid": "N1", "name": "a", "status": "Valid", "analysis": "ok"},
                {"uuid": "N2", "name": "b", "status": "Update",
                 "analysis": "wrong", "request": "Fix it"},
                {"uuid": "N3", "name": "c", "status": "Unfixable",
                 "analysis": "nope"},
            ],
        }))
        svc = MagicMock()
        svc.config = MagicMock()
        svc.config.resources = MagicMock(repo_path=str(tmp_path))
        svc.config.llm = MagicMock(audit_model="claude-sonnet-4-6", api_key=None)
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "SOUL.md").write_text("S")
        (tmp_path / "memory" / "IDENTITY.md").write_text("I")

        node_b = MagicMock()
        node_b.uuid = "N2"
        node_b.name = "b"  # Assign .name directly — MagicMock(name=...) doesn't work
        node_b.labels = []
        node_b.summary = ""
        node_b.attributes = {}
        node_b.created_at = None
        node_b.group_id = ""
        svc.get_entity_by_uuid = AsyncMock(return_value=node_b)

        # Single Update entry → primer runs, no batch is needed.
        primer_mock = AsyncMock(return_value={
            "status": "Updated", "request": "Fix it", "response": "done",
            "guids": None, "count": 1, "queries": [], "warnings": [], "errors": [],
        })
        submit_mock = AsyncMock(return_value="batch-1")
        poll_mock = AsyncMock()
        process_mock = AsyncMock()
        monkeypatch.setattr("pratyabhijna.tools.update._run_primer_update", primer_mock)
        monkeypatch.setattr("pratyabhijna.tools.update._submit_batch", submit_mock)
        monkeypatch.setattr("pratyabhijna.tools.update.poll_batch", poll_mock)
        monkeypatch.setattr("pratyabhijna.tools.update.process_update_results", process_mock)

        client = MagicMock()
        client.close = AsyncMock()
        results = await update_from_audit_file(audit_json, service=svc, client=client)

        # Only N2 was fetched (Valid + Unfixable filtered out)
        svc.get_entity_by_uuid.assert_called_once_with("N2")
        # Primer ran with the only Update entry; batch was skipped
        assert primer_mock.call_count == 1
        primer_request = primer_mock.call_args.args[1]
        assert primer_request["custom_id"] == "update-N2"
        assert submit_mock.call_count == 0
        assert process_mock.call_count == 0
        assert len(results) == 1
        assert results[0]["status"] == "Updated"

    async def test_returns_empty_when_no_updates(self, tmp_path):
        """If audit JSON has no Update entries, return [] without calling submit."""
        import json
        from unittest.mock import MagicMock
        from pratyabhijna.tools.update import update_from_audit_file

        audit_json = tmp_path / "audit.json"
        audit_json.write_text(json.dumps({
            "run_metadata": {},
            "results": [
                {"uuid": "N1", "name": "a", "status": "Valid", "analysis": "ok"},
            ],
        }))
        svc = MagicMock()
        results = await update_from_audit_file(audit_json, service=svc, client=MagicMock())
        assert results == []


# ---------------------------------------------------------------------------
# _handle_batched_message dispatch paths
# ---------------------------------------------------------------------------


class TestHandleBatchedMessage:
    async def test_end_turn_no_tools_is_rejected(self):
        """end_turn with no tool_use → Rejected, with the model's text in response."""
        from pratyabhijna.tools.update import _handle_batched_message

        msg = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "I decline."
        msg.content = [text_block]
        outcome = await _handle_batched_message(
            message=msg, request_text="Fix N1",
            request_params={}, service=MagicMock(), client=MagicMock(),
        )
        assert outcome["status"] == "Rejected"
        assert outcome["response"] == "I decline."
        assert outcome["request"] == "Fix N1"

    async def test_single_write_executes_inline(self, monkeypatch):
        """Single execute_cypher_write tool_use → execute via _run_write client-side."""
        from pratyabhijna.tools.update import _handle_batched_message

        msg = MagicMock()
        tool_use = MagicMock()
        tool_use.type = "tool_use"
        tool_use.name = "execute_cypher_write"
        tool_use.input = {"query": "MATCH (n) SET n.x = 1", "params": {}}
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Updated."
        msg.content = [text_block, tool_use]

        import pratyabhijna.tools.update as _u
        run_write_mock = AsyncMock(return_value=(
            {"summary": "ok"},
            {"mode": "write", "cypher": "MATCH (n) SET n.x = 1",
             "params": {}, "cypher_output": [{"properties_set": 1}]},
        ))
        monkeypatch.setattr(_u, "_run_write", run_write_mock)

        outcome = await _handle_batched_message(
            message=msg, request_text="Set x to 1",
            request_params={}, service=MagicMock(), client=MagicMock(),
        )
        assert outcome["status"] == "Updated"
        assert outcome["count"] == 1
        assert outcome["response"] == "Updated."
        assert len(outcome["queries"]) == 1
        assert outcome["queries"][0]["turn"] == 1

    async def test_read_tool_use_dispatches_to_sync_continue(self, monkeypatch):
        """Read tool_use → dispatch to _sync_continue, return its outcome."""
        from pratyabhijna.tools.update import _handle_batched_message

        msg = MagicMock()
        tool_use = MagicMock()
        tool_use.type = "tool_use"
        tool_use.name = "execute_cypher_read"
        tool_use.input = {"query": "MATCH (n) RETURN n LIMIT 1"}
        msg.content = [tool_use]

        import pratyabhijna.tools.update as _u
        sync_continue_mock = AsyncMock(return_value={
            "status": "Updated", "request": "x", "response": "done",
            "guids": None, "count": 1, "queries": [], "warnings": [], "errors": [],
        })
        monkeypatch.setattr(_u, "_sync_continue", sync_continue_mock)

        outcome = await _handle_batched_message(
            message=msg, request_text="x",
            request_params={"messages": [], "model": "m", "max_tokens": 100,
                            "system": [], "tools": []},
            service=MagicMock(), client=MagicMock(),
        )
        sync_continue_mock.assert_called_once()
        assert outcome["status"] == "Updated"


# ---------------------------------------------------------------------------
# _sync_continue end-to-end
# ---------------------------------------------------------------------------


class TestSyncContinue:
    async def test_read_then_write_end_turn(self, monkeypatch):
        """End-to-end sync continuation: turn 1 had a read, continue, model writes, end."""
        import pratyabhijna.tools.update as _u
        from pratyabhijna.tools.update import _sync_continue

        # Batched turn-1 message: model called execute_cypher_read
        batch_message = MagicMock()
        batch_read = MagicMock()
        batch_read.type = "tool_use"
        batch_read.id = "tu-1"
        batch_read.name = "execute_cypher_read"
        batch_read.input = {"query": "MATCH (n) WHERE n.uuid = 'N1' RETURN n"}
        batch_message.content = [batch_read]

        run_read_mock = AsyncMock(return_value=(
            {"rows": [{"name": "N1"}]},
            {"mode": "read", "cypher": "MATCH ...", "params": {}, "cypher_output": [{"name": "N1"}]},
        ))
        run_write_mock = AsyncMock(return_value=(
            {"summary": "ok"},
            {"mode": "write", "cypher": "MATCH (n) SET n.fixed = true",
             "params": {}, "cypher_output": [{"properties_set": 1}]},
        ))
        monkeypatch.setattr(_u, "_run_read", run_read_mock)
        monkeypatch.setattr(_u, "_run_write", run_write_mock)

        # Mock client: turn 2 returns a write tool_use; turn 3 returns end_turn with text
        client = MagicMock()
        write_tool_use = MagicMock()
        write_tool_use.type = "tool_use"
        write_tool_use.id = "tu-2"
        write_tool_use.name = "execute_cypher_write"
        write_tool_use.input = {"query": "MATCH (n) SET n.fixed = true", "params": {}}
        turn2_response = MagicMock(content=[write_tool_use], stop_reason="tool_use")
        final_text_block = MagicMock(type="text", text="All fixed.")
        turn3_response = MagicMock(content=[final_text_block], stop_reason="end_turn")

        class FakeStream:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get_final_message(self):
                return self.response

        client.messages.stream = MagicMock(side_effect=[
            FakeStream(turn2_response),
            FakeStream(turn3_response),
        ])

        request_params = {
            "model": "m", "max_tokens": 100,
            "system": [], "messages": [{"role": "user", "content": "x"}],
            "tools": [],
        }

        outcome = await _sync_continue(
            client=client, message=batch_message,
            request_params=request_params, request_text="fix N1",
            service=MagicMock(),
        )

        assert outcome["status"] == "Updated"
        assert outcome["count"] >= 1
        assert outcome["response"] == "All fixed."
        # Two queries recorded: turn1 read, turn2 write
        assert len(outcome["queries"]) == 2
        assert outcome["queries"][0]["mode"] == "read"
        assert outcome["queries"][1]["mode"] == "write"


# ---------------------------------------------------------------------------
# _parse_update_args — new --input flag coverage
# (additional tests beyond what test_main.py now covers)
# ---------------------------------------------------------------------------


class TestParseUpdateArgsInputFlag:
    def test_accepts_input_flag(self):
        from pratyabhijna.__main__ import _parse_update_args

        assert _parse_update_args(["--input", "audit.json"]) == (None, False, "audit.json", None)

    def test_rejects_both_description_and_input(self):
        from pratyabhijna.__main__ import _parse_update_args

        assert _parse_update_args(["desc", "--input", "audit.json"]) is None

    def test_rejects_neither(self):
        from pratyabhijna.__main__ import _parse_update_args

        assert _parse_update_args([]) is None

    def test_accepts_description_with_cache(self):
        from pratyabhijna.__main__ import _parse_update_args

        assert _parse_update_args(["fix N1", "--cache"]) == ("fix N1", True, None, None)


# ---------------------------------------------------------------------------
# process_update_results — errored batch entry
# ---------------------------------------------------------------------------


async def test_process_update_results_failed_batch_entry_returns_error_outcome():
    """When a batched response has type != 'succeeded', record an Error outcome
    via _failed_outcome rather than crashing or skipping."""
    from pratyabhijna.tools.update import process_update_results
    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(
            custom_id="update-N1",
            result=MagicMock(type="errored", error="rate limited"),
        ),
    ]))
    results = await process_update_results(
        client, "batch-xyz",
        service=MagicMock(),
        request_lookup={"update-N1": {"params": {}, "request_text": "fix N1"}},
    )
    assert len(results) == 1
    assert results[0]["status"] == "Error"
    assert results[0]["request"] == "fix N1"
    assert "rate limited" in results[0]["errors"][0]
