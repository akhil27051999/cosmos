"""
SLOs & error budgets (Stage 12) — turning Stage-10 metrics into the numbers an
SRE actually manages to.

An **SLO** (Service Level Objective) is a target for a **SLI** (indicator). We
compute two classic ones from the metrics we already collect:

  * **Availability** = successful requests / total (we count 5xx as failures;
    4xx are client errors, excluded — a bad request isn't the service failing).
  * **Latency**      = fraction of requests faster than a threshold (from the
    duration histogram's cumulative buckets — the same data a p95 comes from).

The **error budget** is the flip side of the target: if the availability SLO is
99%, you're *allowed* 1% failures. `error_budget_remaining` = how much of that
allowance is left (1.0 = untouched, 0.0 = spent). Burning the budget is what
gates releases / triggers a freeze in SRE practice — and Stage 12's chaos
injector lets you watch it burn.

Pure function over the registry, so it's unit-testable by driving requests.
"""

from __future__ import annotations

from app.observability.metrics import (
    http_request_duration_seconds,
    http_requests_total,
)


def _budget_remaining(actual: float, target: float) -> float:
    """Fraction of the error budget still available.

    allowed = 1 - target (the tolerated bad fraction). consumed = actual-bad /
    allowed-bad. remaining = 1 - consumed, clamped to [0, 1].
    """
    allowed = 1.0 - target
    if allowed <= 0:
        return 1.0 if actual >= 1.0 else 0.0
    consumed = (1.0 - actual) / allowed
    return max(0.0, min(1.0, 1.0 - consumed))


def compute_slo(
    availability_target: float,
    latency_threshold_seconds: float,
    latency_target: float,
) -> dict:
    # --- availability ---
    total = 0.0
    errors = 0.0
    for labels, val in http_requests_total.collect():
        total += val
        if str(labels.get("status", "")).startswith("5"):
            errors += val
    availability = 1.0 if total == 0 else 1.0 - (errors / total)

    # --- latency (fraction under the threshold) ---
    observed = 0.0
    good = 0.0
    for series in http_request_duration_seconds.collect():
        observed += series["count"]
        # cumulative count at the largest bucket boundary <= threshold
        under = [le for le in series["buckets"] if le <= latency_threshold_seconds]
        if under:
            good += series["buckets"][max(under)]
    latency_ok = 1.0 if observed == 0 else good / observed

    return {
        "window": "process-lifetime",
        "requests": int(total),
        "availability": {
            "sli": "non-5xx / total",
            "target": availability_target,
            "actual": round(availability, 6),
            "error_budget_remaining": round(_budget_remaining(availability, availability_target), 6),
        },
        "latency": {
            "sli": f"fraction of requests <= {latency_threshold_seconds}s",
            "threshold_seconds": latency_threshold_seconds,
            "target": latency_target,
            "actual": round(latency_ok, 6),
            "error_budget_remaining": round(_budget_remaining(latency_ok, latency_target), 6),
        },
    }
