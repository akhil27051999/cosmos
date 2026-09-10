"""In-memory WorkerControlClient — a settable canned status, for tests/local."""

from __future__ import annotations

from app.domain.worker_status import WorkerStatus


class InMemoryWorkerControlClient:
    def __init__(self, status: WorkerStatus | None = None) -> None:
        self.status = status or WorkerStatus(
            worker_id="fake-worker", uptime_seconds=1.0,
            in_flight=0, processed_total=0, healthy=True,
        )

    async def get_status(self) -> WorkerStatus:
        return self.status
