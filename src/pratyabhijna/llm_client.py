"""Anthropic LLM client with prompt-cache breakpoints.

Graphiti's entity attribute extraction fires one Anthropic call per
detected entity (Person, Event, Project, …) in parallel. Every call
sends the same large <MESSAGES> block — the sliding episode-history
window — but with a different tool schema and <ENTITY> payload.

Because the tool schemas differ, automatic prompt caching cannot detect
the shared prefix; it would only cache up to the first schema difference.
Explicit cache_control on the content block itself is independent of
tool-schema variance and puts the breakpoint exactly where we want it.

CachingAnthropicClient subclasses graphiti's AnthropicClient and
overrides _generate_response to:

  1. Wrap the system prompt as a cached content block.
  2. Split the user message content at </MESSAGES>, marking the prefix
     (task instructions + full episode context) as cache_control: ephemeral.
  3. Log cache hit/write token counts at DEBUG level for observability.

Calls without a </MESSAGES> tag (EdgeDuplicate deduplication calls,
etc.) pass through unchanged — their static prefixes are too small to
reach the cache minimum.
"""

from __future__ import annotations

import json
import logging
import typing
from typing import TYPE_CHECKING

from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.llm_client.errors import RateLimitError, RefusalError
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

if TYPE_CHECKING:
    import anthropic
    from anthropic.types import MessageParam
else:
    try:
        import anthropic
        from anthropic.types import MessageParam
    except ImportError:
        raise ImportError("anthropic package is required") from None

_log = logging.getLogger(__name__)

# XML closing tag that marks the end of the cacheable prefix in entity
# attribute extraction and summary prompts. Everything up to and including
# this tag is stable for a given add_episode() invocation; only the
# <ENTITY> block that follows varies per call.
_CACHE_SPLIT_TAG = "</MESSAGES>"


class CachingAnthropicClient(AnthropicClient):
    """AnthropicClient with explicit cache_control on the <MESSAGES> prefix."""

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
    ) -> tuple[dict[str, typing.Any], int, int]:
        system_message = messages[0]
        user_messages: list[dict] = [
            {"role": m.role, "content": m.content} for m in messages[1:]
        ]

        max_creation_tokens = self._resolve_max_tokens(max_tokens, self.model)

        # Wrap system prompt as a cached content block.
        system_param = [
            {
                "type": "text",
                "text": system_message.content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Split the first user message at </MESSAGES> if present.
        # Retry messages appended by the parent's retry loop come after
        # index 0 and should not be modified.
        if user_messages:
            user_messages[0] = _split_at_messages_tag(user_messages[0])

        user_messages_cast = typing.cast(list[MessageParam], user_messages)

        try:
            tools, tool_choice = self._create_tool(response_model)
            result = await self.client.messages.create(
                system=system_param,
                max_tokens=max_creation_tokens,
                temperature=self.temperature,
                messages=user_messages_cast,
                model=self.model,
                tools=tools,
                tool_choice=tool_choice,
            )

            input_tokens = 0
            output_tokens = 0
            if hasattr(result, "usage") and result.usage:
                input_tokens = getattr(result.usage, "input_tokens", 0) or 0
                output_tokens = getattr(result.usage, "output_tokens", 0) or 0
                cache_read = getattr(result.usage, "cache_read_input_tokens", 0) or 0
                cache_write = getattr(result.usage, "cache_creation_input_tokens", 0) or 0
                if cache_read or cache_write:
                    _log.debug(
                        "prompt cache: read=%d write=%d (input=%d)",
                        cache_read,
                        cache_write,
                        input_tokens,
                    )

            for content_item in result.content:
                if content_item.type == "tool_use":
                    if isinstance(content_item.input, dict):
                        tool_args: dict[str, typing.Any] = content_item.input
                    else:
                        tool_args = json.loads(str(content_item.input))
                    return tool_args, input_tokens, output_tokens

            for content_item in result.content:
                if content_item.type == "text":
                    return (
                        self._extract_json_from_text(content_item.text),
                        input_tokens,
                        output_tokens,
                    )
                else:
                    raise ValueError(
                        f"Could not extract structured data from model response: {result.content}"
                    )

            raise ValueError(
                f"Could not extract structured data from model response: {result.content}"
            )

        except anthropic.RateLimitError as e:
            raise RateLimitError(
                f"Rate limit exceeded. Please try again later. Error: {e}"
            ) from e
        except anthropic.APIError as e:
            if "refused to respond" in str(e).lower():
                raise RefusalError(str(e)) from e
            raise e
        except Exception as e:
            raise e


def _split_at_messages_tag(msg: dict) -> dict:
    """Split a user message at </MESSAGES>, marking the prefix as cached.

    If </MESSAGES> is present, returns a message whose content is a list
    of two text blocks: the prefix (with cache_control: ephemeral) and the
    suffix (uncached). If the tag is absent, returns the message unchanged.
    """
    content = msg.get("content", "")
    if not isinstance(content, str):
        return msg  # already structured content blocks; leave alone

    split_at = content.find(_CACHE_SPLIT_TAG)
    if split_at == -1:
        return msg

    split_end = split_at + len(_CACHE_SPLIT_TAG)
    prefix = content[:split_end]
    suffix = content[split_end:]

    blocks: list[dict] = [
        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
    ]
    if suffix:
        blocks.append({"type": "text", "text": suffix})

    return {"role": msg["role"], "content": blocks}
