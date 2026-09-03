"""
Anthropic adapter for the LLMClient port — the only place that touches the SDK.

It makes one Messages API call per turn and normalises the response into our
provider-neutral `LLMTurn`: pull out the assistant text and any `tool_use`
blocks, keep the raw content to replay on the next turn, and report token usage.

Model + key come from config; the SDK reads `ANTHROPIC_API_KEY` from the env by
default. We default to `claude-opus-5` (per Anthropic guidance) but it's
configurable — set `AGENT_MODEL=claude-sonnet-5` (or haiku) to cut cost.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

from app.ports.llm import LLMTurn, ToolCall


class AnthropicLLMClient:
    def __init__(self, model: str, api_key: str | None = None) -> None:
        # If api_key is None the SDK falls back to ANTHROPIC_API_KEY / profiles.
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self._model = model

    async def create_turn(
        self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int
    ) -> LLMTurn:
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )

        text: str | None = None
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text = (text or "") + block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))

        return LLMTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            # Append the raw content blocks back verbatim next turn — this
            # preserves tool_use blocks (and any thinking blocks) correctly.
            raw_assistant_content=resp.content,
        )
