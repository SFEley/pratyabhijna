from pratyabhijna.tools.audit import AUDIT_REVISION


def test_audit_revision_is_int():
    assert isinstance(AUDIT_REVISION, int)
    assert AUDIT_REVISION >= 1


from pratyabhijna.tools.audit import build_system_prompt


def test_system_prompt_has_two_blocks_with_cache_markers():
    blocks = build_system_prompt(
        soul="SOUL TEXT",
        identity="IDENTITY TEXT",
        instructions="INSTRUCTIONS TEXT",
        guidance=None,
    )
    assert len(blocks) == 2
    # Block 0: SOUL + IDENTITY, 1h cache
    assert blocks[0]["type"] == "text"
    assert "SOUL TEXT" in blocks[0]["text"]
    assert "IDENTITY TEXT" in blocks[0]["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Block 1: instructions, also 1h cache (paired audit/update flow shares
    # the same prefix; 5min would expire mid-run on large cohorts).
    assert "INSTRUCTIONS TEXT" in blocks[1]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_system_prompt_appends_guidance():
    blocks = build_system_prompt(
        soul="S", identity="I",
        instructions="BASE",
        guidance="EXTRA RULE",
    )
    assert "BASE" in blocks[1]["text"]
    assert "EXTRA RULE" in blocks[1]["text"]


from pratyabhijna.tools.audit import build_user_message


def test_user_message_orders_episode_recall_node():
    msg = build_user_message(
        obj={"uuid": "N1", "name": "test", "labels": ["Entity"], "summary": "..."},
        episodes=[{"uuid": "E1", "content": "ep body"}],
        recall_results={"nodes": [], "edges": []},
        kind="entity_node",
    )
    assert msg["role"] == "user"
    blocks = msg["content"]
    assert len(blocks) == 3
    # Block 0: episode (1h cached)
    assert "ep body" in blocks[0]["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Block 1: recall (no cache)
    assert "cache_control" not in blocks[1]
    # Block 2: object (no cache)
    assert "N1" in blocks[2]["text"]
    assert "cache_control" not in blocks[2]


def test_user_message_handles_multiple_episodes():
    msg = build_user_message(
        obj={"uuid": "N1", "name": "x"},
        episodes=[
            {"uuid": "E1", "content": "first"},
            {"uuid": "E2", "content": "second"},
        ],
        recall_results={"nodes": [], "edges": []},
        kind="entity_node",
    )
    text = msg["content"][0]["text"]
    assert "first" in text
    assert "second" in text


from pratyabhijna.tools.audit import build_audit_request, AUDIT_RESPONSE_SCHEMA


def test_audit_response_schema_has_required_fields():
    props = AUDIT_RESPONSE_SCHEMA["properties"]
    # name is sourced from the input object, not echoed by the model
    assert {"uuid", "status", "analysis"} <= set(props)
    assert "name" not in props
    assert AUDIT_RESPONSE_SCHEMA["properties"]["status"]["enum"] == [
        "Valid", "Update", "Unfixable"
    ]


def test_build_audit_request_returns_batch_request_shape():
    req = build_audit_request(
        custom_id="audit-N1",
        obj={"uuid": "N1", "name": "x"},
        episodes=[{"uuid": "E1", "content": "c"}],
        recall_results={"nodes": [], "edges": []},
        kind="entity_node",
        soul="S", identity="I",
        guidance=None,
        model="claude-sonnet-4-6",
    )
    assert req["custom_id"] == "audit-N1"
    params = req["params"]
    assert params["model"] == "claude-sonnet-4-6"
    assert "max_tokens" in params
    assert len(params["system"]) == 2
    assert len(params["messages"]) == 1
    assert params["output_config"]["format"]["type"] == "json_schema"
    # Internal key for sorting must be present at the top level (NOT in params)
    assert req["_episode_uuids"] == ["E1"]


from pratyabhijna.tools.audit import sort_requests_by_episodes, strip_internal_keys


def test_sort_clusters_same_episode_requests():
    requests = [
        {"custom_id": "n1", "_episode_uuids": ["E1"]},
        {"custom_id": "n2", "_episode_uuids": ["E2"]},
        {"custom_id": "n3", "_episode_uuids": ["E1"]},
        {"custom_id": "n4", "_episode_uuids": ["E1", "E2"]},
    ]
    sorted_reqs = sort_requests_by_episodes(requests)
    # Same-episode-tuple requests must be adjacent in the result
    keys = [tuple(r["_episode_uuids"]) for r in sorted_reqs]
    seen_then_left = set()
    current = keys[0]
    for k in keys[1:]:
        if k != current:
            seen_then_left.add(current)
            current = k
        # If we see a key we already finished with, sort isn't clustering
        assert current not in seen_then_left


def test_strip_removes_underscore_prefixed_keys():
    requests = [
        {"custom_id": "n1", "_episode_uuids": ["E1"], "params": {"model": "x"}},
    ]
    cleaned = strip_internal_keys(requests)
    assert "_episode_uuids" not in cleaned[0]
    assert cleaned[0]["custom_id"] == "n1"
    assert cleaned[0]["params"] == {"model": "x"}


from pratyabhijna.tools.audit import is_uuid_list, parse_uuid_list


def test_is_uuid_list_recognizes_canonical_uuids():
    assert is_uuid_list("550e8400-e29b-41d4-a716-446655440000")
    assert is_uuid_list(
        "550e8400-e29b-41d4-a716-446655440000 660e8400-e29b-41d4-a716-446655440001"
    )
    assert is_uuid_list(
        "550e8400-e29b-41d4-a716-446655440000\n660e8400-e29b-41d4-a716-446655440001"
    )


def test_is_uuid_list_rejects_prose_and_malformed():
    assert not is_uuid_list("find all Position nodes")
    assert not is_uuid_list("nodes created last week")
    assert not is_uuid_list("abc123-def456-7890")  # not canonical UUID shape
    assert not is_uuid_list("")
    assert not is_uuid_list("   ")


def test_parse_uuid_list_handles_whitespace_variants():
    s = "550e8400-e29b-41d4-a716-446655440000\n660e8400-e29b-41d4-a716-446655440001"
    uuids = parse_uuid_list(s)
    assert uuids == [
        "550e8400-e29b-41d4-a716-446655440000",
        "660e8400-e29b-41d4-a716-446655440001",
    ]


def test_parse_uuid_list_lowercases():
    """Both resolution paths must produce lowercase UUIDs (Neo4j stores them lowercase)."""
    s = "550E8400-E29B-41D4-A716-446655440000"
    assert parse_uuid_list(s) == ["550e8400-e29b-41d4-a716-446655440000"]


from unittest.mock import MagicMock
from pratyabhijna.tools.audit import resolve_uuid_list


async def test_resolve_uuid_list_skips_query():
    service = MagicMock()
    uuids = await resolve_uuid_list(
        "550e8400-e29b-41d4-a716-446655440000", service=service,
    )
    assert uuids == ["550e8400-e29b-41d4-a716-446655440000"]


async def test_resolve_augmented_prompt_asks_for_uuid_prefix(monkeypatch):
    """The prompt sent to the query agent must instruct it to lead with UUIDs,
    one per line, before any prose."""
    seen: dict[str, str] = {}

    async def fake_query(service, request, **kwargs):
        seen["request"] = request
        return {"response": "", "cypher_log": [], "refused": False, "iterations": 1}

    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    await resolve_uuid_list("find all Position nodes", service=MagicMock())
    assert "find all Position nodes" in seen["request"]
    # Contract pinned: leading UUIDs, one per line, nothing before
    lower = seen["request"].lower()
    assert "one" in lower and "per line" in lower
    assert "uuid" in lower


async def test_resolve_extracts_leading_uuid_block_only(monkeypatch):
    """Only the prefix of UUID-only lines is the cohort. Once a non-UUID line
    appears, parsing stops and any later UUIDs are ignored."""
    async def fake_query(service, request, **kwargs):
        return {
            "response": (
                "550e8400-e29b-41d4-a716-446655440000\n"
                "660e8400-e29b-41d4-a716-446655440001\n"
                "\n"
                "I considered 770e8400-e29b-41d4-a716-446655440002 "
                "but rejected it as out of scope."
            ),
            "cypher_log": [],
            "refused": False,
            "iterations": 1,
        }
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    uuids = await resolve_uuid_list("find Position nodes", service=MagicMock())
    assert uuids == [
        "550e8400-e29b-41d4-a716-446655440000",
        "660e8400-e29b-41d4-a716-446655440001",
    ]


async def test_resolve_returns_empty_when_uuids_only_appear_in_prose(monkeypatch):
    """The old permissive regex would have returned the UUID. The strict parser
    requires a leading UUID line and refuses to fish UUIDs out of prose."""
    async def fake_query(service, request, **kwargs):
        return {
            "response": (
                "Found 2 Position nodes: "
                "550e8400-e29b-41d4-a716-446655440000 and "
                "660e8400-e29b-41d4-a716-446655440001."
            ),
            "cypher_log": [],
            "refused": False,
            "iterations": 1,
        }
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    uuids = await resolve_uuid_list("any", service=MagicMock())
    assert uuids == []


async def test_resolve_logs_warning_when_response_has_trailing_prose(
    monkeypatch, caplog,
):
    """When the agent appends prose after the UUID list, log it at WARNING so
    the operator can see what the agent had to say."""
    import logging

    async def fake_query(service, request, **kwargs):
        return {
            "response": (
                "550e8400-e29b-41d4-a716-446655440000\n"
                "Note: I excluded one node because it was already audited."
            ),
            "cypher_log": [],
            "refused": False,
            "iterations": 1,
        }
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)

    with caplog.at_level(logging.WARNING, logger="pratyabhijna.tools.audit"):
        uuids = await resolve_uuid_list("any", service=MagicMock())
    assert uuids == ["550e8400-e29b-41d4-a716-446655440000"]
    # The trailing prose lands in a WARNING record
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "audit" in r.name
    ]
    assert warnings, "expected a WARNING for trailing prose"
    assert any("excluded one node" in r.getMessage() for r in warnings)


