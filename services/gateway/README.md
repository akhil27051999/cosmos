# Cosmos Gateway — Stage 0

The FastAPI rewrite of the platform's front door, built with **clean
(hexagonal) architecture**. This is the foundation the async core (Stage 1) and
everything after it sits on.

## What Stage 0 demonstrates

| Concept | Where to read it |
|---|---|
| Hexagonal layering (domain → ports → adapters → api) | the folder layout below |
| Domain entity + **state machine** | `app/domain/job.py` |
| **Ports & adapters** (swap Postgres ↔ in-memory) | `app/ports/`, `app/adapters/` |
| **Idempotency** (safe retries) | `create_job` in `app/api/routes_jobs.py` + the unique constraint in `models.py` |
| **Optimistic concurrency (OCC)** | `transition_job` + `PostgresJobRepository.update` |
| Consistent **error envelope** + domain→HTTP mapping | `app/api/errors.py` |
| **Correlation IDs** | `app/core/correlation.py` |
| **12-factor config** | `app/core/config.py` |
| Liveness vs **readiness** probes | `app/api/routes_health.py` |

## Reading order (for learning)

1. `app/domain/job.py` — the pure business object + rules
2. `app/domain/exceptions.py`
3. `app/ports/job_repository.py` — the interface the app depends on
4. `app/adapters/memory/repository.py` — the port, implemented simply
5. `app/adapters/postgres/{models,repository}.py` — the same, against a real DB
6. `app/api/{schemas,deps,routes_jobs,routes_health,errors}.py` — the web edge
7. `app/core/{config,db,correlation}.py` — the plumbing
8. `app/main.py` — where it's all wired together
9. `tests/test_jobs.py` — executable examples of every behaviour

## Run the tests (no database needed)

```bash
cd services/gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Run the app (with Postgres, via Docker)

```bash
cd services/gateway
docker compose up --build
# open http://localhost:8000/docs  (interactive API)
```

## Try it by hand

```bash
# create a job
curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' \
     -d '{"payload":{"task":"summarize"}}'

# create idempotently — run twice, get the SAME job back
curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' \
     -H 'Idempotency-Key: demo-1' -d '{"payload":{"task":"x"}}'

# move it pending -> running (version 1 -> 2)
curl -s -XPOST localhost:8000/v1/jobs/<ID>/transition \
     -H 'content-type: application/json' \
     -d '{"to":"running","expected_version":1}'
```

## What this deliberately does NOT do yet

No queue/worker, no event log, no Mongo/Redis, no auth. Those are Stage 1+.
Stage 0 is only the clean, correct, testable foundation.
