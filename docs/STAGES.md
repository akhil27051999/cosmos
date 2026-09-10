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

## Stage 2 — polyglot persistence ✅

**What it adds:** the *right database for each data shape*.

- **Postgres** — the transactional core: `jobs` metadata + `job_events` log (unchanged).
- **MongoDB** — the big document per job: the input **`payload`** (moved out of Postgres) + the worker's **`result`**.
- **Redis** — a **hot-state cache** for `GET /v1/jobs/{id}` (cache-aside on reads, invalidate-on-write in `JobService`).

**New concepts (and where they live)**

| Concept | Where |
|---|---|
| Job document (payload + result) | `app/domain/document.py`, `app/ports/document_store.py` |
| MongoDB adapter (embed, one doc per job) | `app/adapters/mongo/document_store.py` |
| Hot-state cache port | `app/ports/cache.py` |
| Redis cache adapter (JSON + TTL) | `app/adapters/redis/cache.py` |
| Cache-aside read | `get_job` in `app/api/routes_jobs.py` |
| Invalidate-on-write | `JobService._transition` |
| Payload moved out of Postgres | `domain/job.py`, `adapters/postgres/{models,repository}.py` |

**New endpoint:** `GET /v1/jobs/{id}/document` → the payload + result from Mongo.

### Test steps

**A. Automated (no infra — seconds)**
```bash
cd services/gateway
source .venv/bin/activate
pip install -r requirements.txt      # now also installs motor
pytest -v
```
Expect **17 passed** (adds 3 document tests + 2 cache tests). They use the
in-memory document store and cache — no Mongo/Redis needed.

**B. End-to-end (Docker — now also starts MongoDB)**
```bash
cd services/gateway
docker compose up --build            # postgres + redis + mongo + gateway + worker
```
```bash
# submit a job (payload goes to Mongo)
curl -si -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' \
  -d '{"payload":{"task":"demo","n":21}}'      # grab <ID>

# the document (payload now; result appears after the worker runs ~1s later)
curl -s localhost:8000/v1/jobs/<ID>/document ; echo
# -> {"job_id":"...","payload":{"task":"demo","n":21},"result":{"echo":{...},"processed":true},...}

# metadata read (served via the Redis cache on repeat calls)
curl -s localhost:8000/v1/jobs/<ID> ; echo
```

**C. See the polyglot split for yourself**
```bash
# Postgres has metadata only — no payload column anymore:
docker compose exec postgres psql -U cosmos -d cosmos -c "\d jobs"
# Mongo has the document:
docker compose exec mongo mongosh cosmos --quiet --eval 'db.job_documents.find().pretty()'
# Redis has the cached job (briefly, ~30s TTL) after you GET it:
docker compose exec redis redis-cli --scan --pattern 'job:*'
```

**What to verify (learning checkpoints)**
- `\d jobs` shows **no `payload` column** — it's in Mongo now.
- `GET /{id}/document` returns the payload immediately, and the `result` after the worker finishes.
- Repeated `GET /{id}` is a cache hit (add `DB_ECHO=true` to watch Postgres go quiet on the 2nd call).
- After a `cancel`, the next `GET /{id}` shows `cancelled` (cache was invalidated, not stale).

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `pip install` fails on **motor/pymongo** (new Python) | Same as before: use Docker (Python 3.12) or a 3.12 venv. Tests don't need motor. |
| Worker can't reach Mongo / jobs never get a `result` | Check `MONGO_URL` (host `mongo` in compose, `localhost` locally). `docker compose logs mongo`. |
| `GET /{id}/document` returns 404 for a real job | The Mongo doc wasn't written (submit failed after the Postgres insert, or you're pointing at the wrong Mongo DB). Check `MONGO_DB=cosmos` and `docker compose logs gateway`. |
| `GET /{id}` shows a stale status | Invalidation didn't run. Confirm the write went through `JobService` (routes/worker both do). The Redis TTL (~30s) is the backstop. |
| Orphan documents in Mongo | A submit wrote the doc but the Postgres insert failed (rare race). Harmless; the transactional outbox in Stage 5 closes this. |
| `mongosh` not found in the container | Use `docker compose exec mongo mongosh …` (the `mongo:7` image ships `mongosh`). |

---

## Stage 3 — the agent layer ✅