async def test_resolve_deduplicates_uuids(monkeypatch):
    """If the agent lists the same UUID twice in the leading block, return it once."""
    async def fake_query(service, request, **kwargs):
        return {
            "response": (
                "550e8400-e29b-41d4-a716-446655440000\n"
                "550e8400-e29b-41d4-a716-446655440000\n"
            ),
            "cypher_log": [],
            "refused": False,
            "iterations": 1,
        }
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    service = MagicMock()
    uuids = await resolve_uuid_list("any prompt", service=service)
    assert uuids == ["550e8400-e29b-41d4-a716-446655440000"]


async def test_resolve_returns_empty_when_query_returns_no_uuids(monkeypatch):
    async def fake_query(service, request, **kwargs):
        return {
            "response": "No matching nodes found.",
            "cypher_log": [],
            "refused": False,
            "iterations": 1,
        }
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    service = MagicMock()
    uuids = await resolve_uuid_list("find unicorns", service=service)
    assert uuids == []


async def test_resolve_returns_empty_on_refusal(monkeypatch):
    """When the query agent refuses (no Cypher executed), resolve returns []."""
    async def fake_query(service, request, **kwargs):
        return {"response": "", "refused": True, "cypher_log": [], "iterations": 1}
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    uuids = await resolve_uuid_list("find anything", service=MagicMock())
    assert uuids == []


