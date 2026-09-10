# Cosmos Platform

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
│   └── gateway/          # the API front door (FastAPI, hexagonal architecture)
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
| … | Go worker + service mesh → Saga/CDC → k8s operator → frontend/A-B → preview envs → cloud/multi-region → observability/SRE | ⬜ |

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
