"""
The experimentation domain — A/B tests & feature flags (Stage 9).

An `Experiment` splits a population into weighted `Variant`s. Given a stable
"unit" (here a tenant id), assignment is **deterministic and sticky**: the same
unit always lands in the same variant for a given experiment, with no state
stored — the bucket is computed by hashing (see `app/experiments/bucketing.py`).
That's what lets many gateway replicas agree on a unit's variant without
coordinating, and what makes an A/B test measurable (a unit's experience is
consistent across requests).

Pure domain: no hashing, no DB, no HTTP here — just what an experiment *is* and
its rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExperimentStatus(str, Enum):
    DRAFT = "draft"          # created, not assigning yet -> everyone gets control
    RUNNING = "running"      # actively bucketing units into variants
    PAUSED = "paused"        # temporarily control-only
    COMPLETED = "completed"  # finished; control-only


@dataclass
class Variant:
    """One arm of an experiment. `weight` sets its share of traffic; `config`
    carries whatever the variant changes (e.g. {"model": "claude-sonnet-5"})."""

    name: str
    weight: int = 1
    config: dict = field(default_factory=dict)


@dataclass
class Experiment:
    key: str
    description: str = ""
    status: ExperimentStatus = ExperimentStatus.DRAFT
    variants: list[Variant] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)

    @property
    def total_weight(self) -> int:
        return sum(v.weight for v in self.variants)

    @property
    def is_running(self) -> bool:
        return self.status is ExperimentStatus.RUNNING

    @property
    def control(self) -> Variant:
        """The default arm — the first variant. Units get this whenever the
        experiment isn't actively running (draft/paused/completed)."""
        if not self.variants:
            raise ValueError("experiment has no variants")
        return self.variants[0]


@dataclass(frozen=True)
class Assignment:
    """The resolved variant for one unit — what the API returns and what an
    exposure records."""

    experiment_key: str
    unit_id: str
    variant: str
    config: dict
