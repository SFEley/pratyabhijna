"""Anthropic LLM client with prompt-cache breakpoints.

Graphiti's entity attribute extraction fires one Anthropic call per
detected entity (Person, Event, Project, …) in parallel. Every call
sends the same large <MESSAGES> block — the sliding episode-history
window — but with a different tool schema and <ENTITY> payload.

Anthropic matches cached prefixes left-to-right through tools → system
→ messages. If tools[] differs per call, the prefix diverges before the
shared MESSAGES block is reached, defeating any cache_control marker
placed downstream. To make caching work across parallel entity-attribute
calls, we send the *same* tools[] — every entity-type schema — on every
call, and use tool_choice (which is not part of the cached prefix) to
select the one the model should actually emit.

CachingAnthropicClient subclasses graphiti's AnthropicClient and:

  1. Emits a stable, deterministic tools[] containing every entity-type
     model registered via `shared_tool_models`, whenever the current
     response_model is one of them. Other response_models fall through
     to the parent's single-tool behavior.
  2. Wraps the system prompt as a cached content block.
  3. Splits the user message content at the last </MESSAGES>, marking
     the prefix (task instructions + full episode context) as
     cache_control: ephemeral.
  4. Logs cache hit/write token counts at DEBUG level for observability.
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

    def __init__(
        self,
        *args: typing.Any,
        shared_tool_models: typing.Iterable[type[BaseModel]] | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Sort by name so the emitted tools[] is byte-identical across
        # calls, giving the prompt cache a stable prefix to match.
        self._shared_tool_models: tuple[type[BaseModel], ...] = tuple(
            sorted(shared_tool_models or (), key=lambda m: m.__name__)
        )
        self._shared_tool_names: frozenset[str] = frozenset(
            m.__name__ for m in self._shared_tool_models
        )

    def _create_tool(
        self,
        response_model: type[BaseModel] | None = None,
        force_specific: bool = False,
    ) -> tuple[list[typing.Any], typing.Any]:
        if (
            response_model is not None
            and response_model.__name__ in self._shared_tool_names
        ):
            tools: list[dict[str, typing.Any]] = [
                {
                    "name": m.__name__,
                    "description": m.model_json_schema().get(
                        "description", f"Extract {m.__name__} information"
                    ),
                    "input_schema": m.model_json_schema(),
                }
                for m in self._shared_tool_models
            ]
            # Use {"type": "any"} so tool_choice is identical across
            # parallel entity-attribute calls; Anthropic invalidates the
            # messages cache when tool_choice varies. The model picks
            # from context (the <ENTITY> block names the type). On a
            # mismatch, _generate_response retries with force_specific=True.
            tool_choice: dict[str, typing.Any] = (
                {"type": "tool", "name": response_model.__name__}
                if force_specific
                else {"type": "any"}
            )
            return typing.cast(list[typing.Any], tools), typing.cast(
                typing.Any, tool_choice
            )
        return super()._create_tool(response_model)

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

        has_split = (
            user_messages
            and isinstance(user_messages[0].get("content"), list)
        )
        _log.debug(
            "CachingAnthropicClient._generate_response: model=%s split=%s",
            self.model,
            has_split,
        )

        def _read_usage(res: typing.Any) -> tuple[int, int]:
            if not (hasattr(res, "usage") and res.usage):
                return 0, 0
            inp = getattr(res.usage, "input_tokens", 0) or 0
            out = getattr(res.usage, "output_tokens", 0) or 0
            rd = getattr(res.usage, "cache_read_input_tokens", 0) or 0
            wr = getattr(res.usage, "cache_creation_input_tokens", 0) or 0
            _log.debug(
                "prompt cache: read=%d write=%d (input=%d output=%d)",
                rd, wr, inp, out,
            )
            return inp, out

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

            input_tokens, output_tokens = _read_usage(result)

            # If tool_choice was "any" and the model picked the wrong
            # tool, retry with the specific tool_choice forced. This
            # path costs a cache miss on the retry but is the safety
            # valve that makes the "any" optimization acceptable.
            expected = response_model.__name__ if response_model else None
            if (
                expected is not None
                and expected in self._shared_tool_names
            ):
                picked = next(
                    (c.name for c in result.content if c.type == "tool_use"),
                    None,
                )
                if picked is not None and picked != expected:
                    _log.warning(
                        "tool_choice=any returned %s but expected %s; retrying forced",
                        picked, expected,
                    )
                    tools_f, choice_f = self._create_tool(
                        response_model, force_specific=True
                    )
                    result = await self.client.messages.create(
                        system=system_param,
                        max_tokens=max_creation_tokens,
                        temperature=self.temperature,
                        messages=user_messages_cast,
                        model=self.model,
                        tools=tools_f,
                        tool_choice=choice_f,
                    )
                    retry_in, retry_out = _read_usage(result)
                    input_tokens += retry_in
                    output_tokens += retry_out

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

    Uses the LAST occurrence of </MESSAGES> so that memories or episode
    text mentioning the tag literally don't cause the split to happen
    mid-array. The real closing tag of the messages block is always the
    last one in the prompt.

    If </MESSAGES> is present, returns a message whose content is a list
    of two text blocks: the prefix (with cache_control: ephemeral) and the
    suffix (uncached). If the tag is absent, returns the message unchanged.
    """
    content = msg.get("content", "")
    if not isinstance(content, str):
        return msg  # already structured content blocks; leave alone

    split_at = content.rfind(_CACHE_SPLIT_TAG)
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
