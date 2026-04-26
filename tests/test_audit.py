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
    # Block 1: instructions, 5min cache (default TTL)
    assert "INSTRUCTIONS TEXT" in blocks[1]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}


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
        node={"uuid": "N1", "name": "test", "labels": ["Entity"], "summary": "..."},
        episodes=[{"uuid": "E1", "content": "ep body"}],
        recall_results={"nodes": [], "edges": []},
    )
    assert msg["role"] == "user"
    blocks = msg["content"]
    assert len(blocks) == 3
    # Block 0: episode (cached)
    assert "ep body" in blocks[0]["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # Block 1: recall (no cache)
    assert "cache_control" not in blocks[1]
    # Block 2: node (no cache)
    assert "N1" in blocks[2]["text"]
    assert "cache_control" not in blocks[2]


def test_user_message_handles_multiple_episodes():
    msg = build_user_message(
        node={"uuid": "N1", "name": "x"},
        episodes=[
            {"uuid": "E1", "content": "first"},
            {"uuid": "E2", "content": "second"},
        ],
        recall_results={"nodes": [], "edges": []},
    )
    text = msg["content"][0]["text"]
    assert "first" in text
    assert "second" in text


from pratyabhijna.tools.audit import build_audit_request, AUDIT_RESPONSE_SCHEMA


def test_audit_response_schema_has_required_fields():
    props = AUDIT_RESPONSE_SCHEMA["properties"]
    # name is sourced from the input node, not echoed by the model
    assert {"uuid", "status", "analysis"} <= set(props)
    assert "name" not in props
    assert AUDIT_RESPONSE_SCHEMA["properties"]["status"]["enum"] == [
        "Valid", "Update", "Unfixable"
    ]


def test_build_audit_request_returns_batch_request_shape():
    req = build_audit_request(
        custom_id="audit-N1",
        node={"uuid": "N1", "name": "x"},
        episodes=[{"uuid": "E1", "content": "c"}],
        recall_results={"nodes": [], "edges": []},
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
from pratyabhijna.tools.audit import resolve_node_list


async def test_resolve_uuid_list_skips_query():
    service = MagicMock()
    uuids = await resolve_node_list(
        "550e8400-e29b-41d4-a716-446655440000", service=service,
    )
    assert uuids == ["550e8400-e29b-41d4-a716-446655440000"]


async def test_resolve_natural_language_extracts_uuids_from_query_response(monkeypatch):
    async def fake_query(service, request, **kwargs):
        # The augmented prompt should be passed through
        assert "find all Position nodes" in request
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
    service = MagicMock()
    uuids = await resolve_node_list("find all Position nodes", service=service)
    assert uuids == [
        "550e8400-e29b-41d4-a716-446655440000",
        "660e8400-e29b-41d4-a716-446655440001",
    ]


async def test_resolve_deduplicates_uuids(monkeypatch):
    """If the query response mentions a UUID twice, return it once."""
    async def fake_query(service, request, **kwargs):
        return {
            "response": (
                "Node 550e8400-e29b-41d4-a716-446655440000 exists. "
                "(550e8400-e29b-41d4-a716-446655440000 again, for emphasis.)"
            ),
            "cypher_log": [],
            "refused": False,
            "iterations": 1,
        }
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    service = MagicMock()
    uuids = await resolve_node_list("any prompt", service=service)
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
    uuids = await resolve_node_list("find unicorns", service=service)
    assert uuids == []


async def test_resolve_returns_empty_on_refusal(monkeypatch):
    """When the query agent refuses (no Cypher executed), resolve returns []."""
    async def fake_query(service, request, **kwargs):
        return {"response": "", "refused": True, "cypher_log": [], "iterations": 1}
    monkeypatch.setattr("pratyabhijna.tools.audit._call_query", fake_query)
    uuids = await resolve_node_list("find anything", service=MagicMock())
    assert uuids == []


from unittest.mock import AsyncMock
from pratyabhijna.tools.audit import submit_audit_batch


async def test_submit_audit_batch_strips_internal_keys_and_returns_id():
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
    # Sort-by-episode runs before strip — n2 (E1) should come before n1 (E2)
    assert submitted[0]["custom_id"] == "n2"
    assert submitted[1]["custom_id"] == "n1"


from pratyabhijna.tools.audit import poll_batch


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
