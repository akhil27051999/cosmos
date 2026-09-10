"""
gRPC adapter for the WorkerControl contract — the REAL client + server.

This module is **runtime-only**: it imports `grpc` and the generated protobuf
stubs, which exist only after codegen (run automatically in the Docker build, or
manually via the `grpc_tools.protoc` command in `proto/worker_control.proto`).
Nothing in the test path imports this file — the gateway's provider imports it
lazily, and the worker imports it inside `run_worker()` — so `pytest` never needs
gRPC installed. Same convention as the Redpanda/Mongo/Anthropic adapters.

The generated files (`worker_control_pb2*.py`) import each other by bare module
name, so we put their directory on `sys.path` before importing them — the
simplest, package-layout-independent way to consume protoc's Python output.
"""

from __future__ import annotations

import os
import sys

from app.domain.worker_status import WorkerStatus


def _load_stubs():
    """Import the generated pb2 modules (adding their dir to sys.path first)."""
    gen_dir = os.path.join(os.path.dirname(__file__), "gen")
    if gen_dir not in sys.path:
        sys.path.insert(0, gen_dir)
    try:
        import worker_control_pb2 as pb          # type: ignore
        import worker_control_pb2_grpc as pbg    # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - only if codegen skipped
        raise RuntimeError(
            "gRPC stubs not generated. Run grpc_tools.protoc on proto/worker_control.proto "
            "(the Docker build does this automatically)."
        ) from exc
    return pb, pbg


# ---- client side (used by the gateway) ----------------------------------

class GrpcWorkerControlClient:
    """Calls a worker's WorkerControl service over gRPC. Channel is lazy so the
    gateway can start before the worker is reachable."""

    def __init__(self, target: str) -> None:
        self._target = target
        self._channel = None
        self._stub = None

    async def _ensure(self):
        if self._stub is None:
            import grpc  # lazy
            _pb, pbg = _load_stubs()
            self._channel = grpc.aio.insecure_channel(self._target)
            self._stub = pbg.WorkerControlStub(self._channel)

    async def get_status(self) -> WorkerStatus:
        await self._ensure()
        pb, _pbg = _load_stubs()
        reply = await self._stub.GetStatus(pb.GetStatusRequest())
        return WorkerStatus(
            worker_id=reply.worker_id,
            uptime_seconds=reply.uptime_seconds,
            in_flight=reply.in_flight,
            processed_total=reply.processed_total,
            healthy=reply.healthy,
        )


# ---- server side (used by the worker) -----------------------------------

async def serve_worker_control(port: int, snapshot):
    """Start an async gRPC server exposing WorkerControl. `snapshot` is a
    zero-arg callable returning a live `WorkerStatus`. Returns the running server
    (call `await server.stop(grace)` to shut down)."""
    import grpc  # lazy
    pb, pbg = _load_stubs()

    class _Servicer(pbg.WorkerControlServicer):
        async def GetStatus(self, request, context):
            st = snapshot()
            return pb.GetStatusReply(
                worker_id=st.worker_id,
                uptime_seconds=st.uptime_seconds,
                in_flight=st.in_flight,
                processed_total=st.processed_total,
                healthy=st.healthy,
            )

    server = grpc.aio.server()
    pbg.add_WorkerControlServicer_to_server(_Servicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    await server.start()
    return server
