"""
Tests for the Jobs API — each one also documents a Stage-0 concept.

Read these as executable examples of how the endpoints behave. Run them with:
    cd services/gateway && pytest
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio  # every test in this file is async


async def test_create_and_fetch_job(client):
    # Create -> 201, status defaults to pending, version starts at 1.
    r = await client.post("/v1/jobs", json={"payload": {"task": "summarize"}})
    assert r.status_code == 201
    job = r.json()
    assert job["status"] == "pending"
    assert job["version"] == 1

    # Fetch it back by id.
    r2 = await client.get(f"/v1/jobs/{job['id']}")
    assert r2.status_code == 200
    assert r2.json()["id"] == job["id"]


async def test_unknown_job_is_404_with_envelope(client):
    r = await client.get("/v1/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    # The consistent error envelope, including a correlation id.
    body = r.json()
    assert body["error"]["code"] == "job_not_found"
    assert "correlation_id" in body["error"]


async def test_validation_error_is_422(client):
    # Empty payload violates the schema (min_length=1) -> 422 validation_error.
    r = await client.post("/v1/jobs", json={"payload": {}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_idempotency_key_replays_same_job(client):
    headers = {"Idempotency-Key": "abc-123"}
    r1 = await client.post("/v1/jobs", json={"payload": {"n": 1}}, headers=headers)
    r2 = await client.post("/v1/jobs", json={"payload": {"n": 2}}, headers=headers)

    assert r1.status_code == 201       # first call creates
    assert r2.status_code == 200       # second call REPLAYS (not created again)
    assert r1.json()["id"] == r2.json()["id"]   # same job, despite different body


async def test_legal_transition_bumps_version(client):
    job = (await client.post("/v1/jobs", json={"payload": {"x": 1}})).json()

    r = await client.post(
        f"/v1/jobs/{job['id']}/transition",
        json={"to": "running", "expected_version": 1},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["version"] == 2     # version advanced


async def test_illegal_transition_is_409(client):
    job = (await client.post("/v1/jobs", json={"payload": {"x": 1}})).json()
    # pending -> succeeded is not allowed by the state machine.
    r = await client.post(
        f"/v1/jobs/{job['id']}/transition",
        json={"to": "succeeded", "expected_version": 1},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_transition"


async def test_stale_version_is_conflict_409(client):
    job = (await client.post("/v1/jobs", json={"payload": {"x": 1}})).json()
    # Advance once (version 1 -> 2).
    await client.post(
        f"/v1/jobs/{job['id']}/transition",
        json={"to": "running", "expected_version": 1},
    )
    # Now try again with the STALE version 1 -> optimistic-concurrency conflict.
    r = await client.post(
        f"/v1/jobs/{job['id']}/transition",
        json={"to": "succeeded", "expected_version": 1},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_conflict"
