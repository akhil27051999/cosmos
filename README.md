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
| 2 | Polyglot persistence (MongoDB trajectories, Redis hot state) | ⬜ |
| 3 | The agent loop (Claude + tools + RAG) with SSE streaming | ⬜ |
| 4 | Resilience: rate-limit, circuit breaker, retry+jitter, fallback, load-shed | ⬜ |
| … | Kafka/CQRS/Saga → multi-tenancy → gRPC/Go/mesh → k8s operator → frontend/A-B → preview envs → cloud/multi-region → observability/SRE | ⬜ |

## Quick start (Stage 0)

```bash
cd services/gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                       # fast, no database needed
docker compose up --build    # gateway + worker + Postgres + Redis → http://localhost:8000/docs
```

> Local-only. Not a git repo — initialize and commit on your own when you're
> ready to push.
