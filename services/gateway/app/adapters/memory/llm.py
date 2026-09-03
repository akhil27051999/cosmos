"""
Scripted LLMClient — a fake model for tests.

It returns a pre-programmed list of `LLMTurn`s in order, ignoring the actual
messages. That lets a test drive the agent loop through any path — a direct
answer, a tool call then an answer, a refusal, a budget blow-out — with zero API
calls and full determinism. It records the calls it received so tests can assert
what the loop sent.
"""

from __future__ import annotations

from app.ports.llm import LLMTurn


class ScriptedLLMClient:
    def __init__(self, turns: list[LLMTurn]) -> None:
        self._turns = list(turns)
        self._i = 0
        self.calls: list[dict] = []   # what the loop asked for, for assertions

    async def create_turn(self, *, system, messages, tools, max_tokens) -> LLMTurn:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        turn = self._turns[self._i]
        self._i += 1
        return turn