from unittest.mock import AsyncMock
from pratyabhijna.tools.audit import submit_audit_batch


async def test_submit_audit_batch_strips_internal_keys_and_returns_id():
    """submit_audit_batch strips _episode_uuids and preserves caller-provided order.

    Sorting is the caller's responsibility (typically via
    sort_requests_by_episodes); submit_audit_batch is now a thin wrapper
    around batches.create + key-strip so the primer can use the same
    sorted ordering when it picks the first request.
    """
    client = MagicMock()
    client.messages.batches.create = AsyncMock(return_value=MagicMock(id="batch_abc"))
    requests = [
        {"custom_id": "n1", "_episode_uuids": ["E2"], "params": {"model": "x"}},
        {"custom_id": "n2", "_episode_uuids": ["E1"], "params": {"model": "x"}},
    ]
    batch_id = await submit_audit_batch(client, requests)
    assert batch_id == "batch_abc"

    submitted = client.messages.batches.create.call_args.kwargs["requests"]
    # Internal sort-key fields must be stripped
    assert all("_episode_uuids" not in r for r in submitted)
    # Order is preserved — caller decides
    assert submitted[0]["custom_id"] == "n1"
    assert submitted[1]["custom_id"] == "n2"


from pratyabhijna.tools.audit import (
    poll_batch, process_results, run_primer_audit, AUDIT_REVISION,
)


# --- Primer ---


async def test_run_primer_audit_succeeds_returns_outcome_and_node_uuid():
    """The primer sends one synchronous Messages.create and produces an
    outcome dict in the same shape process_results yields per item, plus
    the node uuid for audited_at stamping."""
    client = MagicMock()
    fake_message = MagicMock()
    fake_message.content = [
        MagicMock(type="text", text='{"uuid": "uuid-A", "status": "Valid", '
                                    '"analysis": "fine"}'),
    ]
    client.messages.create = AsyncMock(return_value=fake_message)
    request = {
        "custom_id": "audit-uuid-A",
        "params": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "system": [], "messages": []},
        "_episode_uuids": ["E1"],
    }
    queue = MagicMock()

    outcome, node_uuid, edge_uuid = await run_primer_audit(
        client, request, queue=queue,
        node_names={"uuid-A": "A"},
        object_kinds={"uuid-A": "entity_node"},
    )

    assert outcome["status"] == "Valid"
    assert outcome["uuid"] == "uuid-A"
    assert outcome["name"] == "A"
    assert outcome["kind"] == "entity_node"
    assert node_uuid == "uuid-A"
    assert edge_uuid is None

    # _episode_uuids must NOT leak into messages.create
    create_kwargs = client.messages.create.call_args.kwargs
    assert "_episode_uuids" not in create_kwargs


async def test_run_primer_audit_returns_error_on_exception():
    """Network/timeout failures during the primer surface as an Error outcome
    with no audited UUIDs — the caller aborts the batch in this state."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
    request = {
        "custom_id": "audit-uuid-A",
        "params": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "system": [], "messages": []},
        "_episode_uuids": ["E1"],
    }

    outcome, node_uuid, edge_uuid = await run_primer_audit(
        client, request, queue=MagicMock(),
        node_names={"uuid-A": "A"},
        object_kinds={"uuid-A": "entity_node"},
    )

    assert outcome["status"] == "Error"
    assert outcome["uuid"] == "uuid-A"
    assert "RuntimeError" in outcome["error"]
    assert node_uuid is None
    assert edge_uuid is None


async def test_poll_batch_returns_when_status_ended(monkeypatch):
    """poll_batch returns the final batch object once processing_status == 'ended'."""
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    client = MagicMock()
    states = [
        MagicMock(
            processing_status="in_progress",
            request_counts=MagicMock(processing=5, succeeded=0, errored=0),
        ),
        MagicMock(
            processing_status="in_progress",
            request_counts=MagicMock(processing=2, succeeded=3, errored=0),
        ),
        MagicMock(
            processing_status="ended",
            request_counts=MagicMock(processing=0, succeeded=5, errored=0),
        ),
    ]
    client.messages.batches.retrieve = AsyncMock(side_effect=states)

    final = await poll_batch(client, "batch_abc", interval=0.001)
    assert final.processing_status == "ended"
    # Slept twice (between the three retrieve calls), not after the final one
    assert len(sleeps) == 2
    assert sleeps == [0.001, 0.001]


# ── Task 5: process_results ──────────────────────────────────────────────────


async def _async_iter(items):
    """Async generator helper for mocking AsyncAnthropic's batches.results()."""
    for item in items:
        yield item


