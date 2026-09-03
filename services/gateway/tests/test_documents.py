"""
Tests for the MongoDB document layer (payload + result) — run against the
in-memory document store, so no Mongo is needed.
"""

from __future__ import annotations

import pytest

from app.domain.job import JobStatus
from app.worker import ProcessResult, process_job

pytestmark = pytest.mark.asyncio


async def test_submit_stores_payload_in_document_store(env):
    payload = {"task": "summarize", "url": "https://example.com"}
    r = await env.client.post("/v1/jobs", json={"payload": payload})
    job_id = r.json()["id"]

    # The payload went to the document store (Mongo), not the jobs row.
    r2 = await env.client.get(f"/v1/jobs/{job_id}/document")
    assert r2.status_code == 200
    body = r2.json()
    assert body["payload"] == payload
    assert body["result"] is None            # no result until the worker runs


async def test_worker_writes_result_document(env):
    job, _ = await env.service.submit({"n": 21}, None)

    # A fast stand-in for the real work: read payload, write a result document.
    async def work(j):
        doc = await env.docs.get(j.id)
        await env.docs.set_result(j.id, {"echo": doc.payload, "processed": True})

    result = await process_job(
        job.id, repo=env.repo, events=env.events, work_fn=work
    )

    assert result is ProcessResult.SUCCEEDED
    assert (await env.repo.get(job.id)).status == JobStatus.SUCCEEDED
    doc = await env.docs.get(job.id)
    assert doc.result == {"echo": {"n": 21}, "processed": True}


async def test_document_404_for_unknown_job(env):
    r = await env.client.get("/v1/jobs/00000000-0000-0000-0000-000000000000/document")
    assert r.status_code == 404
