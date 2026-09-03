"""
Groq adapter translation tests — the pure Anthropic<->OpenAI mapping functions.
No Groq SDK or network needed (the SDK is imported lazily inside the client).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.adapters.groq.llm import to_openai_messages, to_openai_tools, turn_from_openai


def test_tools_translate_to_openai_function_shape():
    anthropic_tools = [{
        "name": "calculator",
        "description": "do math",
        "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}},
    }]
    out = to_openai_tools(anthropic_tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "do math",
            "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
        },
    }]


def test_messages_translate_across_the_conversation():
    # a realistic running conversation the loop would build:
    stashed_assistant = {  # what the Groq adapter stashed as raw_assistant_content
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression":"21*2"}'}}],
    }
    messages = [
        {"role": "user", "content": "double 21"},
        {"role": "assistant", "content": stashed_assistant},          # a prior tool-call turn
        {"role": "user", "content": [                                 # the loop's tool_result blocks
            {"type": "tool_result", "tool_use_id": "c1", "content": "42"}
        ]},
    ]

    out = to_openai_messages("SYS", messages)

    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "double 21"}
    assert out[2] is stashed_assistant                               # passed through
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "42"}


def test_turn_parsed_from_openai_response():
    message = SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(
            id="c1",
            function=SimpleNamespace(name="calculator", arguments='{"expression":"21*2"}'),
        )],
    )
    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=7)

    turn = turn_from_openai(message, usage)

    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls[0].name == "calculator"
    assert turn.tool_calls[0].input == {"expression": "21*2"}        # parsed from JSON
    assert turn.input_tokens == 12 and turn.output_tokens == 7
