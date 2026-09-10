"""
Stage 10 — observability tests: the metrics registry, the /metrics endpoint,
RED metrics recorded by the middleware, and structured JSON logging.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.observability.logging import JsonFormatter, configure_logging
from app.observability.metrics import (
    Counter,
    Histogram,
    http_requests_total,
    jobs_submitted_total,
)

pytestmark = pytest.mark.asyncio


# ---- the registry (pure) ------------------------------------------------

async def test_counter_increments_and_renders():
    c = Counter("demo_total", "demo", ("kind",))
    c.inc(kind="a")
    c.inc(2, kind="a")
    c.inc(kind="b")
    assert c.value(kind="a") == 3
    text = "\n".join(c.render())
    assert "# TYPE demo_total counter" in text
    assert 'demo_total{kind="a"} 3' in text


async def test_histogram_buckets_sum_and_count():
    h = Histogram("lat_seconds", "latency", ("route",), buckets=(0.1, 0.5, 1.0))
    for v in (0.05, 0.2, 0.7, 3.0):
        h.observe(v, route="/x")
    lines = h.render()
    text = "\n".join(lines)
    # cumulative buckets: <=0.1 →1, <=0.5 →2, <=1.0 →3, +Inf →4
    assert 'lat_seconds_bucket{route="/x",le="0.1"} 1' in text
    assert 'lat_seconds_bucket{route="/x",le="0.5"} 2' in text
    assert 'lat_seconds_bucket{route="/x",le="+Inf"} 4' in text
    assert 'lat_seconds_count{route="/x"} 4' in text
    # sum ≈ 3.95 (parse to avoid float-repr fragility)
    sum_line = next(l for l in lines if l.startswith("lat_seconds_sum"))
    assert float(sum_line.rsplit(" ", 1)[1]) == pytest.approx(3.95)


async def test_label_values_are_escaped():
    c = Counter("esc_total", "h", ("p",))
    c.inc(p='a"b\\c')
    assert 'esc_total{p="a\\"b\\\\c"} 1' in "\n".join(c.render())


# ---- /metrics endpoint + RED middleware ---------------------------------

async def test_metrics_endpoint_exposes_prometheus_text(env):
    r = await env.client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    assert "# TYPE http_requests_total counter" in body
    assert "http_request_duration_seconds_bucket" in body


async def test_request_is_counted_by_route_template(env):
    before = http_requests_total.value(method="GET", path="/v1/jobs", status="200")
    await env.client.get("/v1/jobs")                      # authed via default fixture key
    after = http_requests_total.value(method="GET", path="/v1/jobs", status="200")
    assert after == before + 1                            # labelled by TEMPLATE, not raw URL


async def test_jobs_submitted_counter_increments(env):
    before = jobs_submitted_total.value()
    await env.client.post("/v1/jobs", json={"payload": {"x": 1}})
    assert jobs_submitted_total.value() == before + 1


# ---- structured logging -------------------------------------------------

async def test_json_formatter_emits_valid_json_with_correlation_id():
    rec = logging.makeLogRecord({
        "name": "access", "levelno": logging.INFO, "levelname": "INFO",
        "msg": "request", "args": (),
    })
    rec.http_status = 200
    line = JsonFormatter().format(rec)
    obj = json.loads(line)                                # must be valid JSON
    assert obj["level"] == "INFO"
    assert obj["logger"] == "access"
    assert obj["msg"] == "request"
    assert obj["http_status"] == 200
    assert "correlation_id" in obj


async def test_configure_logging_is_idempotent():
    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    # exactly one handler, no duplicates stacked
    assert len(logging.getLogger().handlers) == 1
