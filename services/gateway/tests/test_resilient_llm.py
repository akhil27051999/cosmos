"""
Tests for ResilientLLMClient — the decorator that adds retry, circuit breaking,
and fallback around any LLMClient. Driven by flaky fakes; no real provider.
"""

from __future__ import annotations

import pytest

from app.adapters.resilient.llm import BreakerConfig, ResilientLLMClient
from app.ports.llm import LLMTurn
from app.resilience.retry import RetryPolicy

pytestmark = pytest.mark.asyncio

_KW = dict(system="s", messages=[], tools=[], max_tokens=16)


def _turn(text: str = "ok") -> LLMTurn:
    return LLMTurn(text=text, tool_calls=[], stop_reason="end_turn",
                   input_tokens=1, output_tokens=1, raw_assistant_content=None)


async def _noop_sleep(_d):
    return None


class ScriptedLLM:
    """Returns/raises the next scripted item on each call; counts calls."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls = 0

    async def create_turn(self, **kwargs) -> LLMTurn:
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_retries_then_succeeds():
    primary = ScriptedLLM([TimeoutError(), TimeoutError(), _turn("done")])
    client = ResilientLLMClient(
        primary, retry=RetryPolicy(max_attempts=5, base_delay=0.1),
        sleep=_noop_sleep, rand=lambda: 0.0,
    )
    turn = await client.create_turn(**_KW)
    assert turn.text == "done"
    assert primary.calls == 3               # two transient failures, then success


async def test_falls_back_to_secondary_when_primary_exhausted():
    primary = ScriptedLLM([TimeoutError(), TimeoutError(), TimeoutError()])  # 3 attempts, all fail
    fallback = ScriptedLLM([_turn("from-fallback")])
    client = ResilientLLMClient(
        primary, fallback, retry=RetryPolicy(max_attempts=3, base_delay=0.1),
        sleep=_noop_sleep, rand=lambda: 0.0,
    )
    turn = await client.create_turn(**_KW)
    assert turn.text == "from-fallback"
    assert primary.calls == 3 and fallback.calls == 1


async def test_breaker_opens_and_fast_fails_without_calling_provider():
    class AlwaysFail:
        def __init__(self) -> None:
            self.calls = 0

        async def create_turn(self, **kwargs):
            self.calls += 1
            raise TimeoutError("down")

    primary = AlwaysFail()
    client = ResilientLLMClient(
        primary, retry=RetryPolicy(max_attempts=1, base_delay=0.0),
        breaker=BreakerConfig(failure_threshold=2, reset_timeout=100.0),
        sleep=_noop_sleep, rand=lambda: 0.0,
    )

    # two real failures trip the breaker (threshold=2)
    for _ in range(2):
        with pytest.raises(Exception):
            await client.create_turn(**_KW)
    assert primary.calls == 2

    # breaker now OPEN -> next call fast-fails WITHOUT touching the provider
    with pytest.raises(Exception):
        await client.create_turn(**_KW)
    assert primary.calls == 2               # provider was not called again
