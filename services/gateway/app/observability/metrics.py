"""
A tiny, dependency-free metrics registry that speaks the **Prometheus exposition
format** (Stage 10).

Why hand-rolled instead of `prometheus_client`: it keeps the learning build's
tests hermetic (no extra dependency to import at collection time) AND makes the
data model explicit — you can see exactly what a counter, a gauge, and a
histogram *are* and how they render on `/metrics`. A production deployment would
swap this for `prometheus_client` (or an OpenTelemetry SDK exporting to a
collector); the exposition format on the wire is identical, so scrapers don't care.

The three core metric types:
  * **Counter** — monotonically increasing (requests, errors). You rate() it.
  * **Gauge** — goes up and down (in-flight requests, queue depth).
  * **Histogram** — bucketed observations (latency); yields _bucket/_sum/_count,
    which is what powers p50/p95/p99 and the RED "duration" signal.

Each metric can carry labels (method, path, status). Values live in a dict keyed
by the label-tuple; a lock guards mutations since requests share the event loop
across threads in the ASGI server's worker pool.
"""

from __future__ import annotations

import threading
from typing import Iterable

# Default latency buckets (seconds) — the Prometheus client's defaults.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

_lock = threading.Lock()


def _escape(value: str) -> str:
    """Escape a label value per the exposition format (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_str(labelnames: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not labelnames:
        return ""
    inner = ",".join(f'{n}="{_escape(str(v))}"' for n, v in zip(labelnames, values))
    return "{" + inner + "}"


class _Metric:
    def __init__(self, name: str, help_text: str, labelnames: Iterable[str] = ()):
        self.name = name
        self.help = help_text
        self.labelnames = tuple(labelnames)

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        # Enforce the declared label set, in order, so the key is stable.
        return tuple(str(labels[n]) for n in self.labelnames)


class Counter(_Metric):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with _lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def collect(self) -> list[tuple[dict, float]]:
        """Every series as (labels dict, value) — used by the SLO computation."""
        return [(dict(zip(self.labelnames, key)), val) for key, val in self._values.items()]

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, val in sorted(self._values.items()):
            out.append(f"{self.name}{_labels_str(self.labelnames, key)} {val}")
        return out


class Gauge(_Metric):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with _lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def set(self, value: float, **labels: str) -> None:
        with _lock:
            self._values[self._key(labels)] = value

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for key, val in sorted(self._values.items()):
            out.append(f"{self.name}{_labels_str(self.labelnames, key)} {val}")
        return out


class Histogram(_Metric):
    def __init__(self, *a, buckets: Iterable[float] = DEFAULT_BUCKETS, **k):
        super().__init__(*a, **k)
        self._buckets = tuple(sorted(buckets))
        # per label-key: {bucket_upper: cumulative_count}, plus _sum and _count
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sum: dict[tuple[str, ...], float] = {}
        self._count: dict[tuple[str, ...], int] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with _lock:
            counts = self._counts.setdefault(key, [0] * len(self._buckets))
            # Increment every bucket whose upper bound >= value -> counts stay
            # cumulative, which is exactly what the _bucket{le=...} series needs.
            for i, upper in enumerate(self._buckets):
                if value <= upper:
                    counts[i] += 1
            self._sum[key] = self._sum.get(key, 0.0) + value
            self._count[key] = self._count.get(key, 0) + 1

    def collect(self) -> list[dict]:
        """Per-series {labels, buckets:{le: cumulative_count}, count, sum} — the
        SLO computation reads cumulative bucket counts to get 'fraction under X'."""
        out = []
        for key, counts in self._counts.items():
            out.append({
                "labels": dict(zip(self.labelnames, key)),
                "buckets": {b: counts[i] for i, b in enumerate(self._buckets)},
                "count": self._count[key],
                "sum": self._sum[key],
            })
        return out

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key in sorted(self._count):
            counts = self._counts[key]
            base_labels = list(zip(self.labelnames, key))
            for i, upper in enumerate(self._buckets):
                le_labels = base_labels + [("le", _fmt_float(upper))]
                out.append(f"{self.name}_bucket{_kv(le_labels)} {counts[i]}")
            inf_labels = base_labels + [("le", "+Inf")]
            out.append(f"{self.name}_bucket{_kv(inf_labels)} {self._count[key]}")
            out.append(f"{self.name}_sum{_labels_str(self.labelnames, key)} {self._sum[key]}")
            out.append(f"{self.name}_count{_labels_str(self.labelnames, key)} {self._count[key]}")
        return out


def _fmt_float(v: float) -> str:
    return repr(v)


def _kv(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{n}="{_escape(str(v))}"' for n, v in pairs) + "}"


class Registry:
    def __init__(self) -> None:
        self._metrics: list[_Metric] = []

    def register(self, metric: _Metric):
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        lines: list[str] = []
        for m in self._metrics:
            lines.extend(m.render())  # type: ignore[attr-defined]
        return "\n".join(lines) + "\n"


# --- the global registry + the platform's metrics -------------------------

REGISTRY = Registry()

http_requests_total = REGISTRY.register(
    Counter("http_requests_total", "Total HTTP requests.",
            ("method", "path", "status"))
)
http_request_duration_seconds = REGISTRY.register(
    Histogram("http_request_duration_seconds", "HTTP request latency (seconds).",
              ("method", "path"))
)
http_requests_in_flight = REGISTRY.register(
    Gauge("http_requests_in_flight", "In-flight HTTP requests.", ("method",))
)
jobs_submitted_total = REGISTRY.register(
    Counter("jobs_submitted_total", "Jobs accepted at POST /v1/jobs.")
)


def render_latest() -> str:
    """The body served at GET /metrics."""
    return REGISTRY.render()
