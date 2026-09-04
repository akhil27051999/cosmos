# Cosmos — Engineering Notes (Stages 0–1)

> **Purpose of this document.** Everything you need to *explain this project in an
> interview from what you actually built*: the idea, the motive, and a
> block-by-block walkthrough of every file — what it does, **why** it's written
> that way, and the benefit. Pair it with [`STAGES.md`](STAGES.md), which is the
> hands-on test/troubleshoot runbook.
>
> **How to use it.** Read §1–§3 for the mental model, then §4 (Stage 0) and §5
> (Stage 1) file-by-file with the code open beside you. §7 is the interview
> Q&A — rehearse those out loud.

## Table of contents
1. The big picture — what we're building and why
2. The architecture — hexagonal (ports & adapters)
3. The two request flows (submit & worker)
4. **Stage 0** — the clean foundation (block-by-block)
5. **Stage 1** — the async core (block-by-block)
5B. **Stage 2** — polyglot persistence (block-by-block)
5C. **Stage 3** — the agent layer (block-by-block)
5D. **Stage 4** — resilience (block-by-block)
6. Cross-cutting design decisions & trade-offs
7. Interview Q&A — what / why / how
8. Known gaps and what later stages fix
9. Glossary

---

## 1. The big picture — what we're building and why

**One-liner:** *Cosmos is an async, multi-tenant LLM job-orchestration platform.
Clients submit jobs; the platform accepts them instantly, queues them, and a
worker fleet processes them, persisting every state change so the system is
observable, resumable, and defensible.*

**The idea.** Doing real work (later: an LLM agent loop that can take seconds to
minutes) *inside* an HTTP request is a trap — it holds connections open, and a
traffic burst exhausts the server. So we split **accepting** work from **doing**
it. The API accepts a job in milliseconds (`202 + id`) and hands it to a durable
queue; a separate worker does the slow part. This is the backbone pattern of
every serious job/agent platform.

**The motive (for me).** It's the vehicle to learn — and defend in interviews —
distributed-systems fundamentals: async processing, state machines, event
sourcing, idempotency, optimistic concurrency, at-least-once delivery, and clean
architecture. Each stage is a runnable increment so the learning is concrete.

**Services built so far (both live in `services/gateway/`, one codebase, two
processes):**

| Service | Process | Responsibility |
|---|---|---|
| **Gateway (API)** | `uvicorn app.main:app` | Accept/validate requests, persist + enqueue jobs, expose reads (job, list, events), cancellation. Returns fast. |
| **Worker** | `python -m app.worker` | Consume jobs from the queue and drive them through their lifecycle (running → succeeded/failed), doing the actual work. |

> They share the same Python package today for simplicity; **Stage 7** extracts
> the worker into its own (Go) service. That's a deliberate, staged split.

---

## 2. The architecture — hexagonal (ports & adapters)

The single most important idea in the codebase. Picture three rings:

```
            ┌─────────────────────────────────────────────┐
            │  API (FastAPI routes)   Worker (loop)         │  ← drivers (edges)
            │        │                     │                │
            │        ▼                     ▼                │
            │   Application service (use cases)             │  ← orchestration
            │        │                                      │
            │        ▼                                      │
            │   Domain (Job, JobEvent, rules)  ← pure       │  ← the centre
            │        ▲                                      │
            │        │ depends on interfaces (PORTS)        │
            │   Ports: JobRepository, EventStore, JobQueue  │
            │        ▲                                      │
            │   Adapters: Postgres / Redis / in-memory      │  ← driven (edges)
            └─────────────────────────────────────────────┘
```

**The rule:** dependencies point *inward*. The domain depends on nothing. The
application depends on the domain and on *ports* (interfaces). Adapters implement
those ports. FastAPI and the worker sit on the outside and call inward.

**Why this matters (say this in an interview):**
- **Testability** — swap the Postgres adapter for an in-memory one; the domain,
  services, and routes are unchanged. That's why our tests need no database.
- **Replaceability** — Stage 5 swaps Redis Streams for Kafka by writing a new
  `JobQueue` adapter; nothing else changes.
- **Clarity** — business rules live in one place, uncontaminated by frameworks.

**Folder → ring mapping:**
- `app/domain/` — entities + rules (pure Python).
- `app/ports/` — the interfaces the app depends on.
- `app/adapters/` — concrete implementations (`postgres/`, `redis/`, `memory/`).
- `app/application/` — use-case orchestration (`JobService`).
- `app/api/` — the HTTP driver; `app/worker.py` — the queue driver.
- `app/core/` — cross-cutting plumbing (config, DB, correlation ids).

---

## 3. The two request flows

**Flow A — submit (the accept-fast path, milliseconds):**
```
client → POST /v1/jobs
  → routes.submit_job → JobService.submit
      → repo.add(job)                 (INSERT, status=pending)
      → events.append(SUBMITTED)      (append to job_events)
      → queue.enqueue(job.id)         (XADD to Redis Stream)
  → 202 Accepted + {id, status:"pending", version:1}
```

**Flow B — worker (the do-it-later path):**
```
worker loop → queue.consume  (XREADGROUP)
  → process_job(job_id)
      → load job; if terminal → skip+ack (idempotent)
      → JobService.mark_running   (pending→running, STARTED event)
      → work_fn(job)              (the actual work; Stage-1 = simulated)
      → JobService.mark_succeeded (running→succeeded, SUCCEEDED event)
  → ack (XACK) — only after the DB commit
```

---

## 4. Stage 0 — the clean foundation (block-by-block)

**Idea:** rebuild the app front door as a FastAPI service with clean
architecture, so every later stage has a correct, testable base. **Motive:**
get the *fundamentals* right — layering, validation, idempotency, optimistic
concurrency, consistent errors, correlation ids — before adding distributed
complexity.

### 4.1 `app/domain/job.py` — the domain entity + state machine
*The heart. Pure Python: no FastAPI, no DB.*

- **`class JobStatus(str, Enum)`** — the five states (`pending, running,
  succeeded, failed, cancelled`). *Why inherit `str`:* the value serialises
  directly to `"pending"` in JSON/DB while staying a real enum in code. *Benefit:*
  type-safety without conversion boilerplate.
