"""
The agent loop — plan → act (tool call) → observe → repeat.

This is the heart of the platform. Given a task, it calls the model; if the
model asks for a tool, it runs the tool and feeds the result back; it repeats
until the model produces a final answer or a guardrail stops it. Every step is
recorded into a **trajectory** and emitted via `on_step` (which the worker wires
to the SSE stream) so you can watch the agent think in real time.

Guardrails (why an agent needs them):
  * **token budget** — a hard ceiling on total tokens across the loop, so a
    runaway agent can't burn unlimited spend.
  * **max steps** — a ceiling on iterations, so it can't loop forever.
  * **cooperative cancellation** — an optional `should_cancel` check between
    steps, so a cancelled job stops promptly.

The loop is provider-agnostic: it talks to the `LLMClient` port, so tests drive
it with a scripted fake (no API key, no cost).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.agent.tools import Tool
from app.ports.llm import LLMClient

DEFAULT_SYSTEM = (
    "You are Cosmos, a helpful task-running agent. Think step by step. "
    "When a calculation or tool can help, call the tool rather than guessing. "
    "When you have the answer, state it plainly."
)


@dataclass
class AgentResult:
    answer: str | None                 # final text, or None if a guardrail stopped it
    trajectory: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)   # {input, output, total}
    stopped: str = "done"              # done | budget | max_steps | refusal | cancelled


async def run_agent(
    task: str,
    *,
    llm: LLMClient,
    tools: list[Tool],
    system: str = DEFAULT_SYSTEM,
    max_steps: int = 8,
    token_budget: int = 20_000,
    max_tokens: int = 4096,
    on_step: Callable[[dict], Awaitable[None]] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
) -> AgentResult:
    tool_defs = [t.definition() for t in tools]
    tool_map = {t.name: t for t in tools}

    messages: list[dict] = [{"role": "user", "content": task}]
    trajectory: list[dict] = []
    total_in = total_out = 0

    async def emit(step: dict) -> None:
        trajectory.append(step)
        if on_step is not None:
            await on_step(step)

    def usage() -> dict:
        return {"input": total_in, "output": total_out, "total": total_in + total_out}

    for _ in range(max_steps):
        # cooperative cancellation — check before spending another model call
        if should_cancel is not None and await should_cancel():
            await emit({"type": "cancelled"})
            return AgentResult(None, trajectory, usage(), "cancelled")

        turn = await llm.create_turn(
            system=system, messages=messages, tools=tool_defs, max_tokens=max_tokens
        )
        total_in += turn.input_tokens
        total_out += turn.output_tokens

        if turn.text:
            await emit({"type": "assistant", "text": turn.text})

        if turn.stop_reason == "refusal":
            await emit({"type": "refusal"})
            return AgentResult(None, trajectory, usage(), "refusal")

        # No tool calls => the model is done.
        if turn.stop_reason != "tool_use" or not turn.tool_calls:
            await emit({"type": "final", "text": turn.text})
            return AgentResult(turn.text, trajectory, usage(), "done")

        # Tool use: append the assistant turn, run each tool, feed results back.
        messages.append({"role": "assistant", "content": turn.raw_assistant_content})
        tool_results = []
        for call in turn.tool_calls:
            await emit({"type": "tool_call", "id": call.id, "name": call.name, "input": call.input})
            tool = tool_map.get(call.name)
            if tool is None:
                output, is_error = f"Error: unknown tool '{call.name}'", True
            else:
                try:
                    output, is_error = await tool.run(call.input), False
                except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
                    output, is_error = f"Error: {exc}", True
            await emit({"type": "tool_result", "id": call.id, "name": call.name,
                        "output": output, "is_error": is_error})
            block = {"type": "tool_result", "tool_use_id": call.id, "content": output}
            if is_error:
                block["is_error"] = True
            tool_results.append(block)
        messages.append({"role": "user", "content": tool_results})

        # Budget guardrail — checked after a full turn so we never start another.
        if total_in + total_out >= token_budget:
            await emit({"type": "budget_exceeded", "usage": usage()})
            return AgentResult(None, trajectory, usage(), "budget")

    await emit({"type": "max_steps"})
    return AgentResult(None, trajectory, usage(), "max_steps")
