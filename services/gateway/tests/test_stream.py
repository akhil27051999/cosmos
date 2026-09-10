"""
StreamBus + end-to-end worker/agent streaming — all in-memory, no infra.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.adapters.memory.llm import ScriptedLLMClient
from app.adapters.memory.stream_bus import InMemoryStreamBus
from app.agent.loop import run_agent
from app.agent.tools import DEFAULT_TOOLS
from app.domain.job import JobStatus
from app.ports.llm import LLMTurn, ToolCall
from app.worker import ProcessResult, process_job

pytestmark = pytest.mark.asyncio


async def test_bus_fans_out_to_a_subscriber():
    bus = InMemoryStreamBus()
    job_id = uuid.uuid4()

    async def collect():
        received = []
        async for event in bus.subscribe(job_id):
            received.append(event)
        return received

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)                      # let the subscriber register

    await bus.publish(job_id, {"type": "assistant", "text": "hi"})
    await bus.publish(job_id, {"type": "end"})  # terminal event ends the stream

    received = await task
    assert received == [{"type": "assistant", "text": "hi"}, {"type": "end"}]


async def test_worker_runs_agent_streams_and_stores_trajectory(env):
    # a job to process
    job, _ = await env.service.submit({"prompt": "double 21"}, None, env.tenant_id)

    # scripted agent: call the calculator, then answer
    llm = ScriptedLLMClient([
        LLMTurn(text=None, tool_calls=[ToolCall("t1", "calculator", {"expression": "21*2"})],
                stop_reason="tool_use", input_tokens=5, output_tokens=5, raw_assistant_content=[]),
        LLMTurn(text="42", tool_calls=[], stop_reason="end_turn",
                input_tokens=5, output_tokens=5, raw_assistant_content=[]),
    ])

    # collect the live stream while the job runs
    async def collect():
        received = []
        async for event in env.bus.subscribe(job.id):
            received.append(event)
        return received

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)

    async def work(j):
        result = await run_agent(
            "double 21", llm=llm, tools=DEFAULT_TOOLS,
            on_step=lambda step: env.bus.publish(j.id, step),
        )
        await env.docs.set_result(j.id, {"answer": result.answer, "trajectory": result.trajectory})
        await env.bus.publish(j.id, {"type": "end", "stopped": result.stopped})

    outcome = await process_job(job.id, repo=env.repo, events=env.events, work_fn=work)
    streamed = await collector

    # job completed and the result document holds the answer
    assert outcome is ProcessResult.SUCCEEDED
    assert (await env.repo.get(job.id)).status == JobStatus.SUCCEEDED
    assert (await env.docs.get(job.id)).result["answer"] == "42"

    # the client saw the agent's steps live, ending with an "end" event
    kinds = [e["type"] for e in streamed]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert streamed[-1]["type"] == "end"