async def test_process_valid_result_records_and_does_not_enqueue_thread():
    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(
            custom_id="audit-N1",
            result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[
                    MagicMock(
                        type="text",
                        text='{"uuid":"N1","status":"Valid","analysis":"ok"}',
                    )
                ]),
            ),
        ),
    ]))
    service = MagicMock()
    service.execute_write_query = AsyncMock()
    queue = MagicMock()
    queue.enqueue = AsyncMock()

    results = await process_results(
        client, "batch_abc",
        service=service, queue=queue,
        node_names={"N1": "node_one"},
    )

    assert results[0]["status"] == "Valid"
    assert results[0]["uuid"] == "N1"
    assert results[0]["name"] == "node_one"  # injected from node_names
    assert results[0]["analysis"] == "ok"
    queue.enqueue.assert_not_called()  # no Thread for Valid
    # audited_at write happens once at end, regardless of verdict
    service.execute_write_query.assert_called_once()


async def test_process_update_result_records_with_request_field():
    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(
            custom_id="audit-N2",
            result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[
                    MagicMock(
                        type="text",
                        text=(
                            '{"uuid":"N2","status":"Update",'
                            '"analysis":"wrong type",'
                            '"request":"Change entity_type to Observation"}'
                        ),
                    )
                ]),
            ),
        ),
    ]))
    service = MagicMock()
    service.execute_write_query = AsyncMock()
    queue = MagicMock()
    queue.enqueue = AsyncMock()

    results = await process_results(
        client, "b",
        service=service, queue=queue,
        node_names={"N2": "thing"},
    )

    assert results[0]["status"] == "Update"
    assert results[0]["request"] == "Change entity_type to Observation"
    assert results[0]["name"] == "thing"
    queue.enqueue.assert_not_called()  # Update doesn't enqueue a Thread

    # Two writes: stamp audited_at + append notes
    assert service.execute_write_query.call_count == 2
    notes_cypher, notes_params = service.execute_write_query.call_args_list[1].args
    assert "n.notes" in notes_cypher
    assert len(notes_params["updates"]) == 1
    assert notes_params["updates"][0]["uuid"] == "N2"
    note = notes_params["updates"][0]["note"]
    assert note.startswith("Audit ")
    assert "Change entity_type to Observation" in note


async def test_process_unfixable_result_warns_and_enqueues_thread():
    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(
            custom_id="audit-N3",
            result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[
                    MagicMock(
                        type="text",
                        text=(
                            '{"uuid":"N3","status":"Unfixable",'
                            '"analysis":"weird and unrecoverable"}'
                        ),
                    )
                ]),
            ),
        ),
    ]))
    service = MagicMock()
    service.execute_write_query = AsyncMock()
    queue = MagicMock()
    queue.enqueue = AsyncMock()

    results = await process_results(
        client, "b",
        service=service, queue=queue,
        node_names={"N3": "weirdthing"},
    )

    assert results[0]["status"] == "Unfixable"
    # Thread enqueued exactly once via remember (which calls queue.enqueue)
    queue.enqueue.assert_called_once()
    call_args = queue.enqueue.call_args
    assert call_args.args[0] == "add_episode"
    payload = call_args.args[1]
    assert payload["memory_type"] == "Thread"
    assert payload["source"] == "audit"
    assert "N3" in payload["content"]
    assert "weirdthing" in payload["content"]
    assert "weird and unrecoverable" in payload["content"]


async def test_audit_instructions_pin_uuid_to_input_node():
    """The audit instructions must explicitly tell the model to echo the same
    UUID it received — otherwise it might pick a UUID from recall results or
    edges, which silently breaks audited_at stamping and downstream update."""
    from pratyabhijna.tools.audit import AUDIT_INSTRUCTIONS

    text = AUDIT_INSTRUCTIONS.lower()
    assert "uuid" in text
    # Some phrasing that pins the response uuid to the provided node uuid
    assert (
        ("must" in text and "match" in text)
        or "exactly" in text
        or "echo" in text
    ), f"AUDIT_INSTRUCTIONS does not pin the response uuid: {AUDIT_INSTRUCTIONS!r}"


