"""
Agent-loop + tools tests — driven by the scripted fake LLM, so no API key,
no network, no cost, fully deterministic.
"""

from __future__ import annotations

import pytest

from app.adapters.memory.llm import ScriptedLLMClient
from app.agent.loop import run_agent
from app.agent.tools import DEFAULT_TOOLS, calculator
from app.ports.llm import LLMTurn, ToolCall

pytestmark = pytest.mark.asyncio


def _final(text: str) -> LLMTurn:
    return LLMTurn(text=text, tool_calls=[], stop_reason="end_turn",
                   input_tokens=5, output_tokens=5, raw_assistant_content=[])


def _tool(call_id: str, name: str, tool_input: dict, tokens: int = 5) -> LLMTurn:
    return LLMTurn(text=None, tool_calls=[ToolCall(id=call_id, name=name, input=tool_input)],
                   stop_reason="tool_use", input_tokens=tokens, output_tokens=tokens,
                   raw_assistant_content=[])


# ---- the calculator tool ----

async def test_calculator_computes():
    assert await calculator.run({"expression": "2 + 2 * 3"}) == "8"
    assert await calculator.run({"expression": "(21 * 2)"}) == "42"


async def test_calculator_rejects_unsafe_input():
    # not arithmetic -> a safe error string, never code execution
    out = await calculator.run({"expression": "__import__('os').system('ls')"})
    assert out.startswith("Error:")


# ---- the agent loop ----

async def test_direct_answer_no_tools():
    llm = ScriptedLLMClient([_final("The answer is 4.")])
    result = await run_agent("What is 2+2?", llm=llm, tools=DEFAULT_TOOLS)

    assert result.stopped == "done"
    assert result.answer == "The answer is 4."
    assert result.trajectory[-1]["type"] == "final"


async def test_uses_tool_then_answers():
    # turn 1: the model calls the calculator; turn 2: it answers with the result
    llm = ScriptedLLMClient([
        _tool("t1", "calculator", {"expression": "21 * 2"}),
        _final("42"),
    ])
    result = await run_agent("Double 21.", llm=llm, tools=DEFAULT_TOOLS)

    assert result.stopped == "done"
    assert result.answer == "42"
    kinds = [s["type"] for s in result.trajectory]
    assert "tool_call" in kinds and "tool_result" in kinds
    tool_result = next(s for s in result.trajectory if s["type"] == "tool_result")
    assert tool_result["output"] == "42"          # the tool actually ran
    assert result.usage["total"] > 0


async def test_token_budget_stops_the_loop():
    # one tool turn that already blows the tiny budget -> stop with "budget"
    llm = ScriptedLLMClient([_tool("t1", "calculator", {"expression": "1+1"}, tokens=50)])
    result = await run_agent("x", llm=llm, tools=DEFAULT_TOOLS, token_budget=10)

    assert result.stopped == "budget"
    assert result.answer is None
    assert result.trajectory[-1]["type"] == "budget_exceeded"


async def test_refusal_is_handled():
    llm = ScriptedLLMClient([LLMTurn(text=None, tool_calls=[], stop_reason="refusal",
                                     input_tokens=1, output_tokens=0, raw_assistant_content=[])])
    result = await run_agent("something disallowed", llm=llm, tools=DEFAULT_TOOLS)

    assert result.stopped == "refusal"
    assert result.answer is None


async def test_unknown_tool_is_reported_not_crashed():
    llm = ScriptedLLMClient([
        _tool("t1", "does_not_exist", {"foo": 1}),
        _final("done anyway"),
    ])
    result = await run_agent("x", llm=llm, tools=DEFAULT_TOOLS)

    tr = next(s for s in result.trajectory if s["type"] == "tool_result")
    assert tr["is_error"] is True
    assert "unknown tool" in tr["output"]
