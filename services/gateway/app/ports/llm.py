"""
The LLMClient port — the boundary between our agent loop and *a* language model.

Why a port here matters more than anywhere else: the real adapter calls the
Anthropic API, which costs money and needs an API key. By depending on this
interface, our agent loop and worker are fully testable with a **scripted fake**
that returns canned turns — no key, no network, no cost. Swapping providers
later is also just a new adapter.

One `create_turn` call = one round-trip to the model. The agent loop
(app/agent/loop.py) calls it repeatedly, feeding tool results back in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolCall:
    """The model asked to run a tool with these arguments."""

    id: str        # the model's id for this call; the tool_result must echo it
    name: str
    input: dict


@dataclass(frozen=True)
class LLMTurn:
    """The result of one model turn."""

    text: str | None                 # any assistant text emitted this turn
    tool_calls: list[ToolCall]       # tools the model wants run (may be empty)
    stop_reason: str                 # "end_turn" | "tool_use" | "max_tokens" | "refusal"
    input_tokens: int
    output_tokens: int
    # Opaque assistant content to append back to the conversation before the next
    # call (for Anthropic this is the raw content blocks, incl. any thinking).
    raw_assistant_content: object


class LLMClient(Protocol):
    async def create_turn(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
    ) -> LLMTurn:
        ...