async def test_process_uuid_mismatch_records_error_and_skips_stamp(caplog):
    """If the model returns a UUID that doesn't match the one we sent, treat
    the response as a failure: log an ERROR with both UUIDs, record an Error
    outcome carrying the trustworthy custom_id-derived UUID, and skip the
    audited_at stamp for that node."""
    import logging

    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(
            custom_id="audit-N1",  # we sent UUID N1
            result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[
                    MagicMock(
                        type="text",
                        # ...but the model echoed a different UUID (e.g. one
                        # it picked up from a recall result or an edge target)
                        text='{"uuid":"WRONG","status":"Valid","analysis":"ok"}',
                    )
                ]),
            ),
        ),
    ]))
    service = MagicMock()
    service.execute_write_query = AsyncMock()
    queue = MagicMock()
    queue.enqueue = AsyncMock()

    with caplog.at_level(logging.ERROR, logger="pratyabhijna.tools.audit"):
        results = await process_results(
            client, "b",
            service=service, queue=queue,
            node_names={"N1": "node_one"},
        )

    # Outcome records the failure with the trustworthy UUID (from custom_id)
    assert len(results) == 1
    assert results[0]["status"] == "Error"
    assert results[0]["uuid"] == "N1"
    assert results[0]["name"] == "node_one"
    # Error message should mention both UUIDs to aid triage
    err = results[0].get("error", "")
    assert "N1" in err and "WRONG" in err

    # The bogus UUID does NOT get stamped — we couldn't confidently audit it
    service.execute_write_query.assert_not_called()
    queue.enqueue.assert_not_called()

    # An ERROR-level log was emitted
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "audit" in r.name
    ]
    assert errors, "expected an ERROR log record on uuid mismatch"
    msg = " ".join(r.getMessage() for r in errors)
    assert "N1" in msg and "WRONG" in msg


async def test_process_errored_result_records_status_error():
    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(
            custom_id="audit-N4",
            result=MagicMock(
                type="errored",
                error="rate limited",
            ),
        ),
    ]))
    service = MagicMock()
    service.execute_write_query = AsyncMock()
    queue = MagicMock()
    queue.enqueue = AsyncMock()

    results = await process_results(
        client, "b",
        service=service, queue=queue,
        node_names={"N4": "name4"},
    )

    assert results[0]["status"] == "Error"
    assert results[0]["uuid"] == "N4"
    assert results[0]["name"] == "name4"
    assert "rate limited" in results[0]["error"]
    # Errored requests do NOT count toward audited_at stamping
    service.execute_write_query.assert_not_called()
    queue.enqueue.assert_not_called()


async def test_audited_at_stamped_for_all_succeeded_results_in_one_call():
    client = MagicMock()
    client.messages.batches.results = AsyncMock(return_value=_async_iter([
        MagicMock(custom_id="audit-N1", result=MagicMock(
            type="succeeded",
            message=MagicMock(content=[MagicMock(
                type="text",
                text='{"uuid":"N1","status":"Valid","analysis":"ok"}',
            )]),
        )),
        MagicMock(custom_id="audit-N2", result=MagicMock(
            type="succeeded",
            message=MagicMock(content=[MagicMock(
                type="text",
                text='{"uuid":"N2","status":"Valid","analysis":"ok"}',
            )]),
        )),
    ]))
    service = MagicMock()
    service.execute_write_query = AsyncMock()
    queue = MagicMock()

    await process_results(
        client, "b",
        service=service, queue=queue,
        node_names={"N1": "a", "N2": "b"},
    )

    # Single batched write covering both UUIDs
    service.execute_write_query.assert_called_once()
    cypher_arg, params_arg = service.execute_write_query.call_args.args
    assert "UNWIND $uuids" in cypher_arg
    assert sorted(params_arg["uuids"]) == ["N1", "N2"]
    assert params_arg["rev"] == AUDIT_REVISION
    # `now` is an ISO timestamp string
    assert "T" in params_arg["now"]


