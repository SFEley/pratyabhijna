"""Tests for CachingAnthropicClient and _split_at_messages_tag."""

import pytest

from pratyabhijna.llm_client import _split_at_messages_tag


# --- _split_at_messages_tag ---


def test_split_inserts_cache_control_on_prefix():
    content = "task instructions\n<MESSAGES>\nepisode data\n</MESSAGES>\n\n<ENTITY>\n{}\n</ENTITY>"
    result = _split_at_messages_tag({"role": "user", "content": content})

    assert isinstance(result["content"], list)
    assert len(result["content"]) == 2

    prefix_block = result["content"][0]
    suffix_block = result["content"][1]

    assert prefix_block["type"] == "text"
    assert prefix_block["cache_control"] == {"type": "ephemeral"}
    assert prefix_block["text"].endswith("</MESSAGES>")

    assert suffix_block["type"] == "text"
    assert "cache_control" not in suffix_block
    assert "<ENTITY>" in suffix_block["text"]


def test_split_prefix_contains_full_messages_block():
    content = "preamble\n<MESSAGES>\ndata\n</MESSAGES>\nremainder"
    result = _split_at_messages_tag({"role": "user", "content": content})

    prefix = result["content"][0]["text"]
    suffix = result["content"][1]["text"]

    assert "preamble" in prefix
    assert "<MESSAGES>" in prefix
    assert "data" in prefix
    assert "</MESSAGES>" in prefix
    assert "remainder" in suffix
    assert "</MESSAGES>" not in suffix


def test_split_no_tag_returns_unchanged():
    msg = {"role": "user", "content": "no messages tag here"}
    result = _split_at_messages_tag(msg)
    assert result is msg


def test_split_empty_suffix_omits_second_block():
    content = "preamble</MESSAGES>"
    result = _split_at_messages_tag({"role": "user", "content": content})

    assert isinstance(result["content"], list)
    assert len(result["content"]) == 1
    assert result["content"][0]["text"] == content


def test_split_leaves_structured_content_alone():
    msg = {"role": "user", "content": [{"type": "text", "text": "already structured"}]}
    result = _split_at_messages_tag(msg)
    assert result is msg


def test_split_uses_last_messages_tag():
    """An inner </MESSAGES> in memory text must not trigger the split early."""
    content = (
        "preamble\n<MESSAGES>\nepisode mentioning </MESSAGES> literally\n"
        "more episode text\n</MESSAGES>\n<ENTITY>{}</ENTITY>"
    )
    result = _split_at_messages_tag({"role": "user", "content": content})

    prefix = result["content"][0]["text"]
    suffix = result["content"][1]["text"]

    assert prefix.count("</MESSAGES>") == 2
    assert "more episode text" in prefix
    assert "<ENTITY>" in suffix
    assert "</MESSAGES>" not in suffix


def test_split_preserves_role():
    content = "before</MESSAGES>after"
    result = _split_at_messages_tag({"role": "user", "content": content})
    assert result["role"] == "user"


# --- CachingAnthropicClient integration ---


@pytest.mark.asyncio
async def test_caching_client_uses_cached_system_and_split_content(monkeypatch):
    """_generate_response sends a cached system block and splits at </MESSAGES>."""
    from unittest.mock import AsyncMock, MagicMock

    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.prompts.models import Message

    from pratyabhijna.llm_client import CachingAnthropicClient

    client = CachingAnthropicClient(
        config=LLMConfig(api_key="test-key", model="claude-haiku-4-5-latest"),
    )

    # Stub the underlying Anthropic API client
    fake_result = MagicMock()
    fake_result.usage.input_tokens = 100
    fake_result.usage.output_tokens = 10
    fake_result.usage.cache_read_input_tokens = 0
    fake_result.usage.cache_creation_input_tokens = 80
    tool_content = MagicMock()
    tool_content.type = "tool_use"
    tool_content.input = {"key": "value"}
    fake_result.content = [tool_content]

    api_mock = AsyncMock(return_value=fake_result)
    client.client = MagicMock()
    client.client.messages.create = api_mock

    messages = [
        Message(role="system", content="system instructions"),
        Message(
            role="user",
            content="task preamble\n<MESSAGES>\nepisode data\n</MESSAGES>\n\n<ENTITY>{}</ENTITY>",
        ),
    ]

    await client._generate_response(messages)

    _, kwargs = api_mock.await_args
    system = kwargs["system"]
    msgs = kwargs["messages"]

    # System should be a cached content block list
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "system instructions"

    # First user message should be split into two blocks
    user_content = msgs[0]["content"]
    assert isinstance(user_content, list)
    assert user_content[0]["cache_control"] == {"type": "ephemeral"}
    assert user_content[0]["text"].endswith("</MESSAGES>")
    assert "cache_control" not in user_content[1]
    assert "<ENTITY>" in user_content[1]["text"]


