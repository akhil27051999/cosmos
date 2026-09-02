# Stages — build log, test steps & troubleshooting

One section per stage: what it adds, how to **test** it, and how to
**troubleshoot** the common problems. Read the code alongside this.

---

## Stage 0 — FastAPI + clean (hexagonal) architecture ✅

**What it adds:** the platform's API front door as a `Job` resource, built so
business rules stay independent of frameworks.

- **Domain** (`app/domain/`): the `Job` entity + a guarded **state machine**. Pure Python.
- **Ports** (`app/ports/`): the `JobRepository` interface the app depends on.
- **Adapters** (`app/adapters/`): `postgres/` (real DB) and `memory/` (used by tests) — same interface.
- **API** (`app/api/`): the Jobs endpoints, request/response schemas, DI, health probes, and the domain→HTTP error mapping.
- **Core** (`app/core/`): 12-factor config, lazy DB engine, correlation-ID middleware.

**Endpoints**

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/jobs` | create a job (idempotent via `Idempotency-Key` header) |
| GET | `/v1/jobs/{id}` | fetch one |
| GET | `/v1/jobs` | list a page |
| POST | `/v1/jobs/{id}/transition` | change status (optimistic concurrency) |
| GET | `/healthz`, `/readyz` | liveness / readiness |

### Test steps

**A. Unit/API tests (no database — seconds)**
```bash
cd services/gateway
python -m venv .venv && source .venv/bin/activate   # first time only
pip install -r requirements.txt                      # first time only
pytest -v
```
Expect **7 passed**. These run against the in-memory adapter, so no Postgres is needed.

**B. Run the real app (with Postgres via Docker)**
```bash
cd services/gateway
docker compose up --build
# open the interactive API docs:
open http://localhost:8000/docs
```

**C. Exercise it by hand (in another terminal)**
```bash
# health
curl -s localhost:8000/healthz ; echo
curl -s localhost:8000/readyz  ; echo

# create a job  -> 201, status "pending", version 1
curl -si -XPOST localhost:8000/v1/jobs \
  -H 'content-type: application/json' -d '{"payload":{"task":"summarize"}}'

# idempotency: run this twice -> SAME job id; 2nd response is 200 not 201
curl -si -XPOST localhost:8000/v1/jobs \
  -H 'content-type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"payload":{"task":"x"}}'

# transition pending -> running (grab <ID> from a create response)
curl -si -XPOST localhost:8000/v1/jobs/<ID>/transition \
  -H 'content-type: application/json' -d '{"to":"running","expected_version":1}'