async def test_run_audit_run_orchestrates_resolve_build_submit_process(monkeypatch, tmp_path):
    """End-to-end orchestration with all I/O mocked.

    Verifies: resolve produces UUIDs -> for each UUID, fetch + recall + build
    request -> submit batch -> poll -> process -> return summary. The mocked
    `process_results` returns a synthesized list and `submit_audit_batch`
    is observed to receive the right number of requests.
    """
    from pratyabhijna.tools.audit import run_audit_run

    # Mock the service
    service = MagicMock()
    service.config = MagicMock()
    service.config.resources = MagicMock()
    service.config.resources.repo_path = str(tmp_path)
    service.config.llm = MagicMock(audit_model="claude-sonnet-4-6")

    # Set up SOUL + IDENTITY files in repo_path
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "SOUL.md").write_text("SOUL TEXT")
    (tmp_path / "memory" / "IDENTITY.md").write_text("IDENTITY TEXT")

    # Resolve returns two UUIDs
    async def fake_resolve(input_str, *, service):
        return ["uuid-A", "uuid-B"]
    monkeypatch.setattr("pratyabhijna.tools.audit.resolve_uuid_list", fake_resolve)

    # Both UUIDs detect as entity nodes; load_object returns canned shapes.
    async def fake_detect_kind(svc, uuid):
        return "entity_node"
    monkeypatch.setattr("pratyabhijna.tools.audit.detect_kind", fake_detect_kind)

    async def fake_load_object(svc, uuid, kind):
        name = "A" if uuid == "uuid-A" else "B"
        return ({"uuid": uuid, "name": name, "labels": ["Entity"]}, [], {"nodes": [], "edges": []}, name)
    monkeypatch.setattr("pratyabhijna.tools.audit.load_object", fake_load_object)

    # Mock anthropic client + the four batch/primer operations. The primer
    # runs synchronously to warm the cache; the batch handles the rest.
    client = MagicMock()
    primer_mock = AsyncMock(return_value=(
        {"uuid": "uuid-A", "name": "A", "kind": "entity_node",
         "status": "Valid", "analysis": "ok"},
        "uuid-A",  # audited node uuid
        None,      # audited edge uuid
    ))
    submit_mock = AsyncMock(return_value="batch-xyz")
    poll_mock = AsyncMock()
    process_mock = AsyncMock(return_value=[
        {"uuid": "uuid-B", "name": "B", "kind": "entity_node",
         "status": "Update", "analysis": "wrong", "request": "fix it"},
    ])
    stamp_annotate_mock = AsyncMock()
    monkeypatch.setattr("pratyabhijna.tools.audit.run_primer_audit", primer_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.submit_audit_batch", submit_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.poll_batch", poll_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.process_results", process_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit._stamp_and_annotate", stamp_annotate_mock)

    queue = MagicMock()

    summary = await run_audit_run(
        service, queue, "find stuff",
        guidance=None, anthropic_client=client,
    )

    # Primer ran once with the first sorted request
    assert primer_mock.call_count == 1
    primer_request = primer_mock.call_args.args[1]
    assert primer_request["custom_id"] in {"audit-uuid-A", "audit-uuid-B"}

    # Batch was submitted with the remaining 1 request
    requests_arg = submit_mock.call_args.args[1]
    assert len(requests_arg) == 1

    # All custom_ids accounted for across primer + batch
    seen_ids = {primer_request["custom_id"]} | {r["custom_id"] for r in requests_arg}
    assert seen_ids == {"audit-uuid-A", "audit-uuid-B"}

    # Process was called with node_names + object_kinds mappings for both
    process_kwargs = process_mock.call_args.kwargs
    assert process_kwargs["node_names"] == {"uuid-A": "A", "uuid-B": "B"}
    assert process_kwargs["object_kinds"] == {"uuid-A": "entity_node", "uuid-B": "entity_node"}

    # Summary structure — primer + batch outcomes both included
    assert summary["cohort_size"] == 2
    assert summary["model"] == "claude-sonnet-4-6"
    assert len(summary["results"]) == 2
    assert "started_at" in summary and "completed_at" in summary


async def test_run_audit_run_aborts_batch_when_primer_fails(monkeypatch, tmp_path):
    """If the synchronous primer fails, the batch is NOT submitted — submitting
    would re-trigger the cache-write race the primer was meant to prevent.
    Synthetic Error outcomes account for every cohort UUID in the JSON output.
    """
    from pratyabhijna.tools.audit import run_audit_run

    service = MagicMock()
    service.config = MagicMock()
    service.config.resources = MagicMock(repo_path=str(tmp_path))
    service.config.llm = MagicMock(audit_model="claude-sonnet-4-6")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "SOUL.md").write_text("S")
    (tmp_path / "memory" / "IDENTITY.md").write_text("I")

    async def fake_resolve(input_str, *, service):
        return ["uuid-A", "uuid-B", "uuid-C"]
    monkeypatch.setattr("pratyabhijna.tools.audit.resolve_uuid_list", fake_resolve)

    async def fake_detect_kind(svc, uuid):
        return "entity_node"
    monkeypatch.setattr("pratyabhijna.tools.audit.detect_kind", fake_detect_kind)

    async def fake_load_object(svc, uuid, kind):
        name = uuid[-1]
        return ({"uuid": uuid, "name": name}, [], {"nodes": [], "edges": []}, name)
    monkeypatch.setattr("pratyabhijna.tools.audit.load_object", fake_load_object)

    primer_mock = AsyncMock(return_value=(
        {"custom_id": "audit-uuid-A", "uuid": "uuid-A", "name": "A",
         "kind": "entity_node", "status": "Error",
         "error": "RuntimeError: connection refused"},
        None,
        None,
    ))
    submit_mock = AsyncMock()
    process_mock = AsyncMock()
    stamp_mock = AsyncMock()
    monkeypatch.setattr("pratyabhijna.tools.audit.run_primer_audit", primer_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.submit_audit_batch", submit_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.process_results", process_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit._stamp_and_annotate", stamp_mock)

    summary = await run_audit_run(
        service, MagicMock(), "find stuff",
        guidance=None, anthropic_client=MagicMock(),
    )

    # Batch was never submitted — that's the whole point
    submit_mock.assert_not_called()
    process_mock.assert_not_called()
    # Primer's audited_at stamp must NOT land for an Error outcome
    stamp_mock.assert_not_called()

    # All three cohort UUIDs accounted for: primer's Error + 2 synthetic Errors
    assert summary["cohort_size"] == 3
    assert len(summary["results"]) == 3
    statuses = [r["status"] for r in summary["results"]]
    assert statuses == ["Error", "Error", "Error"]
    errors = [r["error"] for r in summary["results"]]
    assert "connection refused" in errors[0]
    assert all("Primer" in e for e in errors[1:])


async def test_run_audit_run_single_request_skips_batch(monkeypatch, tmp_path):
    """A cohort of 1 runs the primer only — no batch submission."""
    from pratyabhijna.tools.audit import run_audit_run

    service = MagicMock()
    service.config = MagicMock()
    service.config.resources = MagicMock(repo_path=str(tmp_path))
    service.config.llm = MagicMock(audit_model="claude-sonnet-4-6")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "SOUL.md").write_text("S")
    (tmp_path / "memory" / "IDENTITY.md").write_text("I")

    async def fake_resolve(input_str, *, service):
        return ["uuid-A"]
    monkeypatch.setattr("pratyabhijna.tools.audit.resolve_uuid_list", fake_resolve)

    async def fake_detect_kind(svc, uuid):
        return "entity_node"
    monkeypatch.setattr("pratyabhijna.tools.audit.detect_kind", fake_detect_kind)

    async def fake_load_object(svc, uuid, kind):
        return ({"uuid": uuid, "name": "A"}, [], {"nodes": [], "edges": []}, "A")
    monkeypatch.setattr("pratyabhijna.tools.audit.load_object", fake_load_object)

    primer_mock = AsyncMock(return_value=(
        {"uuid": "uuid-A", "name": "A", "kind": "entity_node",
         "status": "Valid", "analysis": "ok"},
        "uuid-A",
        None,
    ))
    submit_mock = AsyncMock()
    process_mock = AsyncMock()
    stamp_mock = AsyncMock()
    monkeypatch.setattr("pratyabhijna.tools.audit.run_primer_audit", primer_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.submit_audit_batch", submit_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit.process_results", process_mock)
    monkeypatch.setattr("pratyabhijna.tools.audit._stamp_and_annotate", stamp_mock)

    summary = await run_audit_run(
        service, MagicMock(), "find one",
        guidance=None, anthropic_client=MagicMock(),
    )

    submit_mock.assert_not_called()
    process_mock.assert_not_called()
    stamp_mock.assert_called_once()
    assert summary["cohort_size"] == 1
    assert len(summary["results"]) == 1
    assert summary["results"][0]["status"] == "Valid"


