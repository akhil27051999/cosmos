"""
Deterministic variant assignment — the heart of the A/B logic.

Given an experiment and a unit id (a tenant), pick a variant by **hashing**, not
by rolling a die or storing a decision. Properties this buys us:

  * **Deterministic + sticky:** same (experiment key, unit, weights) -> same
    variant, every time, on every replica, with zero stored state.
  * **Independent across experiments:** the experiment key is mixed into the hash,
    so a unit's bucket in experiment A is uncorrelated with its bucket in B.
  * **Weighted:** the hash maps to a point in [0, total_weight); walking the
    variants by cumulative weight gives each its configured share.

Pure function — trivially unit-tested (and we assert the split is stable and
roughly proportional).
"""

from __future__ import annotations

import hashlib

from app.domain.experiment import Experiment, Variant


def bucket_of(experiment_key: str, unit_id: str, modulo: int) -> int:
    """Map (experiment, unit) to a stable integer in [0, modulo).

    We take the first 8 hex chars (32 bits) of a SHA-256 over "key:unit" — plenty
    of range for weight totals — and mod into the bucket space.
    """
    digest = hashlib.sha256(f"{experiment_key}:{unit_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def assign(experiment: Experiment, unit_id: str) -> Variant:
    """Return the variant this unit belongs to.

    Not running (draft/paused/completed) -> control (first variant), so toggling
    an experiment off cleanly sends everyone to the baseline. Running -> weighted
    deterministic bucket.
    """
    if not experiment.variants:
        raise ValueError(f"experiment '{experiment.key}' has no variants")
    if not experiment.is_running:
        return experiment.control

    total = experiment.total_weight
    b = bucket_of(experiment.key, unit_id, total)
    cumulative = 0
    for variant in experiment.variants:
        cumulative += variant.weight
        if b < cumulative:
            return variant
    return experiment.variants[-1]   # float-safe fallback (shouldn't be reached)
