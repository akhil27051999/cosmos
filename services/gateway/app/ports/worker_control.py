"""
The WorkerControl PORT — the gateway's view of "talk to a worker".

The gateway depends on this Protocol, not on gRPC. The real adapter
(`adapters/grpc/worker_control.py`) speaks gRPC to a worker; the in-memory
adapter (`adapters/memory/worker_control.py`) returns a canned status, so tests
and local runs need no gRPC at all — the same ports payoff as every other
external dependency in this codebase.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.worker_status import WorkerStatus


class WorkerControlClient(Protocol):
    async def get_status(self) -> WorkerStatus:
        """Fetch a worker's live health + metrics (a control-plane RPC)."""
        ...