async def test_run_audit_run_empty_cohort_skips_batch(monkeypatch, tmp_path):
    """If resolve returns no UUIDs, skip submit/poll/process entirely."""
    from pratyabhijna.tools.audit import run_audit_run

    service = MagicMock()
    service.config = MagicMock()
    service.config.resources = MagicMock(repo_path=str(tmp_path))
    service.config.llm = MagicMock(audit_model="claude-sonnet-4-6")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "SOUL.md").write_text("S")
    (tmp_path / "memory" / "IDENTITY.md").write_text("I")

    async def fake_resolve(input_str, *, service):
        return []
    monkeypatch.setattr("pratyabhijna.tools.audit.resolve_uuid_list", fake_resolve)

    submit_mock = AsyncMock()
    monkeypatch.setattr("pratyabhijna.tools.audit.submit_audit_batch", submit_mock)

    summary = await run_audit_run(
        service, MagicMock(), "find unicorns",
        guidance=None, anthropic_client=MagicMock(),
    )

    assert summary["cohort_size"] == 0
    assert summary["results"] == []
    submit_mock.assert_not_called()


def test_parse_audit_args_minimal():
    from pratyabhijna.__main__ import _parse_audit_args
    parsed = _parse_audit_args(["find Position nodes"])
    assert parsed == ("find Position nodes", None, None)


def test_parse_audit_args_with_guidance_and_output():
    from pratyabhijna.__main__ import _parse_audit_args
    parsed = _parse_audit_args([
        "find Position nodes",
        "--guidance", "migrate to Observation",
        "--output", "/tmp/audit.json",
    ])
    assert parsed == ("find Position nodes", "migrate to Observation", "/tmp/audit.json")


def test_parse_audit_args_rejects_empty():
    from pratyabhijna.__main__ import _parse_audit_args
    assert _parse_audit_args([]) is None
    assert _parse_audit_args(["--guidance", "x"]) is None
    assert _parse_audit_args([""]) is None


# ─────────────────────────────────────────────────────────────────────────────
# Kind detection + general-object support
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectKind:
    async def test_detects_entity_node(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_ENTITY_NODE

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": ["Entity", "Person"], "edge_type": None}], None, None,
        ))
        kind = await detect_kind(service, "uuid-x")
        assert kind == KIND_ENTITY_NODE

    async def test_detects_episodic_node(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_EPISODIC_NODE

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": ["Episodic"], "edge_type": None}], None, None,
        ))
        assert await detect_kind(service, "u") == KIND_EPISODIC_NODE

    async def test_detects_community_node(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_COMMUNITY_NODE

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": ["Community"], "edge_type": None}], None, None,
        ))
        assert await detect_kind(service, "u") == KIND_COMMUNITY_NODE

    async def test_detects_saga_node(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_SAGA_NODE

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": ["Saga"], "edge_type": None}], None, None,
        ))
        assert await detect_kind(service, "u") == KIND_SAGA_NODE

    async def test_detects_entity_edge(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_ENTITY_EDGE

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": [], "edge_type": "RELATES_TO"}], None, None,
        ))
        assert await detect_kind(service, "u") == KIND_ENTITY_EDGE

    async def test_detects_mentions_edge(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_EPISODIC_EDGE

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": [], "edge_type": "MENTIONS"}], None, None,
        ))
        assert await detect_kind(service, "u") == KIND_EPISODIC_EDGE

    async def test_unknown_uuid_returns_unknown(self):
        from pratyabhijna.tools.audit import detect_kind, KIND_UNKNOWN

        service = MagicMock()
        service._graphiti.driver.execute_query = AsyncMock(return_value=(
            [{"node_labels": None, "edge_type": None}], None, None,
        ))
        assert await detect_kind(service, "u") == KIND_UNKNOWN


