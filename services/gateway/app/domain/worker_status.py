"""
WorkerStatus — the control-plane view of a worker (Stage 7 part 2).

A plain, framework-free snapshot of one worker's health/metrics. It's what the
`WorkerControlClient` port returns and what the gateway serves at
`/v1/admin/worker/status` — kept separate from the generated protobuf type so the
domain never depends on gRPC (the adapter translates between the two).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkerStatus:
    worker_id: str
    uptime_seconds: float
    in_flight: int
    processed_total: int
    healthy: bool = True