def test_create_tool_emits_shared_tools_list_when_response_model_is_shared():
    """Shared-tool calls send the full registry, identical across entity types."""
    from graphiti_core.llm_client.config import LLMConfig
    from pydantic import BaseModel

    from pratyabhijna.llm_client import CachingAnthropicClient

    class Person(BaseModel):
        notes: str | None = None

    class Drive(BaseModel):
        notes: str | None = None

    class Event(BaseModel):
        notes: str | None = None

    client = CachingAnthropicClient(
        config=LLMConfig(api_key="test-key", model="claude-haiku-4-5-20251001"),
        shared_tool_models=[Person, Drive, Event],
    )

    tools_for_person, choice_person = client._create_tool(Person)
    tools_for_drive, choice_drive = client._create_tool(Drive)

    # Identical tools[] regardless of which model was requested.
    assert tools_for_person == tools_for_drive
    assert [t["name"] for t in tools_for_person] == ["Drive", "Event", "Person"]

    # Last tool carries a cache_control breakpoint so tools[] can cache
    # independently of system drift.
    assert tools_for_person[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools_for_person[0]

    # tool_choice is identical ({"type": "any"}) so Anthropic's message
    # cache isn't invalidated by a per-call choice change.
    assert choice_person == {"type": "any"}
    assert choice_drive == {"type": "any"}

    # force_specific=True pins to the named tool for mismatch retries.
    _, forced = client._create_tool(Person, force_specific=True)
    assert forced == {"type": "tool", "name": "Person"}


def test_create_tool_falls_through_for_non_shared_models():
    """Models outside the shared set get the parent's single-tool behavior."""
    from graphiti_core.llm_client.config import LLMConfig
    from pydantic import BaseModel

    from pratyabhijna.llm_client import CachingAnthropicClient

    class Person(BaseModel):
        notes: str | None = None

    class EdgeDuplicate(BaseModel):
        duplicate_facts: list[int] = []

    client = CachingAnthropicClient(
        config=LLMConfig(api_key="test-key", model="claude-haiku-4-5-20251001"),
        shared_tool_models=[Person],
    )

    tools, choice = client._create_tool(EdgeDuplicate)
    assert len(tools) == 1
    assert tools[0]["name"] == "EdgeDuplicate"
    assert choice == {"type": "tool", "name": "EdgeDuplicate"}


@pytest.mark.asyncio
async def test_caching_client_no_split_when_no_messages_tag(monkeypatch):
    """Messages without </MESSAGES> are sent as-is (EdgeDuplicate calls etc.)."""
    from unittest.mock import AsyncMock, MagicMock

    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.prompts.models import Message

    from pratyabhijna.llm_client import CachingAnthropicClient

    client = CachingAnthropicClient(
        config=LLMConfig(api_key="test-key", model="claude-haiku-4-5-latest"),
    )

    fake_result = MagicMock()
    fake_result.usage.input_tokens = 50
    fake_result.usage.output_tokens = 5
    fake_result.usage.cache_read_input_tokens = 0
    fake_result.usage.cache_creation_input_tokens = 0
    tool_content = MagicMock()
    tool_content.type = "tool_use"
    tool_content.input = {"result": "ok"}
    fake_result.content = [tool_content]

    api_mock = AsyncMock(return_value=fake_result)
    client.client = MagicMock()
    client.client.messages.create = api_mock

    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="no messages tag here"),
    ]

    await client._generate_response(messages)

    _, kwargs = api_mock.await_args
    # User content should be a plain string, not split into blocks
    assert kwargs["messages"][0]["content"] == "no messages tag here"