- **`_ALLOWED_TRANSITIONS` dict** — the state machine *as data*: each status maps
  to the set of statuses it may move to; terminal states map to `set()`. *Why:*
  expressing the rule declaratively (not as scattered `if`s) makes illegal states
  impossible and the rule readable at a glance. *Benefit:* one obvious place to
  audit "what transitions are legal?"
- **`_now()`** — UTC, timezone-aware timestamps. *Why:* always store UTC; format
  for humans at the edge. *Benefit:* no timezone bugs across services/regions.
- **`@dataclass class Job`** — fields: `payload, id (uuid4), status, idempotency_key,
  version, created_at, updated_at`. *Why a dataclass:* it's a data holder with a
  little behaviour; no ORM/framework coupling. *`version` field:* the optimistic-
  concurrency token (see `update`). *`idempotency_key`:* ties a job to a client's
  "do this once" request.
- **`can_transition_to` / `transition_to`** — `transition_to` guards the move
  against `_ALLOWED_TRANSITIONS`, and on success bumps `version` and
  `updated_at`; an illegal move raises `InvalidTransition`. *Why in the entity:*
  the rule belongs to the thing it governs, not to a route or the DB. *Benefit:*
  you cannot corrupt a job's lifecycle from anywhere in the system.
- **`is_terminal` property** — true when no transitions are possible. *Benefit:*
  the worker uses it to decide "already done — skip" (idempotency).

### 4.2 `app/domain/exceptions.py` — domain errors
- **`DomainError` base + `JobNotFound`, `InvalidTransition`, `VersionConflict`** —
  errors described in *business* language, never HTTP. *Why a base class:* the web
  layer can catch the whole family in one handler. *Benefit:* the domain stays
  ignorant of HTTP; the mapping to status codes lives at the edge (§4.9).

### 4.3 `app/ports/job_repository.py` — the repository PORT
- **`class JobRepository(Protocol)`** with `add, get, get_by_idempotency_key,
  list, update`. *Why a `Protocol` (structural typing):* any class with these
  methods satisfies it — no inheritance needed. *Why it exists:* the app depends
  on this *interface*, not on Postgres. *Benefit:* swap implementations freely
  (Postgres in prod, in-memory in tests).
- **`update(job, expected_version)`** — the signature bakes optimistic concurrency
  into the contract: implementers *must* refuse if the stored version moved.

### 4.4 `app/adapters/memory/repository.py` — in-memory adapter
- **`_jobs: dict` + `copy.deepcopy` everywhere** — stores copies so callers can't
  mutate internal state by holding a reference. *Why:* mimics the isolation a real
  DB gives. *Benefit:* realistic behaviour, and it's the reason tests are fast and
  need no infrastructure.
- **`update` version check** — raises `VersionConflict` when
  `current.version != expected_version`. *Why:* proves the OCC contract holds even
  in the fake, so tests exercise the real behaviour.

### 4.5 `app/adapters/postgres/models.py` — the `jobs` table
- **`JobModel(Base)`** — the SQLAlchemy table. *Key point:* this is **not** the
  domain `Job`; it's a persistence detail. The repository translates between them.
  *Benefit:* DB schema and business object evolve independently.
- **`UniqueConstraint("idempotency_key")`** — the *race-proof* backstop for
  idempotency. *Why:* even if two identical requests both pass the "does it
  exist?" check at the same instant, the DB rejects the second insert. *Benefit:*
  correctness under concurrency that application logic alone can't guarantee.
- **Comment on `version_id_col`** — we *don't* let SQLAlchemy auto-manage the
  version because the domain already owns it; the repo does an explicit
  conditional UPDATE instead (§4.6). *Why called out:* so the OCC mechanism is
  visible, not magic.

### 4.6 `app/adapters/postgres/repository.py` — Postgres adapter
- **`_to_entity` / `_to_model`** — map row↔domain object. *Benefit:* the only
  place that knows both shapes; everything else deals in domain objects.
- **`add`** — `session.add` + `flush`, catching `IntegrityError` → re-raised as
  `VersionConflict`. *Why flush now:* to surface the unique-key conflict
  immediately so the service can replay the existing job.
- **`get_by_idempotency_key`** — the lookup that makes submit idempotent.
- **`update` (the important one)** — a single conditional statement:
  `UPDATE jobs SET … WHERE id = :id AND version = :expected_version`. If
  `rowcount == 0`, we distinguish "gone" (`JobNotFound`) vs "version moved"
  (`VersionConflict`). *Why:* this is **optimistic concurrency control** — no row
  locks; the write simply fails if someone changed the row first. *Benefit:* high
  concurrency without deadlocks; the loser retries.

### 4.7 `app/api/schemas.py` — DTOs (the HTTP contract)
- **`JobCreate`** — request body; `payload: dict = Field(min_length=1)` rejects
  empty submissions → automatic `422`. *Why separate from the domain `Job`:* the
  public API contract shouldn't leak internal fields and can evolve independently.
- **`JobOut` (`from_attributes=True`, `from_entity`)** — response shape, built
  straight from a domain object. **`JobEventOut`** — one event-log entry (Stage 1).

### 4.8 `app/api/deps.py` — dependency injection
- **`get_session`** — yields one `AsyncSession` per request inside `async with`,
  **committing on success and rolling back on error**. *Why:* the unit-of-work
  pattern — a request either fully applies or not at all. *Benefit:* atomicity for
  free; the job update and its event commit together.
- **`get_job_repository` / `get_event_store`** — return the *port* type, built on
  the request's session. FastAPI caches `get_session` per request, so repo and
  event store share one session (→ one transaction).
- **`_get_redis` (`@lru_cache`, lazy import)** — one Redis client per process,
  created on first use. *Why lazy:* keeps `redis` out of test imports.
- **`get_job_service`** — composes repo + event store + queue into the
  `JobService`. *Benefit:* routes ask for a use-case object, not plumbing.

### 4.9 `app/api/errors.py` — errors → HTTP, in one place
- **`_envelope(...)`** — the single JSON error shape
  `{"error": {code, message, correlation_id}}`. *Benefit:* clients parse one
  format; every error carries the correlation id for tracing.