**What it adds:** the placeholder `echo` work becomes a **real Claude agent
loop** — plan → call a tool → observe → repeat — bounded by a **token budget**,
with the full **trajectory** stored in Mongo and streamed live over **SSE**.

**New concepts (and where they live)**

| Concept | Where |
|---|---|
| LLM behind a port (testable, swappable) | `app/ports/llm.py`, `app/adapters/anthropic/llm.py`, `app/adapters/memory/llm.py` (scripted fake) |
| Tool abstraction + safe calculator | `app/agent/tools.py` |
| Agent loop (plan→act→observe) + guardrails | `app/agent/loop.py` |
| Token budget / max-steps / cooperative cancel | `run_agent` guardrails |
| Trajectory persisted to Mongo | worker `_make_agent_work_fn` → `docs.set_result` |
| Live streaming (SSE + Redis pub/sub) | `app/ports/stream_bus.py`, `app/adapters/redis/stream_bus.py`, `GET /v1/jobs/{id}/stream` |

**New endpoint:** `GET /v1/jobs/{id}/stream` (Server-Sent Events).
**New config:** `ANTHROPIC_API_KEY`, `AGENT_MODEL` (default `claude-opus-5`), `AGENT_MAX_TOKENS`, `AGENT_MAX_STEPS`, `AGENT_TOKEN_BUDGET`.

### Test steps

**A. Automated (no API key, no cost — seconds)**
```bash
cd services/gateway
source .venv/bin/activate
pip install -r requirements.txt      # now also installs anthropic
pytest -v
```
Expect **26 passed** (adds agent-loop + tool + streaming tests). They use a
**scripted fake LLM**, so no Anthropic key or network is needed.

**B. End-to-end with the real agent (Docker — needs your API key)**
```bash
export ANTHROPIC_API_KEY=sk-ant-...          # your key
# optional, to cut cost while testing:
export AGENT_MODEL=claude-sonnet-5
cd services/gateway
docker compose up --build
docker compose logs -f worker
```
Submit a task and read the trajectory:
```bash
ID=$(curl -s -X POST localhost:8000/v1/jobs -H 'content-type: application/json' \
  -d '{"payload":{"prompt":"What is 21 * 2? Use the calculator."}}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo "job=$ID"

curl -s localhost:8000/v1/jobs/$ID/document | python3 -m json.tool
# -> result.answer = "42", result.trajectory = [assistant, tool_call, tool_result, final], result.usage = {...}
```

**B2. FREE alternative — run the agent on Groq (no credit card)**

Because the LLM is behind a port, you can swap providers with an env switch —
no code change. Get a free key at **console.groq.com** (API Keys → Create), then:
```bash
export LLM_PROVIDER=groq
export GROQ_API_KEY=gsk_...
# optional: export GROQ_MODEL=llama-3.3-70b-versatile   (default; supports tools)
cd services/gateway
docker compose up --build
```
Everything else (submit, `/document`, `/stream`) works identically — the worker
just uses `GroqLLMClient` instead of `AnthropicLLMClient`. Switch back any time
with `LLM_PROVIDER=anthropic`.

**C. Watch the agent think live (SSE)**
```bash
# open the stream FIRST (it's live-only), then submit in another terminal
curl -N localhost:8000/v1/jobs/$ID/stream
# streams: data: {"type":"tool_call",...}  /  {"type":"tool_result",...}  /  {"type":"final",...}  /  {"type":"end",...}
```

