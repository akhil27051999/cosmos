"""
Groq adapter for the LLMClient port — a FREE alternative model provider.

This is the ports-and-adapters payoff in action: the agent loop, worker, and
tests don't change at all; we just add another implementation of `LLMClient`.
Groq's API is OpenAI-compatible and has a generous free tier (no credit card),
so it's ideal for running the live agent without paying for Anthropic.

The loop speaks in Anthropic-ish shapes (its `messages` list and the
`{name, description, input_schema}` tool defs), so this adapter TRANSLATES:
  * Anthropic-style messages  -> OpenAI-style chat messages
  * Anthropic tool defs       -> OpenAI "function" tools
  * OpenAI tool_calls in the response -> our neutral `ToolCall`s
The translation helpers are pure functions (below), so they're unit-tested
without the Groq SDK installed. The SDK itself is imported lazily.
"""

from __future__ import annotations

import json

from app.ports.llm import LLMTurn, ToolCall


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool def -> OpenAI 'function' tool."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the loop's running conversation into OpenAI chat messages.

    - a user string stays a user message
    - an assistant entry holds the raw dict WE stashed last turn (already
      OpenAI-shaped) -> pass it through
    - a user entry whose content is a list is the loop's Anthropic tool_result
      blocks -> emit one OpenAI `role: "tool"` message per block
    """
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "user" and isinstance(content, str):
            out.append({"role": "user", "content": content})
        elif role == "user" and isinstance(content, list):
            for block in content:
                out.append({
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": str(block["content"]),
                })
        elif role == "assistant" and isinstance(content, dict):
            out.append(content)                     # our stashed OpenAI assistant msg
        elif role == "assistant":
            out.append({"role": "assistant", "content": content or ""})
    return out


def turn_from_openai(message, usage) -> LLMTurn:
    """Map an OpenAI/Groq chat completion message + usage into our LLMTurn."""
    tool_calls: list[ToolCall] = []
    raw_tool_calls = getattr(message, "tool_calls", None) or []
    for tc in raw_tool_calls:
        tool_calls.append(
            ToolCall(id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments or "{}"))
        )

    # What we append back next turn (OpenAI assistant message shape).
    raw_assistant: dict = {"role": "assistant", "content": message.content or ""}
    if raw_tool_calls:
        raw_assistant["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in raw_tool_calls
        ]

    return LLMTurn(
        text=message.content,
        tool_calls=tool_calls,
        stop_reason="tool_use" if tool_calls else "end_turn",
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        raw_assistant_content=raw_assistant,
    )


class GroqLLMClient:
    def __init__(self, model: str, api_key: str | None = None) -> None:
        from groq import AsyncGroq  # lazy: keep the SDK out of test imports

        self._client = AsyncGroq(api_key=api_key) if api_key else AsyncGroq()
        self._model = model

    async def create_turn(
        self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int
    ) -> LLMTurn:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=to_openai_messages(system, messages),
            tools=to_openai_tools(tools) or None,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        return turn_from_openai(choice.message, resp.usage)