- **`_DOMAIN_MAP`** — maps `JobNotFound→404`, `InvalidTransition→409`,
  `VersionConflict→409`. *Why here and nowhere else:* the domain raises business
  errors; *this* module is the only place that knows HTTP. *Benefit:* change a
  status code in one spot.
- **validation & catch-all handlers** — reshape Pydantic errors to `422` in our
  envelope; the catch-all returns `500` without leaking a stack trace. *Benefit:*
  consistent, safe error output.

### 4.10 `app/api/routes_health.py` — liveness vs readiness
- **`/healthz` (liveness)** — "is the process alive?" No dependencies. *Use:*
  Kubernetes restarts the pod if this fails.
- **`/readyz` (readiness)** — "can I serve traffic?" Runs `SELECT 1`; returns
  `503` if the DB is down. *Use:* K8s takes the pod out of the load balancer but
  does **not** restart it (a restart won't fix the DB). *Why separate:* conflating
  them causes restart loops. Good detail to mention in interviews.

### 4.11 `app/core/config.py` — 12-factor config
- **`Settings(BaseSettings)`** — typed config read from env vars / `.env`
  (`database_url`, `redis_url`, `db_echo`, …). *Why:* 12-factor factor III — config
  lives in the environment, not code. **`get_settings()` `@lru_cache`d** — parsed
  once. *Benefit:* one typed source of truth; no `os.getenv` scattered around.

### 4.12 `app/core/db.py` — DB engine (lazy)
- **`Base(DeclarativeBase)`** — parent for ORM models.
- **`get_engine` / `get_sessionmaker` (`@lru_cache`, lazy)** — build the engine on
  first use, not at import. *Why lazy (important):* creating the engine eagerly
  makes *importing this module* pull in the `asyncpg` driver, which would force
  the (DB-free) tests to install a driver they never use. Deferring it keeps
  imports cheap and tests hermetic, while still being a per-process singleton.
- **`create_all()`** — creates tables from metadata on startup. *Why a shortcut:*
  makes a fresh clone runnable immediately; **Alembic migrations** replace this
  later. Called out so it's not mistaken for production practice.

### 4.13 `app/core/correlation.py` — correlation ids
- **`_correlation_id: ContextVar`** — async-aware per-request storage. *Why a
  ContextVar not a global:* many requests share the event loop; each needs its own
  value.
- **`CorrelationIdMiddleware`** — honours an incoming `X-Request-ID` or mints a
  UUID, binds it for the request, echoes it on the response, resets it in
  `finally`. *Benefit:* every log line/error can carry the id → you can trace one
  request end-to-end. This is the seed of distributed tracing (Stage 12).

### 4.14 `app/main.py` — composition root
- **`lifespan`** — runs `create_all()` once at startup. *Why a context manager:*
  clean startup/shutdown hooks.
- **`create_app()` (factory)** — builds the app, adds the correlation middleware,
  installs error handlers, mounts routers. *Why a factory function (not module-
  level):* tests build their own instance with overridden dependencies.
- **`app = create_app()`** — the module-level object uvicorn imports.

---

## 5. Stage 1 — the async core (block-by-block)

**Idea:** stop doing work inside the request. Accept fast (`202 + id`), queue the
job, and let a worker process it, recording every step. **Motive:** learn and
demonstrate the core distributed-systems mechanics — durable queue, worker,
state machine, **event sourcing**, **at-least-once + idempotency**, and
cancellation.

### 5.1 `app/domain/events.py` — event sourcing
- **`JobEventType` enum** — `submitted, started, succeeded, failed, cancelled`.
- **`@dataclass(frozen=True) class JobEvent`** — an immutable fact: `job_id, type,
  data, id, at`. *Why `frozen`:* you never edit an event, only append new ones —
  that immutability is what makes the log trustworthy. *Why keep a log at all when
  we already store current status:* audit/debugging, **replay** (state =
  fold(events)), and it's the foundation CQRS read models and CDC build on
  (Stage 5). *Benefit:* full, ordered history of everything that happened.

### 5.2 `app/ports/event_store.py` — the EventStore port
- **`append` + `list_for_job` only** — deliberately no update/delete. *Why:* an
  event store only ever grows. *Benefit:* the append-only guarantee is encoded in
  the interface itself.

### 5.3 `app/ports/job_queue.py` — the JobQueue port
- **`@dataclass(frozen=True) QueueMessage {msg_id, job_id}`** — a delivery:
  `msg_id` is the queue's handle (used to ack); `job_id` is our payload.
- **`enqueue / consume / ack / reclaim_stale`** — the interface is shaped around a
  **consumer group with explicit acknowledgement**, which is what gives
  **at-least-once** delivery: `consume` hands out a message but keeps it *pending*
  until `ack`; if a worker dies before acking, `reclaim_stale` re-delivers it.
  *Why model it this way:* at-least-once means "never silently lose a job," at the
  cost that a message *may* arrive twice — so the worker must be idempotent.
  *Benefit:* durability + a clear contract both adapters honour.

### 5.4 `app/adapters/memory/{event_store,queue}.py` — in-memory twins
- **`InMemoryEventStore`** — dict `job_id → [events]`, append + list.
- **`InMemoryJobQueue`** — `_ready` deque + `_pending` dict + a deterministic
  counter for `msg_id` (no clock/randomness → reproducible tests). `consume` moves
  ready→pending; `ack` drops from pending; `reclaim_stale` returns what's still
  pending. *Benefit:* exercises the worker's at-least-once/idempotency logic with
  zero infrastructure. `depth()` is a test-only helper.

### 5.5 `app/adapters/postgres/event_models.py` — the `job_events` table
- **`seq` BigInteger autoincrement primary key** — a DB-assigned monotonic order
  key. *Why order by `seq` not `at`:* wall-clock times can tie or go backwards;
  `seq` gives a stable **total order**. *Benefit:* the log always reads back in the
  true order things happened.
- **No UPDATE/DELETE anywhere** — append-only enforced by convention + the port.

### 5.6 `app/adapters/postgres/event_store.py` — Postgres EventStore
- **shares the caller's `session`** — so appending an event and updating the job
  commit in **one transaction**. *Benefit:* state change and its event are atomic
  (can't get one without the other within a single DB).
- **`list_for_job` orders by `seq.asc()`** — stable history.

### 5.7 `app/adapters/redis/queue.py` — Redis Streams adapter
- **`STREAM`, `GROUP` constants** — one stream, one consumer group named
  `workers`.
- **`ensure_group`** — `XGROUP CREATE … MKSTREAM`, ignoring `BUSYGROUP` (already
  exists). `id="$"` = only deliver messages added after the group exists.
- **`enqueue`** — `XADD` the `job_id`.
- **`consume`** — `XREADGROUP … STREAMS stream ">"`; `">"` means "new messages
  never delivered to this group." Blocks up to `block_ms`. Claimed messages become
  *pending* until acked.
- **`ack`** — `XACK` clears the message from the pending list.
- **`reclaim_stale`** — `XAUTOCLAIM` reassigns messages pending longer than
  `min_idle_ms` (their worker likely crashed). *This is the at-least-once
  guarantee in action.*
- **`_parse` helpers** — normalise Redis' nested reply shapes into `QueueMessage`.
  *Why Redis Streams over a plain list:* lists don't track per-consumer pending
  state or support acks/reclaim. *Benefit:* durable, at-least-once hand-off.
  Stage 5 swaps this for Kafka behind the same port.

### 5.8 `app/application/job_service.py` — the use cases
*The single place lifecycle logic lives, shared by the API and the worker.*
- **`submit(payload, idempotency_key) → (job, created)`** — idempotency-key
  lookup first (replay without re-enqueue if seen); else create job, append
  `SUBMITTED`, `enqueue`. Catches the unique-key race and replays. *Benefit:* the
  API route stays a thin wrapper; the "create + log + enqueue" rule is defined
  once.
- **`mark_running / mark_succeeded / mark_failed`** — thin wrappers over
  `_transition`. Used by the worker.
- **`cancel(job_id)`** — load, then transition to `CANCELLED` (legal from
  pending/running; raises if terminal). Used by the API.
- **`_transition(job, to, event_type, data)` (the shared core)** — captures
  `prev_version`, calls `job.transition_to(to)` (domain rule + version bump), then
  **persists first** with `repo.update(expected_version=prev_version)` and **only
  then** appends the event. *Why persist first:* a version conflict aborts before
  we log a bogus event. *Benefit:* the state change and its event stay consistent.
- **The atomicity/outbox comment** — the DB writes are atomic, but `enqueue` talks
  to Redis *outside* that transaction, so a crash can leave a job committed but not
  enqueued (or vice-versa). *Why called out:* it's a real distributed-systems
  gotcha; the **transactional outbox** (Stage 5) fixes it. The worker tolerates
  the gap (treats an unknown id as a no-op). Great interview talking point.

### 5.9 `app/worker.py` — the worker
- **`ProcessResult` enum** — `orphan, skipped, succeeded, failed, abandoned` — a
  readable outcome for logs/tests.
- **`process_job(job_id, *, repo, events, work_fn)` (the pure, testable core)** —
  the idempotent per-message handler:
  1. load job; `None` → `ORPHAN` (enqueued but not persisted — the outbox gap).
  2. `is_terminal` → `SKIPPED` (redelivery, or cancelled before pickup). **This is
     the idempotency guard.**
  3. if `PENDING` → `mark_running` (skip if already RUNNING = a mid-run
     redelivery).
  4. `await work_fn(job)`; on any exception → reload + `mark_failed` → `FAILED`.
  5. reload; if terminal now → `ABANDONED` (cancelled mid-run); else
     `mark_succeeded` → `SUCCEEDED`. *Why reload before completing:* the job may
     have been cancelled while we worked; the transition guard + OCC are the
     backstop. *Why ports are arguments:* so tests drive it with in-memory
     adapters — this is where the important behaviour is verified.
- **`_make_work_fn(repo)`** — the placeholder unit of work with **cooperative
  cancellation**: it "works" in a few steps, re-reading the job between steps and
  returning early if it's been cancelled. *Why cooperative:* you check "should I
  stop?" at safe points rather than being force-killed mid-write. Real work
  (Claude + tools) arrives in Stage 3.
- **`run_worker()` (the I/O loop)** — lazily imports Redis; wires the Postgres
  adapters with **a fresh DB session per message**; `ensure_group`; reclaims stale
  messages on startup (crash recovery); then loops: `consume → process_job →
  commit → ack`. **Acks only after a clean commit**; on error it rolls back and
  does *not* ack, so the message is redelivered. *Why this ordering is the whole
  ballgame:* ack-after-commit is what turns "at-least-once delivery" into
  "at-least-once *processing*" — nothing is lost, and nothing is marked done
  before it's durably done.
- **`main()`** — `asyncio.run(run_worker())` with a clean `KeyboardInterrupt`
  shutdown; enables `python -m app.worker`.

### 5.10 `app/api/routes_jobs.py` — the HTTP surface (Stage 1)
- **`submit_job` → `202 ACCEPTED`** (200 on idempotent replay) — delegates to
  `JobService.submit`. *Why 202 not 201:* we accepted the job for *async*
  processing; it isn't done. Semantically honest, and the pattern the whole
  platform is built on.
- **`get_job` / `list_jobs`** — simple reads straight from the repository.
- **`get_job_events`** — returns the append-only log (404 if the job is unknown
  rather than an empty list). *Use:* how a client (or you) sees a job's full
  history.
- **`cancel_job`** — delegates to `JobService.cancel`; domain errors become
  `404`/`409` via the error handlers. *Note:* the Stage-0 manual `/transition`
  endpoint was removed — the **worker** owns transitions now; the only client-
  driven transition that makes sense is cancel.

---

## 5B. Stage 2 — polyglot persistence (block-by-block)

**Idea:** stop forcing every kind of data into Postgres. Use the *right database
for each data shape*. **Motive:** learn and defend polyglot persistence, MongoDB
document modelling, and caching — the "data-at-scale" gap.

**The split:**
- **Postgres** — small, structured, transactional **metadata** (`jobs`) + the ordered `job_events` log. Needs uniqueness, ordering, transactions.
- **MongoDB** — the large, nested, schema-varying **document**: the input `payload` (moved out of Postgres) + the worker's `result`/trajectory. No joins needed.
- **Redis** — a **hot-state cache** for job reads.

### 5B.1 `app/domain/document.py` — the JobDocument
- **`@dataclass JobDocument {job_id, payload, result, created_at, updated_at}`** —
  the "big" part of a job, pure Python. *Why separate from `Job`:* payload/result
  are large and vary by job type; the `Job` entity keeps only metadata. *Benefit:*
  each store holds what it's good at.

### 5B.2 `app/ports/document_store.py` & `cache.py` — two new ports
- **`JobDocumentStore` (`create`, `set_result`, `get`)** — the interface for the
  big document; adapters are Mongo (real) and in-memory (tests).
- **`JobCache` (`get`, `set`, `invalidate`)** — the interface for the hot cache.
  *Design stated in the docstring:* the cache is **never the source of truth**
  (cache-aside); correctness comes from **invalidate-on-write**. *Benefit:* you
  can wipe Redis anytime and the system still works from Postgres.

### 5B.3 In-memory adapters (`adapters/memory/document_store.py`, `cache.py`)
- Dict-backed implementations for tests. The cache one adds `hits`/`misses`
  counters so tests can *prove* cache-aside behaviour. *Benefit:* the whole
  polyglot flow is tested with zero infrastructure.

### 5B.4 `app/adapters/mongo/document_store.py` — MongoDB adapter
- **one document per job, `_id = str(job_id)`, payload+result inline** — the
  classic Mongo "**embed, don't join**" model: fetch everything about a job's data
  in one round trip. *Why Mongo:* schema-flexible, document-shaped, no relational
  joins. **`set_result` uses `upsert=True`** — safe even if create didn't land.
- Uses `motor`, the async Mongo driver (lazy-imported in `deps`).

### 5B.5 `app/adapters/redis/cache.py` — Redis cache adapter
- **JSON-serialises the job under `job:{id}` with a short TTL (`ex=30`)** — a burst
  of polls is served from Redis, not Postgres. *Why a TTL:* a safety net — even if
  an invalidation is somehow missed, the entry self-expires. The port speaks in
  domain `Job`, so (de)serialization is hidden here.

### 5B.6 The Postgres change — `jobs` becomes metadata-only
- **`domain/job.py`** drops the `payload` field; **`adapters/postgres/models.py`**
  drops the `payload` column; **`repository.py`** stops mapping it. *Why:* the
  payload now lives in Mongo. *Benefit:* the relational row is small and purely
  transactional. (In real life this is an Alembic migration — noted as a gap.)

### 5B.7 `app/application/job_service.py` — the coordination changes
- **`submit` writes the Mongo document FIRST**, then the Postgres row + event +
  enqueue. *Why that order:* the payload must exist before the job is
  visible/enqueued (the worker reads it from Mongo). If the Postgres insert then
  fails, the Mongo doc is a harmless orphan.
- **`_transition` calls `cache.invalidate(job.id)`** after every state change —
  invalidate-on-write.
- **The consistency note (interview gold):** a submit touches THREE stores (Mongo,
  Postgres, Redis) and only the Postgres writes are transactional. Small crash
  windows exist (orphan doc; committed-but-not-enqueued). The full fix is the
  transactional **outbox + CDC** in Stage 5. Naming this shows you understand
  distributed-data consistency.

### 5B.8 `app/api/deps.py`, `routes_jobs.py`, `schemas.py`
- **deps:** `_get_mongo_client` (lazy singleton), `get_document_store`,
  `get_job_cache`; `get_job_service` now wires docs + cache.
- **routes:** `get_job` is now **cache-aside** (try cache → miss → Postgres →
  populate); new **`GET /{id}/document`** returns the Mongo doc.
- **schemas:** `JobDocumentOut` DTO.

### 5B.9 `app/worker.py` — the worker changes
- **`process_job` now takes `cache`** and builds `JobService(repo, events,
  cache=cache)` — so the worker's own transitions invalidate the cache too.
  *Why it matters:* otherwise an API read could serve a stale status after the
  worker advanced the job.
- **`_make_work_fn(repo, docs)`** now reads the `payload` from Mongo, "works", and
  writes a `result` document — the shape the real agent loop (Stage 3) will fill.

---

## 5C. Stage 3 — the agent layer (block-by-block)

**Idea:** replace the placeholder work with a real **agent** — a model that
plans, calls tools, observes results, and iterates until it answers — then
persist and stream everything it does. **Motive:** this is the platform's reason
to exist and the deepest interview area (the agent/LLM layer).

### 5C.1 `app/ports/llm.py` — the LLMClient port
- **`LLMClient.create_turn(system, messages, tools, max_tokens) -> LLMTurn`** —
  one model round-trip. `LLMTurn` carries the assistant `text`, any `tool_calls`,
  the `stop_reason`, token `usage`, and `raw_assistant_content` (opaque blocks to
  replay next turn). *Why a port here matters most:* the real adapter costs money
  and needs a key — the port lets tests use a **scripted fake**. Provider-swap is
  also just a new adapter.

### 5C.2 `app/adapters/anthropic/llm.py` — the real client
- The **only** file that imports the Anthropic SDK. `messages.create(...)`, then
  normalise `resp.content` blocks into text + `ToolCall`s, and keep `resp.content`
  as `raw_assistant_content` so tool_use (and thinking) blocks replay correctly.
  Model from config (default `claude-opus-5`, overridable to cut cost). Lazy-
  imported, so tests never load the SDK.

### 5C.2b `app/adapters/groq/llm.py` — a FREE provider (the ports payoff, live)
- A second real `LLMClient` — Groq's OpenAI-compatible API (free tier, no card).
  It **translates** the loop's Anthropic-shaped messages/tools into OpenAI shape
  and maps the response back, so the agent loop, worker, and tests are unchanged.
  Selected with `LLM_PROVIDER=groq`. *This is the concrete proof of the port:*
  a new provider = one new adapter + a config switch, nothing else. The
  translation helpers are pure functions (unit-tested without the SDK).

### 5C.3 `app/adapters/memory/llm.py` — the scripted fake
- **`ScriptedLLMClient([turns...])`** returns pre-programmed turns in order,
  ignoring the messages. *Benefit:* deterministic tests of every path (direct
  answer, tool-use, refusal, budget) with no key, no network, no cost.

### 5C.4 `app/agent/tools.py` — the tool abstraction
- **`Tool {name, description, input_schema, run}`** — description + schema go to
  the model (so it knows when/how to call), `run` is what *we* execute. The model
  decides; our code executes — the core of safe tool use.
- **`calculator`** evaluates arithmetic via a **restricted AST walk**, not
  `eval` — the model can't smuggle code through it. Deterministic + side-effect-
  free, so the loop is easy to test.

### 5C.5 `app/agent/loop.py` — the agent loop
- **`run_agent(task, *, llm, tools, ...)`** — the plan→act→observe cycle: call
  the model; if `stop_reason == "tool_use"`, run each tool and feed the results
  back as a `tool_result` user message; repeat until `end_turn`. It appends the
  raw assistant content before the tool results (correct replay), records every
  step into a **trajectory**, and calls `on_step` per step (for live streaming).
- **Guardrails (why an agent needs them):** a **token budget** (hard ceiling on
  total tokens → no runaway spend), **max steps** (no infinite loop), and
  **cooperative cancellation** (`should_cancel` checked between steps). Each stop
  reason is recorded (`done`/`budget`/`max_steps`/`refusal`/`cancelled`).
- **Refusal handling:** `stop_reason == "refusal"` is checked before reading
  content, and ends the loop cleanly.

### 5C.6 Streaming — `ports/stream_bus.py` + adapters
- **`StreamBus.publish/subscribe`** — the worker (which runs the agent) publishes
  each step; the gateway's SSE endpoint subscribes and forwards. Cross-process in
  real runs → **Redis pub/sub** (`adapters/redis/stream_bus.py`); one-process in
  tests → **in-memory** (`adapters/memory/stream_bus.py`). A terminal
  `{"type":"end"}` closes the stream.
- **`GET /v1/jobs/{id}/stream`** returns a `StreamingResponse` of
  `text/event-stream`, yielding `data: {json}\n\n` per event. *Known gap:* it's
  live-only (no replay of steps emitted before you connect) — a production version
  would replay the Mongo trajectory first, then go live.

### 5C.7 `app/worker.py` — wiring the agent in
- **`_make_agent_work_fn(repo, docs, bus, llm, settings)`** replaces the echo:
  read the task from the payload, run `run_agent` with `on_step` publishing to the
  bus and `should_cancel` re-reading the job, then store `{answer, trajectory,
  usage, stopped}` to Mongo and publish `end`. A non-`done` stop **raises**, so
  `process_job` marks the job `FAILED` with the reason.

---

## 5D. Stage 4 — resilience (block-by-block)

**Idea:** the provider call is the flaky part (network, rate limits, outages).
Wrap it so transient failures are absorbed and a dead dependency can't take the
whole system down — and shed load at the edge before it overwhelms us.
**Motive:** this is one of the three "go-to-90%" interview anchors; every
pattern here is standard senior-SRE vocabulary.

**Key move:** resilience is a **decorator over the `LLMClient` port**, so the
agent loop is untouched — same trick as every other stage.

### 5D.1 `app/resilience/retry.py` — retry with backoff + jitter
- **`RetryPolicy`** (max_attempts, base_delay, max_delay) + **`run_with_retry`**.
- **Exponential backoff** (`base·2^(n-1)`, capped) so we don't hammer a
  struggling dependency; **full jitter** (`uniform(0, backoff)`) so many clients
  don't retry in lockstep (thundering herd). Only retries what `is_retryable`
  approves — a permanent 400 fails at once. `sleep`/`rand` are injected so tests
  are instant and deterministic.

### 5D.2 `app/resilience/circuit_breaker.py` — fail fast when a dep is down
- **`CircuitBreaker`** with CLOSED / OPEN / HALF_OPEN. Trips to OPEN after
  `failure_threshold` consecutive failures; OPEN rejects instantly
  (`CircuitOpenError`) for `reset_timeout`; then HALF_OPEN lets **one** probe
  through — success closes it, failure reopens. *Why:* retrying a genuinely-down
  provider just piles on load and makes every caller wait; the breaker frees them
  immediately and gives the dep room to recover. Injectable clock for tests.

### 5D.3 `app/resilience/rate_limiter.py` — token bucket
- **`TokenBucket`** (capacity + refill/sec): steady average rate with room for
  short bursts — the shape real traffic has. Used for admission control (shed) at
  the edge, and reusable to throttle provider calls. In-memory here; Redis for
  the distributed version.

### 5D.4 `app/resilience/errors.py` — the retry classifier
- `is_retryable(exc)` decides transient vs permanent **generically** (by
  exception type, class-name hint, or HTTP status) so it works across provider
  SDKs without importing them. `CircuitOpenError` and `RateLimited` live here too.

### 5D.5 `app/adapters/resilient/llm.py` — `ResilientLLMClient` (the centerpiece)
- Implements `LLMClient`, wraps a **primary** and optional **fallback** client.
  Per call to a provider: **circuit breaker → retry(backoff+jitter) → timeout**
  (`asyncio.wait_for`). If the primary is exhausted or its breaker is open, it
  tries the fallback — **each provider has its own breaker**, so one being down
  doesn't hide the other's health. The agent loop calls `create_turn` and is
  none the wiser.

### 5D.6 `app/adapters/redis/rate_limiter.py` — distributed limiter
- **`RedisTokenBucket`** implements the bucket in Redis via a **Lua script**, run
  atomically. *Why atomic:* two gateway replicas must not both consume "the last
  token" (a race); one Lua check-and-decrement is indivisible and shared by all
  replicas, so the limit is global, not per-process.

### 5D.7 Wiring — worker + gateway
- **Worker** (`worker.py`) builds the provider(s) and wraps them in
  `ResilientLLMClient` (retry/timeout/breaker from config, optional
  `LLM_FALLBACK_PROVIDER`). One line; the agent loop unchanged.
- **Gateway** (`routes_jobs.py` + `deps.py`): `submit_job` calls the shared
  `RedisTokenBucket`; over the limit it raises `RateLimited`, which
  `api/errors.py` maps to **429 + `Retry-After`** — **load shedding** as
  admission control.

---

## 6. Cross-cutting design decisions & trade-offs

| Decision | Why | Trade-off / later fix |
|---|---|---|
| Hexagonal architecture | testability, replaceable infra | more files/indirection up front |
| `202 + id` accept-fast | responsiveness under load; decouple accept from process | client must poll/stream for result |
| Event sourcing (log + current state) | audit, replay, foundation for CQRS/CDC | more writes; current state also stored (not pure ES yet) |
| Optimistic concurrency (version) | no locks → high concurrency | losers must retry |
| At-least-once + idempotent worker | never lose a job | must handle duplicate delivery (we do) |
| Ack-after-commit | never mark done before durable | a crash after commit, before ack → one safe redelivery |
| `create_all()` on startup | instant runnability | not migration-safe → Alembic later |
| Redis Streams queue | durable, at-least-once, simple | not a full log/replay bus → Kafka in Stage 5 |
| Worker inside gateway package | one deployable early | split into own Go service in Stage 7 |
| Enqueue outside the DB tx | simple | crash window → **transactional outbox** in Stage 5 |
| Polyglot: payload/result in Mongo | document-shaped, schema-flexible, no joins | multi-store writes aren't atomic → outbox/CDC (Stage 5) |
| Redis cache-aside + invalidate-on-write | fast reads, absorb poll storms | cache can briefly lag; TTL is the backstop |
| Cache also invalidated by the worker | reads never serve a stale status | worker must know the cache (threaded in) |
| Resilience as a decorator over the LLM port | agent loop unchanged; composable | one more layer to reason about |
| Retry + backoff + **jitter** | absorbs transient blips without stampedes | adds latency on failures; needs a retryable/permanent split |
| Circuit breaker per provider | fail fast when a dep is down; independent health | a flapping dep can oscillate; tune threshold/cooldown |
| Fallback provider chain | survive one provider's outage | fallback may cost/behave differently; not always desirable |
| Distributed (Redis+Lua) rate limit | one global limit across replicas | a Redis round-trip per request; Redis is now on the submit path |
| LLM behind a port + scripted fake | tests run with no key/network/cost | one more abstraction layer |
| Agent guardrails (token budget, max steps) | bound cost + prevent infinite loops | may stop before a hard task finishes |
| SSE via Redis pub/sub | live progress across processes | live-only (no replay) until Stage 5-style history stream |
| Manual agent loop (not the SDK tool-runner) | full control to record + stream each step | more code than the runner |

---

## 7. Interview Q&A — what / why / how

**Q: Walk me through what happens when a job is submitted.**
Validated by Pydantic → `JobService.submit` inserts a `pending` job, appends a
`SUBMITTED` event, and enqueues the id on Redis Streams → API returns `202 + id`
in milliseconds. A worker later consumes the id, moves it `running`, does the
work, and marks it `succeeded`/`failed`, appending an event at each step. The
client polls `GET /v1/jobs/{id}` or reads `GET /v1/jobs/{id}/events`.

**Q: Why 202 and not 201?**
The work is asynchronous — the job is *accepted*, not *completed*. `202` says
"I've taken this and will process it," which is honest and lets the API stay
responsive regardless of how long the work takes.

**Q: How do you guarantee a retried submit doesn't create duplicates?**
Idempotency key. `submit` first looks it up and replays the existing job if
found. Under a race, a **unique DB constraint** on `idempotency_key` rejects the
second insert, and we fetch+replay. Application check + DB constraint together.

**Q: How do you handle concurrent updates to the same job?**
Optimistic concurrency: each job has a `version`. Updates are a conditional
`UPDATE … WHERE version = expected`. If another writer already bumped it, zero
rows match and we return `409 version_conflict` instead of silently overwriting.
No row locks → better concurrency.

**Q: The queue is at-least-once — how is the worker correct under duplicate
delivery?**
`process_job` is idempotent: it inspects the job first. A terminal job is
skipped; only a `pending` job is *started*. And we **ack only after the DB
commit**, so a crash mid-processing redelivers the message and it's safely
re-run, never lost, never double-completed.

**Q: What's event sourcing buying you here?**
An append-only, ordered history of every state change — audit/debugging, the
ability to reconstruct state by folding events, and the substrate for CQRS read
models and CDC to a warehouse later. Events are immutable (`frozen`) and ordered
by a DB-assigned `seq`.

**Q: How does cancellation work with a running worker?**
Cooperative. Cancelling sets the job to `cancelled` (a legal transition from
pending/running). The worker re-reads the job between work steps and stops if
it's terminal; and even if it races to complete, the transition guard + OCC
refuse to move a cancelled job to succeeded. So no forced kill, no corruption.

**Q: Why the ports-and-adapters structure? Show me the payoff.**
The domain and use cases depend on interfaces (`JobRepository`, `EventStore`,
`JobQueue`), not on Postgres/Redis. Tests inject in-memory adapters, so the whole
API and worker logic is verified in milliseconds with no infrastructure — and
Stage 5 can replace Redis with Kafka by writing one new adapter.

**Q: Why did you move the payload to MongoDB instead of keeping it in Postgres?**
Data-shape fit. The payload/result are large, deeply nested, and vary by job
type, and I never join on them — that's a document, not a relational row. So they
live in Mongo (one document per job, embedded), while the small transactional
metadata (status, version, idempotency key) stays in Postgres where uniqueness,
ordering, and transactions matter. Right database per data shape.

**Q: A submit now writes to Mongo, Postgres, and Redis — how do you keep them
consistent?**
I don't get a distributed transaction across them, and I say so. I order the
writes to fail safe: the Mongo document first (so the payload exists before the
job is visible), then the Postgres row + event (one transaction), then the
enqueue. The leftover windows (an orphan Mongo doc, or committed-but-not-enqueued)
are bounded and the worker treats an unknown id as a no-op. The correct fix is a
transactional **outbox + change-data-capture** in Stage 5.

**Q: How does the cache stay correct?**
Cache-aside for reads (try Redis, miss → Postgres → populate) and
invalidate-on-write for changes: `JobService` deletes the cache entry on every
transition, and the **worker uses a cache-aware JobService too**, so a status it
advances is invalidated immediately. A short Redis TTL is the backstop if an
invalidation is ever missed. The cache is never the source of truth.

**Q: Mongo modelling — embed or reference?**
Embed. Everything about a job's data is one document keyed by the job id, fetched
in a single round trip. There's no cross-document relationship that would justify
references/joins here — that's exactly the workload document DBs are best at.

**Q: Walk me through the agent loop.**
Given a task, `run_agent` calls the model through the `LLMClient` port. If the
model returns `tool_use`, I run each requested tool locally, append the raw
assistant turn plus a `tool_result` message, and call again — plan → act →
observe. It repeats until `end_turn` (final answer) or a guardrail fires. Every
step is recorded to a trajectory and emitted via `on_step` for live streaming.

**Q: How do you stop an agent from running away (cost/loops)?**
Three guardrails: a **token budget** (sum of input+output tokens across the loop;
stop when exceeded), a **max-steps** cap (no infinite loop), and **cooperative
cancellation** (re-check the job's status between steps). Each produces a
distinct, recorded stop reason, and a non-`done` stop marks the job failed with
that reason.

**Q: How is any of this testable without an API key?**
The model is behind the `LLMClient` port. Tests inject a `ScriptedLLMClient` that
returns canned turns — so I can drive the exact tool-use, refusal, and
budget-exceeded paths deterministically, with no key, network, or cost. The
Anthropic adapter is the only thing that touches the SDK.

**Q: How does the live SSE stream work if the agent runs in the worker?**
The worker publishes each step to a `StreamBus`; the gateway's SSE endpoint
subscribes and forwards. Across processes that's Redis pub/sub; the API just
relays `data: {json}` frames. It's live-only today (a known gap) — production
would replay the stored trajectory from Mongo first, then go live.

**Q: Is tool use safe?**
The model only *requests* a tool; our code decides whether/how to run it. The
calculator evaluates via a restricted AST (numbers + operators only), never
`eval`, so the model can't execute arbitrary code. Tools are deterministic and
side-effect-free at this stage.

**Q: What are the known weaknesses?**
(1) Multi-store writes (Mongo/Postgres/Redis) aren't atomic → transactional
outbox + CDC in Stage 5. (2) Enqueue is outside the DB transaction → same fix.
(3) Schema changes use `create_all()`, not migrations → Alembic. (4) The worker
shares the gateway's package → split into its own service in Stage 7. (5) The SSE
stream is live-only (no replay). (6) No retry/backoff around the model call yet →
Stage 4 resilience. Naming these unprompted signals seniority.

---

**Q: Retries can make an outage worse — how do you retry safely? (Stage 4)**
Three ways. (1) Only retry *transient* failures (timeouts, 429, 5xx); a 400 fails
immediately — no point retrying a bad request. (2) **Exponential backoff** so I
wait longer each time instead of hammering a struggling dependency. (3) **Full
jitter** so many workers don't retry in lockstep and create a thundering herd.

**Q: What does the circuit breaker add on top of retries?**
Retries handle a blip; the breaker handles a *sustained* outage. After N
consecutive failures it OPENs and rejects calls instantly for a cooldown —
callers fail fast instead of each waiting through the full timeout+retries, and
the dead dependency gets breathing room. After the cooldown it half-opens, lets
one probe through, and closes on success. Each provider gets its own breaker.

**Q: How does a provider outage not fail the job?**
`ResilientLLMClient` wraps a primary and a fallback. If the primary is exhausted
or its breaker is open, it transparently calls the fallback (its own breaker).
Set `LLM_FALLBACK_PROVIDER` and, say, a Groq outage falls back to Anthropic —
the agent loop never knows.

**Q: Where and how do you shed load?**
At the edge — `POST /v1/jobs` consults a token-bucket rate limiter and returns
**429 + Retry-After** when over the limit (admission control). Rejecting early is
far cheaper than accepting work that backs up and collapses the system. The
bucket lives in Redis (atomic Lua), so the limit is **global across gateway
replicas**, not N× too high.

**Q: Why is the rate limiter in Redis and not in-process?**
With multiple gateway replicas, per-process buckets would each allow the full
rate, so the real limit would be replicas × configured. A shared Redis bucket,
decremented atomically by a Lua script, enforces one global limit and avoids the
race where two replicas both consume the last token.

## 8. Known gaps and what later stages fix
- **MongoDB trajectories + Redis hot state** (polyglot) → **Stage 2**. ✅ done
- **Multi-store consistency** (Mongo+Postgres+Redis not atomic) → transactional **outbox + CDC**, **Stage 5**.
- **Transactional outbox** (enqueue-in-transaction) → **Stage 5**.
- **Alembic migrations** (vs `create_all`) → housekeeping, next.
- **Real work** (Claude agent loop + tool use) replacing the echo placeholder → **Stage 3**. ✅ done
- **RAG** (embeddings + vector retrieval) added to the agent → later in Stage 3/expansion.
- **SSE trajectory replay** (not just live) → Stage 5-style history stream.
- **Retries/backoff/circuit breaker/fallback + rate-limit/load-shed** → **Stage 4**. ✅ done
- **Provider-side call throttling** (reuse the token bucket to respect provider limits) — small follow-on to Stage 4.
- **Kafka backbone, CQRS read models, CDC + transactional outbox** → **Stage 5**.

## 9. Glossary
- **Accept-fast / 202+id** — return an id immediately, process later.
- **Idempotency** — same request twice = same effect once.
- **Optimistic concurrency (OCC)** — version-checked writes instead of locks.
- **Event sourcing** — state changes stored as an append-only event log.
- **At-least-once** — a message is delivered one or more times (never zero).
- **Idempotent consumer** — safe to process the same message more than once.
- **Unit of work** — a set of writes that commit or roll back together.
- **Port / adapter** — an interface the app depends on / its concrete impl.
- **Cooperative cancellation** — the worker checks for "stop" at safe points.
- **Correlation id** — a per-request id threaded through logs for tracing.