**What to verify (learning checkpoints)**
- With no tools needed, the agent answers directly; with math, it **calls the calculator** (see `tool_call`/`tool_result` in the trajectory).
- `result.usage.total` reports tokens spent; a tiny `AGENT_TOKEN_BUDGET` makes the job stop early (`stopped: "budget"`, job `FAILED`).
- The SSE stream shows steps arriving in real time, ending with `end`.

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Job goes `failed`, worker logs `401`/authentication error | `ANTHROPIC_API_KEY` missing or wrong. `export` it in the shell you run `docker compose up` from (compose passes it through). |
| `model ... does not exist or you do not have access` (404) | The model id isn't available to your key (catalogs change). **Anthropic:** set `AGENT_MODEL` to one you have (`claude-sonnet-5`, `claude-haiku-4-5`). **Groq:** list yours with `curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"` and set `GROQ_MODEL` to a tool-capable one (e.g. `openai/gpt-oss-20b`). Then `docker compose up -d`. The job fails cleanly with the exact error in its event log. |
| Cost higher than expected | Default is `claude-opus-5` with adaptive thinking. Set `AGENT_MODEL=claude-haiku-4-5` and/or lower `AGENT_TOKEN_BUDGET` for cheap testing. |
| `pip install` fails building a wheel | You're on Python 3.13/3.14. Use a **3.12** venv (`python3.12 -m venv .venv`) or Docker. |
| SSE shows nothing | It's **live-only** — connect *before/while* the job runs. If the job already finished, read `GET /{id}/document` for the stored trajectory instead. |
| Job `failed` with `agent stopped: max_steps` / `budget` / `refusal` | The agent hit a guardrail (that's by design). Raise `AGENT_MAX_STEPS` / `AGENT_TOKEN_BUDGET`, or refine the prompt. The partial trajectory is still in Mongo. |

---

## Stage 4 — resilience ✅

**What it adds:** the provider call (the flaky, external part) is now wrapped in
resilience, and the gateway sheds excess load.

- **`ResilientLLMClient`** (`app/adapters/resilient/llm.py`) — a decorator over the `LLMClient` port that adds, per call: **timeout → retry (backoff + full jitter) → circuit breaker → fallback provider**. The agent loop is unchanged.
- **Resilience primitives** (`app/resilience/`): `RetryPolicy`/`run_with_retry`, `CircuitBreaker`, `TokenBucket` — all pure and unit-tested.
- **Distributed rate limit + load-shedding**: a Redis Lua token bucket (`app/adapters/redis/rate_limiter.py`) guards `POST /v1/jobs`; over the limit → **429** with `Retry-After`.

**Config knobs (env):** `LLM_TIMEOUT_S`, `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY`, `BREAKER_FAILURE_THRESHOLD`, `BREAKER_RESET_S`, `LLM_FALLBACK_PROVIDER` (`anthropic`|`groq`), `SUBMIT_RATE_PER_SEC`, `SUBMIT_BURST`.

### Test steps

**A. Automated (no infra — seconds)**
```bash
cd services/gateway && source .venv/bin/activate && pytest -q
```
Expect **40 passed** (adds circuit-breaker, retry, token-bucket, resilient-LLM, and load-shed tests). All use fakes/injected clocks — no provider, no Redis.

**B. See a fallback provider in action (Docker)**
Set a primary that will fail and a fallback that works, so a provider outage is absorbed:
```bash
export LLM_PROVIDER=groq            GROQ_API_KEY=gsk_...   GROQ_MODEL=openai/gpt-oss-20b
export LLM_FALLBACK_PROVIDER=anthropic  ANTHROPIC_API_KEY=sk-ant-...   # or swap which is primary
cd services/gateway && docker compose up -d
```
Submit a job; if the primary errors (e.g. a bad model id), the worker retries, then falls back — the job still `succeeds`. The failure/fallback shows in `docker compose logs -f worker`.

**C. Trigger load-shedding (429)** — set a tiny submit limit and hammer it:
```bash
export SUBMIT_RATE_PER_SEC=1 SUBMIT_BURST=3
docker compose up -d
# fire 6 quick submits; the first ~3 get 202, the rest 429:
for i in $(seq 1 6); do
  curl -s -o /dev/null -w "%{http_code}\n" -XPOST localhost:8000/v1/jobs \
    -H 'content-type: application/json' -d '{"payload":{"prompt":"hi"}}'
done
# expect: 202 202 202 429 429 429   (then reset with SUBMIT_BURST=40 and up -d)
```

**What to verify (learning checkpoints):**
- A *transient* failure (timeout/429/5xx) is **retried**; a *permanent* one (400/validation) fails immediately.
- After repeated failures the **breaker opens** and calls fast-fail (worker logs) until the cooldown.
- With `LLM_FALLBACK_PROVIDER` set, a primary outage still yields a `succeeded` job via the fallback.
- Over-rate submits return **429 + `Retry-After`**, and the body uses the standard error envelope (`code: "rate_limited"`).

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Every submit returns 429 immediately | `SUBMIT_BURST`/`SUBMIT_RATE_PER_SEC` too low, or the Redis bucket key is stale. Raise them and `docker compose up -d`; or `docker compose exec redis redis-cli DEL ratelimit:submit`. |
| Jobs still fail instead of falling back | `LLM_FALLBACK_PROVIDER` unset, or the fallback's key/model is also invalid. Check both providers' keys/models; watch `docker compose logs worker`. |
| A job hangs then fails after ~60s | The provider stalled and hit `LLM_TIMEOUT_S`; it's retried per `RETRY_MAX_ATTEMPTS`. Lower the timeout for faster failover, or raise attempts for flakier providers. |
| Breaker seems stuck open | It reopens for `BREAKER_RESET_S` after each failed probe. If the provider is genuinely down, that's correct — fix the provider or switch `LLM_PROVIDER`. |
| Load-shed rejects legit traffic under multiple gateway replicas | Good — the limit is *shared* via Redis (that's the point). Size `SUBMIT_RATE_PER_SEC` for total capacity, not per replica. |
| **Code change has no effect** (e.g. rate-limit not enforced, new endpoint 404s) | `docker compose up -d` reuses the **old image** — it only recreates containers. **Code** changes need a rebuild: `docker compose up -d --build`. (Env-only changes don't; the `Dockerfile` bakes code in at build via `COPY app ./app`.) |
| `curl` prints **`000`** right after `up -d` | `000` = couldn't connect: the app hadn't finished booting when you fired the request. Wait for readiness first: `until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done`. Never hit a container the instant it says "Started". |
| Env change (e.g. `SUBMIT_BURST`) didn't take effect | Two causes: (1) you *assigned* but didn't **`export`** it (`VAR=3` alone isn't seen by compose's `${VAR}`; use `export VAR=3`); (2) the container wasn't recreated — use `docker compose up -d --force-recreate gateway`. Verify with `docker compose exec gateway env | grep -i submit`. |
| Rate limit behaves oddly after changing the limit | The bucket state lives in Redis and **survives restarts** (that's how it's shared across replicas). Reset it for a clean test: `docker compose exec redis redis-cli DEL ratelimit:submit`. |

---

## Stage 5 — event-driven backbone 🟡 (part 1: outbox + bus)

**What part 1 adds:** the **transactional outbox** + a message-bus backbone.

- Every domain event is **staged in an `outbox` table in the same DB transaction** that writes it (`JobService._stage_event`) — so "state changed" and "event to publish" commit atomically. A lost publish becomes impossible.
- A background **relay** (`app/relay.py`, running inside the worker) drains unpublished outbox rows to **Redpanda/Kafka** (`job-events` topic), marking each done — **publish-before-mark = at-least-once**.
- New: `MessageBus` port (`app/ports/message_bus.py`) with Redpanda (`aiokafka`) + in-memory adapters; `Outbox` port + Postgres/in-memory adapters; `redpanda` service in compose.

**Deferred to later increments:** CQRS read model (the `job-events` consumer/projector), Saga, CDC, schema registry, exactly-once consumer semantics.

### Test steps

**A. Automated (no infra — seconds)**
```bash
cd services/gateway && source .venv/bin/activate && pip install -r requirements.txt && pytest -q
```
Expect **44 passed** (adds 4 outbox tests: events are staged; the relay publishes-then-marks; it's idempotent). Tests use the in-memory bus — no Redpanda needed.

**B. End-to-end (Docker — now also starts Redpanda)**
```bash
cd services/gateway
docker compose up -d --build            # +redpanda; the worker runs the relay
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done; echo ready
# submit a job; its events flow: DB (outbox) -> relay -> Redpanda topic "job-events"
curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' -d '{"payload":{"prompt":"hi"}}'
```
Watch the outbox drain and the events land on the topic:
```bash
# outbox rows should end up with a published_at (relay ran):
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT seq,topic,value->>'type',published_at IS NOT NULL AS published FROM outbox ORDER BY seq;"
# consume the topic from the broker (host tool):
docker compose exec redpanda rpk topic consume job-events --num 5 --offset start
```
You'll see `submitted → started → succeeded` on `job-events`, and every outbox row marked published.

**What to verify (learning checkpoints):**
- Each job produces outbox rows for its events, all eventually `published_at IS NOT NULL`.
- The same events appear on the Redpanda `job-events` topic, keyed by job id (so one job's events stay ordered on one partition).
- Kill the worker mid-run and restart — unpublished rows are still published (at-least-once); nothing is lost.

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `pip install` fails on **aiokafka** (new Python) | Same as before — use Docker (Python 3.12) or a 3.12 venv. Tests don't import aiokafka (in-memory bus). |
| Outbox rows never get `published_at` | The relay can't reach Redpanda. `docker compose logs redpanda`; confirm `REDPANDA_BROKERS=redpanda:9092` in the worker (`docker compose exec worker env | grep REDPANDA`). The relay retries, so it self-heals once the broker is up. |
| `rpk topic consume` says the topic doesn't exist | It's auto-created on first publish; submit a job first. Or `docker compose exec redpanda rpk topic create job-events`. |
| Redpanda container unhealthy on an old/small Docker | It's a single-node dev broker; give Docker ≥1 CPU / 1–2 GB. The worker uses `depends_on: service_started` (not healthy), so it won't block the stack — the relay just retries. |
| Events appear twice on the topic | Expected under crash recovery — publish-before-mark is **at-least-once**. Consumers (the CQRS projector, next increment) dedupe by event/outbox id. |

---

## Stage 5 — part 2 (CQRS read model) ✅

**What part 2 adds:** the query side of CQRS. A **projector** consumes the
`job-events` topic and folds each event into a denormalized **read model**
(`job_read_model` table), which the API serves for fast queries — separate from
the write model (jobs/job_events).

- `app/projector.py` — `project_once` (poll → apply → commit) + `run_projector` (loop), running as a background task in the worker.
- Read model: `ReadModelStore` port + Postgres (atomic upsert `ON CONFLICT … WHERE version < EXCLUDED.version`) + in-memory adapters.
- **Idempotent under at-least-once**: each event carries the job `version`; the projector applies **last-writer-wins by version**, so duplicate/out-of-order events can't corrupt the view.
- New endpoints: `GET /v1/stats` (counts per status) and `GET /v1/jobs/{id}/view` (the projection).
- The bus consumer side (`poll`/`commit`) is now implemented in both adapters.

### Test steps

**A. Automated**
```bash
cd services/gateway && source .venv/bin/activate && pytest -q
```
Expect **48 passed** (adds 4 CQRS tests: projector builds the model end-to-end via the in-memory bus; apply is idempotent + LWW; `/v1/stats`; `/v1/jobs/{id}/view` + 404).

**B. End-to-end (Docker)**
```bash
cd services/gateway && docker compose up -d --build
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done; echo ready
# submit a couple of jobs, let the worker + projector run (~2s)
curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' -d '{"payload":{"prompt":"hi"}}' >/dev/null
curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' -d '{"payload":{"prompt":"hi again"}}' >/dev/null
sleep 3
curl -s localhost:8000/v1/stats | python3 -m json.tool          # counts per status, from the read model
# view one job's projection (grab an <ID> from a submit response):
curl -s localhost:8000/v1/jobs/<ID>/view | python3 -m json.tool
```
Or look at the read model + the projector's consumer group directly:
```bash
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT job_id,status,version,event_count FROM job_read_model;"
docker compose exec redpanda rpk group describe cqrs-projector   # lag should be ~0
```

**What to verify (learning checkpoints):**
- `/v1/stats` reflects the jobs you submitted (e.g. `succeeded` count rises as the worker finishes them).
- `/v1/jobs/{id}/view` shows the projected `status`/`version`/`event_count` — the *read* model, not the write tables.
- It's **eventually consistent**: right after submit, `/view` may 404 for a beat until the projector catches up (the write-side `GET /v1/jobs/{id}` is immediate).
- Kill/restart the worker — the projected counts stay correct (idempotent LWW; no double counting).

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `/v1/stats` empty or `/view` 404s for a real job | The projector hasn't caught up (or can't reach Redpanda). Give it a second; check `docker compose logs worker` for `[projector]` errors and `rpk group describe cqrs-projector` for lag. |
| `event_count` looks doubled | Shouldn't happen — apply is version-guarded. If you see it, an event was staged without a `version` field; confirm `JobService._stage_event` includes `version`. |
| Consumer group not advancing | The projector commits only after applying a batch. If it errors mid-batch it won't commit and will re-poll the same batch (safe). Fix the underlying error shown in the worker logs. |
| Read model diverges from write model | It's eventually consistent by design. Persistent divergence = projector down; restart the worker (it re-consumes from its committed offset). |

---

## Stage 6 — multi-tenancy & identity ✅

**What it adds:** the platform becomes **multi-tenant**. Every request now
authenticates with an **API key**, every job belongs to a **tenant**, and every
read/write is **scoped** to the caller's tenant. A per-tenant **monthly quota**
(backed by a usage ledger) governs how much each tenant may submit, and an
**admin API** mints the first tenants and keys.

- Identity domain: `Tenant`, `ApiKey` (only a **hash** of the secret is stored), `Principal` (the authenticated caller).
- `app/auth/keys.py` — mint/parse/verify keys of the form `csk_<key_id>_<secret>`; constant-time compare.
- Auth choke point: `get_principal` (in `deps.py`) verifies the key → tenant, or raises **401/403**.
- Isolation: `Job` gains `tenant_id`; the repository scopes `get`/`list`/idempotency by it; a cross-tenant id is a **404** (never leaked). The CQRS read model carries `tenant_id` too, so `/v1/stats` and `/view` are per-tenant.
- Quota: `JobService.submit` checks the tenant is active and under its monthly quota (**429** `quota_exceeded`), then records usage in the ledger (atomic upsert-increment).
- Admin: `POST /v1/admin/tenants` (+ `/keys`), guarded by `ADMIN_TOKEN` (blank ⇒ admin API disabled).

### Test steps

**A. Automated**
```bash
cd services/gateway && source .venv/bin/activate && pytest -q
```
Expect **60 passed** (adds 12 identity tests: key crypto, 401 missing/invalid/revoked, tenant isolation, quota 429, suspended tenant 403, admin bootstrap).

**B. End-to-end (Docker)** — auth is now required, so you need a key first.
```bash
cd services/gateway
export ADMIN_TOKEN=dev-admin-secret          # enables the admin API
docker compose up -d --build
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done; echo ready

# 1) A call with NO key is rejected.
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs \
  -H 'content-type: application/json' -d '{"payload":{"prompt":"hi"}}'      # -> 401

# 2) Create a tenant + its first API key (admin token, NOT an API key).
curl -s -XPOST localhost:8000/v1/admin/tenants \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"name":"Acme","monthly_quota":100}' | python3 -m json.tool
# copy the "api_key" from the response:
export KEY=csk_...                            # paste it here

# 3) Submit as that tenant (Bearer key) -> 202.
curl -s -XPOST localhost:8000/v1/jobs \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"payload":{"prompt":"hello"}}' | python3 -m json.tool
# note the returned "tenant_id"; list + stats are scoped to this tenant only:
curl -s localhost:8000/v1/jobs  -H "Authorization: Bearer $KEY" | python3 -m json.tool
curl -s localhost:8000/v1/stats -H "Authorization: Bearer $KEY" | python3 -m json.tool
```

**Prove tenant isolation:** make a *second* tenant/key, then try to fetch the
first tenant's job id with the second key — you get a **404**, as if it doesn't
exist. Its jobs never appear in the second tenant's list or stats.

**Prove the quota:** create a tenant with `"monthly_quota":1`, submit twice — the
second returns **429** `quota_exceeded` with a `Retry-After`.

### Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Every request is `401 unauthenticated` | Missing/'' `Authorization`. Send `Authorization: Bearer csk_...` (or `X-API-Key: csk_...`). |
| `POST /v1/admin/tenants` returns `403 forbidden` "admin API is disabled" | `ADMIN_TOKEN` is unset in the gateway. `export ADMIN_TOKEN=... ` then `docker compose up -d` (env-only change → no `--build`). |
| Admin call returns `401` | Wrong/missing `X-Admin-Token`. It must equal the gateway's `ADMIN_TOKEN`. |
| A valid key suddenly 401s | The key is revoked, or the row is gone (fresh volume). Mint a new one via the admin API. |
| Submits 429 with `quota_exceeded` (not `rate_limited`) | The tenant hit its **monthly** quota (different lever from the Stage-4 per-second load-shed). Raise `monthly_quota` on a new tenant, or wait for the next calendar month. |
| Lost the API key | It's shown once and only the hash is stored — it **cannot** be recovered. Mint a new key: `POST /v1/admin/tenants/{id}/keys`. |
| `/v1/jobs/{id}/view` 404s for your own job | Either not projected yet (eventual consistency — wait a beat) or you're using a different tenant's key than the one that submitted it. |

---

## Stage 7 — the worker as its own (Go/gRPC) service ⬜ (next)

Will extract the worker into a standalone service with a typed gRPC contract, and
add OAuth2/OIDC (JWT) as a second principal source alongside API keys. Steps
appended when it lands.
