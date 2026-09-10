"""
Process lifecycle state (Stage 12) — graceful shutdown / draining.

Kubernetes shuts a pod down by sending SIGTERM, waiting `terminationGracePeriod`,
then SIGKILL. During that window we want to **drain**: stop accepting new traffic
but let in-flight work finish. Flipping this flag makes `/readyz` report
not-ready, so the Service/load balancer pulls the pod out of rotation while the
process finishes current requests and disposes resources — no dropped requests,
no restart loop.
"""

from __future__ import annotations

_draining = False


def begin_draining() -> None:
    """Enter the draining state (called from the app's shutdown hook)."""
    global _draining
    _draining = True


def is_draining() -> bool:
    return _draining


def reset_draining() -> None:
    """Test helper — clear the flag between cases."""
    global _draining
    _draining = False