```

**What to verify (the learning checkpoints)**
- Empty payload `{"payload":{}}` → **422** with `{"error":{"code":"validation_error",...}}`.
- Unknown id → **404** `job_not_found`.
- `pending → succeeded` directly → **409** `invalid_transition` (state machine).
- Re-run a transition with an old `expected_version` → **409** `version_conflict` (optimistic concurrency).
- Every response carries an `X-Request-ID` header (correlation id).

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `ModuleNotFoundError: No module named 'app'` when running pytest | Run from `services/gateway/` (pytest.ini sets `pythonpath=.`). Or `PYTHONPATH=. pytest`. |
| `pip install` fails building **asyncpg** | You're on a very new Python (3.13/3.14) without a prebuilt wheel. Use **Docker** (image is Python 3.12), or make a 3.12 venv. Tests themselves don't need asyncpg. |
| `docker compose up` → gateway exits with a DB connection error | The gateway must reach Postgres by the compose service name. Confirm `DATABASE_URL` host is **`postgres`** (compose), not `localhost`. |
| `/readyz` returns **503** `database_unreachable` | Postgres isn't up/reachable yet. `docker compose logs postgres`; wait for the healthcheck; check the URL. Liveness `/healthz` should still be 200. |
| Port already in use (`8000` or `5432`) | Something else is bound. Stop it, or change the left-hand port in `docker-compose.yaml` (e.g. `8001:8000`). |
| Transition returns **409** unexpectedly | That's the design: either the move is illegal for the current state, or your `expected_version` is stale. GET the job to read its current `status` and `version`. |
| Code changes not reflected in the container | Rebuild: `docker compose up --build` (Stage 0 has no hot-reload in the image). For live reload, run locally with `uvicorn app.main:app --reload`. |

---

## Stage 1 — async core ✅

**What it adds:** the platform stops doing work inline. Submitting a job now
**accepts** it (persist + enqueue) and returns **202 + id** immediately; a
separate **worker** process does the work later and drives the job through its
lifecycle. Every state change is appended to an **event log**.

**New concepts (and where they live)**

| Concept | Where |
|---|---|
| Accept-fast (`202 + id`) | `submit_job` in `app/api/routes_jobs.py` |
| Application service (use cases in one place) | `app/application/job_service.py` |
| Durable **queue** (Redis Streams) + in-memory twin | `app/ports/job_queue.py`, `app/adapters/redis/queue.py`, `app/adapters/memory/queue.py` |
| **Worker** process | `app/worker.py` — `python -m app.worker` |
| **Event sourcing** (append-only log) | `app/domain/events.py`, `app/ports/event_store.py`, `app/adapters/*/event_store.py` |
| **At-least-once + idempotent** consumption | `process_job` in `app/worker.py` (skips terminal jobs; acks only after commit) |
| **Cancellation** (cooperative) | `POST /v1/jobs/{id}/cancel` + the worker's mid-work re-check |

**Changes from Stage 0:** `POST /v1/jobs` now returns **202** (was 201); the
manual `POST /{id}/transition` endpoint is **removed** (the worker owns
transitions now). New endpoints: `POST /{id}/cancel`, `GET /{id}/events`.

**Lifecycle:** `PENDING --(worker starts)--> RUNNING --(work)--> SUCCEEDED | FAILED`,
or `--> CANCELLED` at any non-terminal point.

### Test steps

**A. Automated (no infra — seconds)**
```bash
cd services/gateway
source .venv/bin/activate            # or create it (see Stage 0)
pip install -r requirements.txt      # now also installs redis
pytest -v
```
Expect **12 passed** (7 API + 5 worker). The worker tests call `process_job`
directly against in-memory adapters — no Redis/Postgres needed.

**B. End-to-end with the real worker (Docker)**
```bash
cd services/gateway
docker compose up --build            # starts postgres + redis + gateway + worker
# in another terminal, watch the worker:
docker compose logs -f worker
```
Then submit a job and watch it flow:
```bash
# submit -> 202 + id, status "pending"
curl -si -XPOST localhost:8000/v1/jobs \
  -H 'content-type: application/json' -d '{"payload":{"task":"demo"}}'

# within ~1s the worker logs:  job <id> -> succeeded
# poll the job -> status "succeeded"
curl -s localhost:8000/v1/jobs/<ID> ; echo

# read the event log -> submitted, started, succeeded
curl -s localhost:8000/v1/jobs/<ID>/events ; echo
```

**C. See cancellation**
```bash
# submit, then immediately cancel before the worker finishes
curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' \
  -d '{"payload":{"task":"slow"}}'            # grab <ID>
curl -s -XPOST localhost:8000/v1/jobs/<ID>/cancel ; echo   # -> "cancelled"
curl -s localhost:8000/v1/jobs/<ID>/events ; echo          # worker skips it
```

**D. See at-least-once redelivery (crash recovery)**
```bash
# submit a job, then kill the worker mid-run and restart it:
docker compose restart worker
# the unacked message is reclaimed on startup and processed again — the job
# still ends "succeeded" exactly once (idempotent), never lost.
```

**What to verify (learning checkpoints)**
- Submit returns **202** (accepted), not 201.
- The event log shows the full ordered history (`submitted → started → succeeded`).
- Cancelling a pending job → worker **skips** it (only `submitted, cancelled` events).
- A repeated `Idempotency-Key` returns the same job and is enqueued **once**.
- Restarting the worker mid-job does **not** duplicate or lose the job.

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Worker logs nothing / jobs stay `pending` | Worker can't reach Redis. Check `REDIS_URL` (host `redis` in compose, `localhost` when running locally). `docker compose logs redis`. |
| `NOGROUP No such key ... or consumer group` | The consumer group wasn't created. The worker calls `ensure_group()` on startup; make sure the worker actually started (`docker compose ps`). Restarting the worker recreates it. |
| Job stuck in `running` forever | The worker died mid-work and hasn't reclaimed the pending message yet. `reclaim_stale` runs on worker startup — `docker compose restart worker`. (Tune `min_idle_ms` in `app/worker.py`.) |
| `pip install` fails on **redis/asyncpg/pydantic-core** (new Python) | Same as Stage 0: use Docker (Python 3.12) or a 3.12 venv. Tests don't need redis/asyncpg. |
| `ModuleNotFoundError: redis` when running tests | Shouldn't happen — redis is imported lazily. If it does, you imported `run_worker` (the loop) instead of `process_job`; import only `process_job`/`ProcessResult` in tests. |
| Submitted a job but worker says `orphan` | The job id was enqueued but the DB row wasn't committed (a crash in the tiny window before commit). Known Stage-1 gap; the transactional **outbox** in Stage 5 closes it. The worker safely acks and moves on. |
| Two workers, a job processed twice? | It won't double-apply: `process_job` is idempotent (skips terminal jobs, only starts PENDING). Scale workers freely: `docker compose up --scale worker=3`. |

---

## Stage 2 — polyglot persistence ⬜ (next)

Will add MongoDB for job trajectories/payloads and Redis for hot state, with the
"why polyglot persistence" reasoning. Steps appended when it lands.
