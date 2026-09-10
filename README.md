# Cosmos Platform

[![CI](https://github.com/akhil27051999/cosmos/actions/workflows/ci.yml/badge.svg)](https://github.com/akhil27051999/cosmos/actions/workflows/ci.yml)

A cloud-native, event-driven **LLM job-orchestration platform**, built from
scratch one runnable stage at a time. Clients submit jobs; the platform accepts
them instantly, routes them to healthy providers under limits, runs agent
workloads on a worker fleet, and persists every state change — growing into a
multi-tenant, observable, multi-region system.

This is a **learning build**: each stage is a working increment with clear
concepts, and every piece comes with steps to **test** and **troubleshoot**
(see [`docs/STAGES.md`](docs/STAGES.md)).

## Layout

```
cosmos-platform/
├── services/
│   ├── gateway/          # the API front door + worker (FastAPI, hexagonal architecture)
│   └── operator/         # Kubernetes operator: reconciles CosmosTenant CRs (Stage 8 pt2)
├── deploy/
│   └── helm/cosmos/      # Kubernetes Helm chart for the whole platform (Stage 8)
├── docs/
│   └── STAGES.md         # what each stage adds + test/troubleshoot steps
└── README.md
```

As later stages arrive, `services/` grows (e.g. a `worker/`), and a top-level
`docker-compose.yaml` will run the whole local stack together.

## Stage index

| Stage | Adds | Status |
|-------|------|--------|
| **0** | FastAPI + clean (hexagonal) architecture; Jobs API; idempotency; optimistic concurrency; error envelope; health probes | ✅ built |
| **1** | Accept-fast (`202 + id`); Redis-Streams queue + worker; state machine driven by the worker; append-only event log; at-least-once + idempotent; cancellation | ✅ built |
| **2** | Polyglot persistence: MongoDB for the job document (payload + result), Redis hot-state cache (cache-aside + invalidate-on-write) | ✅ built |
| **3** | The agent layer: real Claude agent loop (tool use + token budget), trajectory persisted to Mongo, live SSE streaming | ✅ built |
| **4** | Resilience: `ResilientLLMClient` decorator (timeout → retry+backoff/jitter → circuit breaker → fallback provider) + distributed token-bucket rate limit and edge load-shedding (429) | ✅ built |
| **5** | Event-driven backbone: **transactional outbox** + relay → Redpanda/Kafka; **CQRS** projector builds a read model (`/v1/stats`, `/v1/jobs/{id}/view`), idempotent under at-least-once. Saga & CDC are later increments. | 🟢 pts 1–2 |
| **6** | Multi-tenancy & identity: **API-key auth** (hashed keys), **tenant isolation** on every read/write, per-tenant **monthly quota** + usage ledger, admin bootstrap to mint tenants/keys. | ✅ built |
| **7** | **OAuth2/OIDC** JWT bearer tokens as a second credential + **scope-based authZ** (`jobs:read`/`jobs:write`); **typed gRPC control-plane** contract (protobuf) between gateway and worker (`/v1/admin/worker/status`), behind a port so tests need no gRPC. | 🟢 pts 1–2 |
| **8** | **Kubernetes**: a Helm chart (gateway + worker + backing stores, ConfigMap/Secret, probes, gateway **HPA**) **plus a custom operator** — a `CosmosTenant` **CRD** reconciled by a `kopf` controller that provisions tenants via the admin API and writes credentials to a Secret. | 🟢 pts 1–2 |
| **9** | **A/B experimentation & feature flags**: deterministic, sticky, weighted variant assignment (hash-based, no stored state); exposure tracking; admin-managed experiments; `/v1/experiments/{key}/assignment` + `/results`. | ✅ built |
| **10** | **Observability & SRE**: structured JSON logs with correlation IDs, **RED metrics** middleware (rate/errors/duration by route template), a Prometheus `/metrics` endpoint (dependency-free exposition-format registry). | ✅ built |
| **11** | **Saga / distributed transactions**: a saga orchestrator (forward steps + **compensations in reverse** on failure, persisted for crash-recovery), applied to a **tenant-onboarding** workflow spanning identity + experiments. | ✅ built |
| **12** | **Capstone — reliability & release**: **SLOs + error budgets** (`/v1/admin/slo`) computed from the metrics, **graceful shutdown** (draining readiness + pool disposal; worker finishes in-flight on SIGTERM), **chaos** fault injection, and an async **load-test** script. | ✅ built |

**All 12 stages built.** Later hardening (Go worker + service mesh, CDC, distributed tracing, preview envs, multi-region) is noted in `docs/ENGINEERING-NOTES.md §8`.

## Quick start (Stage 0)

```bash
cd services/gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                       # fast, no database needed
docker compose up --build    # gateway + worker + Postgres + Redis + MongoDB → http://localhost:8000/docs
```

> Local-only. Not a git repo — initialize and commit on your own when you're
> ready to push.