class TestStampDispatch:
    async def test_node_results_use_node_cypher(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-N1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"N1","status":"Valid","analysis":"ok"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()
        queue = MagicMock()

        await process_results(
            client, "b",
            service=service, queue=queue,
            node_names={"N1": "n1"},
            object_kinds={"N1": "entity_node"},
        )

        cypher = service.execute_write_query.call_args_list[0].args[0]
        assert "MATCH (n {uuid: u})" in cypher  # node-shaped
        assert "()-[r" not in cypher  # not edge-shaped

    async def test_edge_results_use_edge_cypher(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-E1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"E1","status":"Valid","analysis":"ok"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()
        queue = MagicMock()

        await process_results(
            client, "b",
            service=service, queue=queue,
            node_names={"E1": "fact"},
            object_kinds={"E1": "entity_edge"},
        )

        cypher = service.execute_write_query.call_args_list[0].args[0]
        assert "()-[r {uuid: u}]->()" in cypher  # edge-shaped

    async def test_mixed_cohort_splits_node_and_edge_stamps(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-N1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"N1","status":"Valid","analysis":"ok"}',
                )]),
            )),
            MagicMock(custom_id="audit-E1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"E1","status":"Valid","analysis":"ok"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()

        await process_results(
            client, "b",
            service=service, queue=MagicMock(),
            node_names={"N1": "n", "E1": "e"},
            object_kinds={"N1": "entity_node", "E1": "entity_edge"},
        )

        # Two write calls: one node-shaped, one edge-shaped.
        assert service.execute_write_query.call_count == 2
        cyphers = [c.args[0] for c in service.execute_write_query.call_args_list]
        assert any("MATCH (n {uuid: u})" in c for c in cyphers)
        assert any("()-[r {uuid: u}]->()" in c for c in cyphers)


class TestNotesDispatch:
    async def test_entity_node_update_writes_node_notes(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-N1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"N1","status":"Update",'
                         '"analysis":"wrong type",'
                         '"request":"Change to Observation"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()

        await process_results(
            client, "b",
            service=service, queue=MagicMock(),
            node_names={"N1": "n"},
            object_kinds={"N1": "entity_node"},
        )

        # Two writes: stamp + notes; notes Cypher targets nodes
        assert service.execute_write_query.call_count == 2
        notes_cypher = service.execute_write_query.call_args_list[1].args[0]
        assert "MATCH (n:Entity {uuid: u.uuid})" in notes_cypher
        assert "n.notes" in notes_cypher

    async def test_entity_edge_update_writes_edge_notes(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-E1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"E1","status":"Update",'
                         '"analysis":"wrong group_id",'
                         '"request":"Set group_id=Vesper"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()

        await process_results(
            client, "b",
            service=service, queue=MagicMock(),
            node_names={"E1": "fact"},
            object_kinds={"E1": "entity_edge"},
        )

        # Two writes: stamp + notes; notes Cypher targets RELATES_TO edges
        assert service.execute_write_query.call_count == 2
        notes_cypher = service.execute_write_query.call_args_list[1].args[0]
        assert "[r:RELATES_TO {uuid: u.uuid}]" in notes_cypher
        assert "r.notes" in notes_cypher

    async def test_episodic_node_update_does_not_write_notes(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-EP1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"EP1","status":"Update",'
                         '"analysis":"bad source enum",'
                         '"request":"Set source=text"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()

        await process_results(
            client, "b",
            service=service, queue=MagicMock(),
            node_names={"EP1": "ep"},
            object_kinds={"EP1": "episodic_node"},
        )

        # Only one write: the audited_at stamp. Notes are skipped for
        # non-Entity kinds.
        assert service.execute_write_query.call_count == 1


class TestUnfixableEdgeBehavior:
    async def test_edge_unfixable_does_not_enqueue_thread(self):
        from pratyabhijna.tools.audit import process_results

        client = MagicMock()
        client.messages.batches.results = AsyncMock(return_value=_async_iter([
            MagicMock(custom_id="audit-E1", result=MagicMock(
                type="succeeded",
                message=MagicMock(content=[MagicMock(
                    type="text",
                    text='{"uuid":"E1","status":"Unfixable",'
                         '"analysis":"orphan edge — recommend deletion"}',
                )]),
            )),
        ]))
        service = MagicMock()
        service.execute_write_query = AsyncMock()
        queue = MagicMock()
        queue.enqueue = AsyncMock()

        await process_results(
            client, "b",
            service=service, queue=queue,
            node_names={"E1": "edge"},
            object_kinds={"E1": "entity_edge"},
        )

        # Edge Unfixable does NOT enqueue a Thread; only nodes do.
        queue.enqueue.assert_not_called()
