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
    assert {"uuid", "name", "status", "analysis"} <= set(props)
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
