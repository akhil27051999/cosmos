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
5E. **Stage 5 (part 1)** — transactional outbox + event backbone (block-by-block)
5F. **Stage 5 (part 2)** — CQRS read model (block-by-block)
5G. **Stage 6** — multi-tenancy & identity (block-by-block)
5H. **Stage 7 (part 1)** — OAuth2/OIDC JWT + scopes (block-by-block)
5I. **Stage 7 (part 2)** — typed gRPC control plane (block-by-block)
5J. **Stage 8 (part 1)** — Kubernetes packaging with Helm (block-by-block)
5K. **Stage 8 (part 2)** — custom operator + CRD (block-by-block)
5L. **Stage 9** — A/B experimentation & feature flags (block-by-block)
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

## 5E. Stage 5 (part 1) — transactional outbox + event backbone (block-by-block)

**Idea:** give the platform a durable, replayable **event backbone** (Kafka/
Redpanda), and get events onto it **reliably** — never losing one to a crash
between the DB write and the publish. **Motive:** this is the substrate for CQRS,
CDC, and analytics, and the **transactional outbox** is the canonical fix for the
dual-write problem we've flagged since Stage 1.

**The dual-write problem:** writing to Postgres and publishing to the bus are two
systems; a crash between them loses one. You can't wrap both in one transaction.

**The outbox fix:** make the publish a *database write*. In the same transaction
as the event, insert an `outbox` row. Now it's a single atomic commit. A separate
relay reads the outbox and publishes — decoupling correctness (the DB commit)
from delivery (best-effort, retried).

### 5E.1 `app/ports/message_bus.py` — the backbone port
- `MessageBus` = `publish` / `poll` / `commit` over topics + consumer groups —
  the Kafka model (durable log, independent groups, replayable offsets). Only
  `publish` is used this increment (the relay); `poll`/`commit` are for the CQRS
  projector next.

### 5E.2 `app/ports/outbox.py` — the outbox port
- Split into **`OutboxWriter`** (`add`, shares the caller's transaction) and
  **`OutboxRelay`** (`fetch_unpublished` / `mark_published`, the drainer's side).
  The docstring states the pattern and the at-least-once consequence.

### 5E.3 `app/adapters/postgres/outbox_models.py` + `outbox.py`
- An `outbox` table (`topic, key, value, created_at, published_at`) with a
  **partial index on unpublished rows** so the relay's poll stays cheap as the
  table grows. `PostgresOutbox` (writer) shares the request/worker session;
  `PostgresOutboxRelay` (drainer) uses its own short sessions.

### 5E.4 `app/adapters/redpanda/message_bus.py`
- `RedpandaMessageBus` — a Kafka producer via `aiokafka` (Redpanda speaks the
  Kafka protocol). `enable_idempotence=True` avoids duplicate/reordered producer
  writes; the **key = job id** so a job's events stay ordered on one partition.
  Consumer side lands with the projector.

### 5E.5 `app/relay.py` — the relay loop
- `relay_once` (pure/testable): fetch a batch → **publish each, THEN mark
  published**. `run_relay`: loop, sleeping when idle, surviving transient bus
  errors. The publish-before-mark ordering is what makes delivery at-least-once
  (a crash re-publishes, never drops).

### 5E.6 Wiring — service + worker
- **`JobService._stage_event`** adds an outbox row (topic `job-events`) right
  after each event append — in the SAME session/transaction, so it commits with
  the state change. Wired into `submit` and every `_transition`.
- **The worker** runs the relay as a background `asyncio` task alongside the job
  loop, and builds a `PostgresOutbox` per message so the worker's own
  started/succeeded events are staged transactionally too. (In prod the relay is
  its own deployable; co-located here for local simplicity.)

> **Note on the Redis enqueue:** job *dispatch* still uses the Stage-1 Redis
> queue directly (a small dual-write window remains there). The exact same outbox
> pattern will replace that enqueue when we move dispatch onto the backbone —
> this increment proves the pattern on the event stream first.

---

## 5F. Stage 5 (part 2) — CQRS read model (block-by-block)

**Idea:** split reads from writes. The **write model** (jobs + job_events, tuned
for correctness: state machine, OCC, event log) is a poor shape for queries like
"how many jobs per status?" or "give me one job's status fast." So we build a
separate **read model** — a denormalized projection — fed asynchronously from the
event backbone we just laid down. This is **CQRS** (Command Query Responsibility
Segregation). **Motive:** reads and writes have different shapes, loads, and
scaling needs; separating them lets each be optimized independently, and the
read model can be rebuilt at any time by replaying the log.

**The flow now end-to-end:**
`service` stages an event (+ outbox row, one commit) → `relay` publishes to the
`job-events` topic → **`projector`** (a consumer group) folds each event into the
read model → the API serves it from the read model. Every hop after the commit is
asynchronous, so the read model is **eventually consistent** with the write side.

### 5F.1 `app/domain/read_model.py` — the projection shape
- `ReadModelEntry(job_id, status, version, event_count, created_at, updated_at)`
  — a flat, query-friendly row. It carries `version` because that's what makes
  the projection **idempotent** (below), and `event_count` so we can see how many
  events folded in (and prove no double-counting).

### 5F.2 `app/ports/read_model.py` — the ReadModelStore port
- `apply(event) -> bool` (fold one event; returns whether it changed anything),
  `get(job_id)`, and `stats()` (counts per status). Returning a **bool** from
  `apply` is deliberate — it lets the projector and tests assert idempotency
  (a duplicate returns `False`).

### 5F.3 `app/ports/message_bus.py` — the consumer side lands
- Part 1 only used `publish`. Now the projector needs `poll` (read a batch for a
  consumer group) and `commit(group)` (advance that group's offset after a batch
  is safely applied). `commit` takes the **group** so different consumers track
  independent offsets over the same log — the whole point of Kafka-style groups.

### 5F.4 Adapters — `memory/read_model.py` + `postgres/read_model.py`
- **In-memory** (`InMemoryReadModel`): a dict keyed by job id, with the
  **last-writer-wins by version** rule (ignore an event whose `version` ≤ the
  stored one). Used by every test — the projector is exercised with no infra.
- **Postgres** (`PostgresReadModel`): a `job_read_model` table, upserted with one
  atomic statement:
  `insert(...).on_conflict_do_update(..., where=(row.version < excluded.version))`.
  The `WHERE` on the conflict clause pushes the LWW rule **into the database**, so
  concurrent projectors (or duplicate deliveries) can't race — the DB only applies
  a strictly-newer version. This is the idempotency guarantee in one line of SQL.

### 5F.5 `app/adapters/memory/message_bus.py` — consumer semantics in a fake
- `InMemoryMessageBus` tracks `_committed` (per-group offset) and `_inflight`
  (polled-but-not-yet-committed), so a test can prove: poll returns a batch,
  redelivers it if not committed, and only advances past it on `commit`. A
  `published()` helper lets tests read the raw topic. This is what lets the CQRS
  test run the full relay→projector pipeline in-process.

### 5F.6 `app/projector.py` — the projector
- `project_once` (pure/testable): `poll` a batch → `apply` each into the read
  model → `commit(GROUP)` **only after** the whole batch is applied. If it
  crashes mid-batch it never commits, so the next poll redelivers the same
  batch — at-least-once, made safe by the idempotent `apply`.
- `run_projector`: the loop (sleep when idle, survive transient errors).
- Constants `GROUP="cqrs-projector"`, `EVENTS_TOPIC="job-events"`.

### 5F.7 `app/application/job_service.py` — events carry the version
- `_stage_event` now includes `"version": job.version` in the event payload. That
  single field is what the read model keys LWW on — without it the projection
  couldn't tell a new event from a redelivered old one. (This is why the write
  side and read side stay decoupled yet consistent: the version travels *with*
  the event, not out-of-band.)

### 5F.8 `app/api/routes_read.py` + `schemas.py` — the query surface
- A new router (`/v1`): `GET /stats` → `read_model.stats()` (counts per status),
  and `GET /jobs/{job_id}/view` → the projected `JobViewOut`, **404** if that job
  hasn't been projected yet (honest about eventual consistency — the write-side
  `GET /v1/jobs/{id}` is the immediately-consistent read).

### 5F.9 Wiring — worker + deps + main
- **The worker** starts `run_projector(event_bus, read_model)` as a second
  background task alongside the relay, so locally one process does write + relay +
  project. (In prod the projector is its own deployable consumer — that's the
  point of decoupling via the log.)
- **`deps.py`** gains `get_read_model_store()` (`PostgresReadModel(get_sessionmaker())`)
  and imports `PostgresReadModel` at module top-level so its table registers
  before `create_all()`.
- **`main.py`** mounts `routes_read`.

> **Why this is safe under at-least-once:** the relay can publish an event twice,
> and the projector can re-poll an uncommitted batch — both produce duplicate
> `apply` calls. The version-keyed LWW rule makes every duplicate (and every
> reordered older event) a **no-op**, so the read model converges to the correct
> state no matter how many times or in what order events arrive.

---

## 5G. Stage 6 — multi-tenancy & identity (block-by-block)

**Idea:** turn a single-user service into a **multi-tenant** platform. Three
things have to be true: (1) every request proves *who* it is — **authentication**;
(2) a tenant can only ever touch *its own* data — **isolation / authorization**;
(3) a tenant's consumption is **metered and bounded** — quota. **Motive:** this is
table-stakes for any SaaS/API product, and it's where "distributed system" meets
"real customers": the security boundary and the fairness boundary.

**The three planes, kept separate:**
- the **tenant plane** — normal API calls, authenticated by an *API key*;
- the **admin plane** — creating tenants/keys, authenticated by a *shared secret*
  (`ADMIN_TOKEN`), because there's a bootstrap chicken-and-egg (no key exists yet);
- the **system plane** — the worker, a trusted component that processes jobs
  across all tenants (no per-request principal).

### 5G.1 `app/domain/identity.py` — the identity domain
- `Tenant` (id, name, status, `monthly_quota`), `ApiKey` (stores only
  `secret_hash` + a displayable `prefix`, never the secret), and a **frozen**
  `Principal` (tenant_id + key_id) — the authenticated result the routes carry.
  Pure data + `is_active` rules; no framework, no crypto here.

### 5G.2 `app/auth/keys.py` — API-key crypto hygiene
- Keys look like `csk_<key_id>_<secret>`: the **key_id is a non-secret handle**
  (we index rows by it → auth is one lookup, never a scan), the **secret is
  high-entropy** and only its `sha256` is stored. `verify_secret` uses
  `hmac.compare_digest` (constant-time). **Why sha256, not bcrypt/argon2:** slow
  KDFs exist to protect *low-entropy human passwords*; a 24-byte random token is
  already infeasible to brute-force, so a fast hash is the correct, standard
  choice for API keys. Pure stdlib — trivially unit-testable.

### 5G.3 `app/ports/identity_store.py` + `usage_ledger.py`
- **`IdentityStore`**: get/add a tenant, get/add an API key (get-by-key-id is the
  auth hot path). **`UsageLedger`**: `get_usage(tenant, period)` and
  `record(tenant, period, n)` where `period` is the calendar month `"YYYY-MM"`.
  Two tiny ports, each with memory + Postgres adapters.

### 5G.4 Adapters — memory + Postgres
- **Postgres identity**: `tenants` and `api_keys` tables (api_key PK = the public
  key_id, so auth is a primary-key `get`). **Postgres usage**: one row per
  `(tenant_id, period)`; `record` is an atomic
  `INSERT … ON CONFLICT (tenant_id, period) DO UPDATE SET used = used + :n RETURNING used`
  — the increment happens **inside the DB**, so two concurrent submits can't both
  read "5" and both write "6" (the classic lost-update race).

### 5G.5 Authentication — `get_principal` (in `api/deps.py`)
- The single choke point every protected route depends on: pull the key from
  `Authorization: Bearer …` (or `X-API-Key`), `parse` → look up by key_id →
  `verify_secret` → check the tenant is active → return `Principal`. Every failure
  returns the **same 401** ("invalid API key") whether the key-id is unknown, the
  key is revoked, or the secret is wrong — so an attacker learns nothing about
  which part was wrong. A suspended tenant is **403**.

### 5G.6 Tenant isolation — threaded through the write + read models
- `Job` gains `tenant_id`; `JobModel` gets a `tenant_id` column and the
  idempotency uniqueness becomes **`(tenant_id, idempotency_key)`** (two tenants
  may reuse the same key). The repository's `get`/`list`/`get_by_idempotency_key`
  take a `tenant_id`; a **cross-tenant id reads as `None` → 404** (the id's very
  existence is never leaked — a 404, never a 403). The staged event carries
  `tenant_id`, so the **CQRS read model** stores it too and `/v1/stats` + `/view`
  are per-tenant. The worker passes `tenant_id=None` (unscoped) — it's the trusted
  system plane.

### 5G.7 Quota — `JobService.submit`
- Before doing any work: load the tenant, refuse if suspended (`Forbidden`) or if
  `used >= monthly_quota` (`QuotaExceeded` → **429** + `Retry-After`); after
  accepting, `record` +1 in the ledger. An **idempotent replay does NOT consume
  quota** (it's not new work). Check-then-record isn't one atomic unit, so a
  tenant could overshoot slightly under heavy concurrency — an accepted trade-off
  for a coarse monthly quota (the increment itself is atomic, so counts are never
  lost). This is a *different lever* from the Stage-4 rate limiter: that smooths
  bursts (req/sec, system protection); this bounds volume (req/month, fairness).

### 5G.8 The admin plane — `api/routes_admin.py`
- `POST /v1/admin/tenants` creates a tenant + mints its first key (returns the
  plaintext **once**); `POST /v1/admin/tenants/{id}/keys` mints more. The whole
  router is gated by `require_admin` (the `ADMIN_TOKEN` shared secret). **A blank
  `ADMIN_TOKEN` disables the admin API** (403) — a missing secret must never
  silently mean "no auth required".

> **What's deliberately deferred:** OAuth2/OIDC (JWT bearer tokens) as a *second*
> principal source — the `get_principal` choke point is exactly where a JWT
> verifier slots in later, returning the same `Principal`. Role-based scopes
> (read-only vs submit keys) and key rotation/expiry are also later increments.

---

## 5H. Stage 7 (part 1) — OAuth2/OIDC JWT + scopes (block-by-block)

**Idea:** two things. (1) Accept a **second credential type** — an OAuth2/OIDC
**JWT** — alongside API keys, so the platform can sit behind a real identity
provider (SSO), not just its own keys. (2) Add **authorization** on top of
authentication: a credential now carries **scopes**, and each route requires one,
so a read-only token can't submit work. **Motive:** federation (let an IdP own
identity) and least-privilege (authN says *who*; authZ says *what you may do* —
they're different questions and Stage 6 only answered the first).

**The payoff of Stage 6's design:** I built `get_principal` as the *single* auth
choke point. Adding a whole new credential scheme touches **only that function** —
every route, the service, and isolation logic are unchanged, because both schemes
produce the same `Principal`.

### 5H.1 `app/auth/jwt.py` — a dependency-free JWT verifier (HS256)
- A JWT is `header.payload.signature`, each base64url. The verifier checks the
  things that actually matter: **signature** (recompute HMAC-SHA256, constant-time
  compare — proves the token wasn't forged/tampered), **`exp`/`nbf`** with a
  clock-skew **leeway**, and **`iss`/`aud`** (so a token minted for another
  service can't be replayed here). It explicitly **rejects `alg:none` and any
  non-HS256 alg** — the classic JWT alg-confusion attack. Malformed segments/JSON
  become a clean `JWTError`, never a 500.
- **Why HS256 + pure stdlib:** HS256 is symmetric HMAC — no crypto library, so
  tests stay hermetic and the validation is fully visible. Production OIDC uses
  **RS256** (the IdP signs with a private key; we verify with its public key from
  a **JWKS** endpoint, selected by the token's `kid`). That's a later increment
  behind this same `decode_and_verify` shape — and it will use a vetted library,
  because you hand-roll HMAC but never hand-roll RSA.

### 5H.2 `app/auth/scopes.py` — the authorization vocabulary
- Constants `JOBS_READ` / `JOBS_WRITE`, an `ALL` set (what an API key gets), and
  `parse_scopes` which normalises the OAuth2 space-delimited `scope` string
  ("jobs:read jobs:write") — or a list — into a set.

### 5H.3 `Principal` gains `scopes` + `credential`
- The frozen `Principal` now carries `scopes: frozenset[str]` and `credential`
  ("api_key"|"jwt", for audit). `has_scope()` is the check `require_scope` uses.

### 5H.4 `get_principal` becomes multi-scheme (`api/deps.py`)
- One line of dispatch: a `csk_`-prefixed token → `_principal_from_api_key`
  (Stage 6, granted **full** scopes — a tenant's root credential);
  anything else → `_principal_from_jwt`. The JWT path verifies the token, reads
  the tenant from the configured claim (`jwt_tenant_claim`), confirms that tenant
  exists and is active (so suspension still applies to SSO users), and derives
  scopes from the `scope` claim. A blank `JWT_HS256_SECRET` means JWT auth is
  **off** → such tokens are "unrecognised credential" (not "wrong"), so we don't
  imply a scheme we haven't enabled.

### 5H.5 `require_scope(scope)` — the authorization gate
- A dependency **factory**: `require_scope(JOBS_WRITE)` returns a dependency that
  first runs `get_principal` (so **401** for bad/no credential happens first),
  then returns **403** if the principal lacks the scope. Routes swap
  `Depends(get_principal)` for `Depends(require_scope(...))`: submit/cancel need
  `jobs:write`; get/list/events/document/stream/stats/view need `jobs:read`.

> **Backward compatible by construction:** API keys are granted `ALL` scopes, so
> every existing Stage-0–6 route and test behaves exactly as before; only JWTs are
> constrained by their token's scopes.

> **Deferred to later:** RS256 + JWKS (real OIDC provider), key rotation/expiry
> for API keys, finer scopes (per-tenant admin, read-only keys).

---

## 5I. Stage 7 (part 2) — typed gRPC control plane (block-by-block)

**Idea:** give the gateway and worker a **typed, versioned contract** to talk over
— gRPC/protobuf — for **control-plane** operations (health, metrics). **Motive:**
so far the two processes only shared a Redis queue (fire-and-forget). A real
platform also needs synchronous, strongly-typed service-to-service calls, and gRPC
is the industry-standard for that: define the interface once in a `.proto`, and
`protoc` generates matching types + stubs for every language, so the wire format
can never drift between client and server.

**The key design line — data plane vs control plane:**
- **Data plane** (submitting/dispatching jobs) stays **asynchronous** over the
  Redis queue — that's what gives accept-fast and decoupling; a synchronous RPC
  there would reintroduce coupling and back-pressure.
- **Control plane** (is the worker healthy? how many jobs in flight?) is
  **synchronous gRPC** — a request/response question with a typed answer.
Choosing the right communication style per concern is the whole lesson here.

### 5I.1 `proto/worker_control.proto` — the contract
- A versioned package (`cosmos.worker.v1`) and one service, `WorkerControl`, with
  `GetStatus(GetStatusRequest) → GetStatusReply` (worker_id, uptime, in_flight,
  processed_total, healthy). This file — not any Python — is the source of truth;
  a breaking change ships as `v2` alongside `v1`.

### 5I.2 `app/ports/worker_control.py` + `domain/worker_status.py`
- The gateway depends on a **port** (`WorkerControlClient.get_status() ->
  WorkerStatus`), never on gRPC directly. `WorkerStatus` is a plain domain
  dataclass, kept separate from the generated protobuf type (the adapter
  translates). Same hexagonal move as every other external dependency.

### 5I.3 Adapters — fake + real
- **In-memory** (`adapters/memory/worker_control.py`): returns a settable
  `WorkerStatus`. Tests and local runs use it — **no gRPC required**.
- **Real gRPC** (`adapters/grpc/worker_control.py`): a `GrpcWorkerControlClient`
  (lazy channel/stub) and a `serve_worker_control(port, snapshot)` server helper.
  Both **import `grpc` and the generated stubs lazily**, so nothing on the test
  import path pulls in gRPC. The generated `*_pb2` modules import each other by
  bare name, so the adapter puts their directory on `sys.path` before importing —
  the simplest way to consume protoc's Python output regardless of package layout.

### 5I.4 Codegen — build-time, not committed
- The Dockerfile runs `python -m grpc_tools.protoc … proto/worker_control.proto`
  into `app/adapters/grpc/gen/` (gitignored). Generated code isn't source, so it
  doesn't belong in git; the build regenerates it, and the same command is
  documented for local runs. `grpcio`/`grpcio-tools` are in requirements but only
  the runtime (`grpcio`) is needed to *run*, and neither is needed to *test*.

### 5I.5 The worker side — metrics + server
- A tiny `WorkerMetrics` holder tracks `in_flight` (bumped around each
  `process_job`) and `processed_total`; `snapshot()` produces a `WorkerStatus`.
  `run_worker` starts the gRPC server as one more background component alongside
  the relay and projector — **best-effort**: if gRPC/codegen isn't available it
  logs and keeps the data plane running (the queue loop never depends on it).

### 5I.6 The gateway side — route + failure mapping
- `GET /v1/admin/worker/status` (on the admin plane — operational, cross-tenant)
  depends on the port, calls the worker, and returns `WorkerStatusOut`. A
  transport failure becomes a clean **503 `service_unavailable`** (new
  `ServiceUnavailable` domain error) rather than a 500 — honest about "a
  dependency I call synchronously is down".

> **Why not commit the generated stubs?** They're build artifacts tied to the
> grpc runtime version; committing them invites drift and merge noise. Generating
> at build time keeps git clean and the stubs in lockstep with `grpcio`.

> **Deferred (Stage 8+):** rewriting the worker in Go behind the *same* proto
> (the contract makes the language swap transparent to the gateway), mutual-TLS
> between services, and a service mesh.

---

## 5J. Stage 8 (part 1) — Kubernetes packaging with Helm (block-by-block)

**Idea:** package the whole platform so it runs on **Kubernetes** — declaratively,
repeatably, and parameterised per environment — using a **Helm chart**. **Motive:**
Docker Compose is a single-host dev tool; production orchestration (self-healing,
horizontal scaling, rolling updates, config/secret management, service discovery)
is Kubernetes. This stage is **pure packaging** — not one line of application code
changes, which is itself the point: the hexagonal app was always deployment-agnostic.

**Why Helm, not raw manifests:** a chart is a *templated, versioned* package.
One `values.yaml` drives dev vs prod (replica counts, resources, whether to run
the dev stores or point at managed services) without copy-pasting YAML. It's the
de-facto standard for shipping k8s apps.

### 5J.1 `Chart.yaml` + `values.yaml`
- `Chart.yaml` separates the **chart version** (bump on template changes) from
  **appVersion** (the image the templates target). `values.yaml` is the single
  surface of knobs: image, per-component replicas/resources, the config/secret
  values, the HPA, and the `backingServices` toggle.

### 5J.2 `templates/_helpers.tpl` — names + labels
- `cosmos.fullname` prefixes every object with the release name (two installs
  can coexist), and `cosmos.selectorLabels` emits the **stable** label set a
  Deployment selects on (excluding version/chart, which change on upgrade — a
  Deployment's selector is immutable, so it must not include churny labels).

### 5J.3 `templates/configmap.yaml` — non-secret config
- Renders the 12-factor env into a ConfigMap. The clever bit: connection URLs
  **default to the in-cluster Service DNS names** (`<release>-postgres:5432`, …)
  computed from the release name, but each is overridable — so the same chart
  targets the bundled dev stores or external managed services with a `--set`.
  `WORKER_GRPC_TARGET` is likewise computed to the worker's Service, wiring
  Stage 7's control plane through k8s DNS.

### 5J.4 `templates/secret.yaml` — sensitive config
- `ADMIN_TOKEN`, the JWT secret, and provider keys go in a Secret (via
  `stringData`, so we write plaintext and k8s encodes it). `secrets.create=false`
  + `existingSecret` lets prod supply an externally-managed Secret (Sealed
  Secrets / External Secrets / cloud store) instead — the honest production path.

### 5J.5 `templates/gateway-*.yaml` — Deployment + Service + HPA
- The Deployment `envFrom` both the ConfigMap and Secret; **liveness** hits
  `/healthz` (is the process alive?) and **readiness** hits `/readyz` (can it
  serve? — checks deps), so a pod that loses its DB is pulled from the Service
  without being killed. **Checksum annotations** on the pod template roll the
  pods when config/secret content changes (otherwise a ConfigMap edit wouldn't
  restart anything). When the **HPA** is enabled the Deployment omits `replicas`
  (so it doesn't fight the autoscaler); the HPA scales on CPU utilisation.

### 5J.6 `templates/worker-*.yaml`
- The worker runs the **same image** with `command: python -m app.worker`. It has
  no HTTP surface, so readiness is a **TCP check on the gRPC port** ("is the
  control plane accepting connections?"). Its Service is what
  `WORKER_GRPC_TARGET` resolves to.

### 5J.7 `templates/{postgres,redis,mongo,redpanda}.yaml` — dev stores
- Single-replica Deployments (+ PVCs for the stateful ones) so `helm install`
  yields a runnable stack on a laptop. All gated by `backingServices.enabled`;
  Postgres/Mongo use `strategy: Recreate` because one RWO PVC can't attach to two
  pods during a rolling update. **In prod you disable these** and point the
  config URLs at real managed services — the gateway/worker templates are
  untouched.

> **Honest scope:** these bundled stores are for dev/demo — single replica, no
> backups, no HA. Production databases belong to StatefulSets with proper storage
> or (better) managed services; that's exactly why they're behind a toggle.

---

## 5K. Stage 8 (part 2) — custom operator + CRD (block-by-block)

**Idea:** extend the Kubernetes API itself with a **Custom Resource** —
`CosmosTenant` — and run a **controller (operator)** that reconciles it: you
`kubectl apply` a tenant, and the operator provisions it on the platform. **Motive:**
this is the **operator pattern**, how platforms make their own concepts
first-class k8s objects (declarative, GitOps-able, self-healing). It also closes a
nice loop — the Stage 6 admin API is now driven by a Kubernetes controller.

**The core principle — level-triggered, idempotent reconciliation.** A controller
doesn't run once; it's called repeatedly for the same object (on create, on
operator restart via *resume*, on every edit, on periodic re-sync). So the logic
must converge the world to the spec **without** repeating side effects — here,
never minting a second tenant. That property is the whole design.

### 5K.1 The CRD (`templates/operator/crd.yaml`)
- Registers `cosmostenants.cosmos.dev`: an OpenAPI v3 schema validating `spec`
  (`displayName` required, `monthlyQuota` optional), a **`status` subresource**
  (so the controller patches status without touching spec, and users can't hand-
  edit it), and **printer columns** so `kubectl get ctenant` shows phase + tenant
  id. Registering the CRD makes `CosmosTenant` a real, `kubectl`-native kind.

### 5K.2 `services/operator/app/reconcile.py` — the pure logic
- Framework-free: `TenantSpec` in, a `ReconcileResult` (status patch + optional
  Secret data) out, talking to an `AdminClient` **port**. The idempotency lives
  here: **if `status.tenantId` is already set, it's a no-op** (re-assert Ready,
  create nothing). This is what makes it safe under repeated reconciliation — and
  it's unit-tested with a fake admin client, no cluster, no kopf.

### 5K.3 `services/operator/app/admin_client.py`
- `HttpAdminClient` calls `POST /v1/admin/tenants` with the admin token (httpx,
  lazy import). A non-2xx raises → the handler turns it into a retry. Behind the
  port so tests never hit the network.

### 5K.4 `services/operator/app/handlers.py` — the kopf glue
- `@kopf.on.create / .resume / .update` all point at one handler, so the resource
  converges however it's triggered (idempotency makes the fan-in safe). The
  handler reads the spec, calls `reconcile()`, and on a fresh provision writes the
  **credentials Secret** — capturing the API key the gateway returns **once** —
  using `kopf.adopt()` so the Secret is **owned by the CR** and garbage-collected
  when the CR is deleted. It returns a dict that kopf merges into `status`. Admin
  failures become `kopf.TemporaryError` (retry with backoff), not a permanent
  failure. `on.delete` logs (the admin API has no delete-tenant endpoint yet).

### 5K.5 RBAC + Deployment (`templates/operator/*.yaml`)
- A ServiceAccount the operator runs as, and a ClusterRole granting exactly what
  it needs: watch/patch `cosmostenants` (+ status), create/patch Secrets, post
  Events, and read the CRD/namespaces (what kopf needs). The Deployment runs
  `kopf run --standalone` (single instance, no peering — so no extra peering
  CRDs/RBAC), wired via env to the gateway Service URL and the shared
  `ADMIN_TOKEN` Secret. All gated by `operator.enabled`.

> **Why the credentials go in a Secret the CR owns:** the gateway reveals the API
> key exactly once, so the operator must persist it immediately — a k8s Secret is
> the natural home, and the owner reference ties its lifecycle to the CR.

> **Honest scope:** deleting a CosmosTenant doesn't deprovision the tenant (no
> admin DELETE endpoint yet) — it only GCs the Secret. Real production would add
> a finalizer + a delete API so teardown is complete.

---

## 5L. Stage 9 — A/B experimentation & feature flags (block-by-block)

**Idea:** ship an **experimentation** capability — split traffic into weighted
variants, decide each unit's variant **deterministically**, and record
**exposures** so results are measurable. **Motive:** every product platform needs
to roll out changes behind flags and A/B-test them; the interesting engineering is
doing the assignment *without shared state* so it's correct across many replicas.

**The core insight — assignment by hashing, not by dice or a database.** A naive
A/B test rolls a random number per request (not sticky — a user flips variants) or
stores each decision (a DB write on every call, and replicas must agree). Instead
we compute the variant as a pure function of `(experiment_key, unit_id, weights)`
via a hash. That's **deterministic** (same inputs → same variant), **sticky** (a
tenant's experience is consistent), **stateless** (no storage, no coordination —
every replica computes the same answer), and **independent** across experiments
(the key is mixed into the hash).

### 5L.1 `app/domain/experiment.py`
- `Experiment` (key, status, weighted `Variant`s, each with a `config` payload of
  what it changes), `ExperimentStatus` (draft/running/paused/completed), and a
  frozen `Assignment`. `control` = the first variant, handed out whenever the
  experiment isn't running. Pure data + rules; no hashing/DB/HTTP.

### 5L.2 `app/experiments/bucketing.py` — the assignment function
- `bucket_of(key, unit, mod)` = `int(sha256("key:unit")[:8], 16) % mod` — a stable
  32-bit hash into the bucket space. `assign(exp, unit)`: not running → control;
  running → hash into `[0, total_weight)` and walk variants by cumulative weight,
  so each gets its configured share. Pure and unit-tested (stable + roughly
  proportional + independent across keys).

### 5L.3 Ports + adapters
- `ExperimentStore` (definitions: add/get/list/set_status) and `ExposureLog`
  (record/counts), each with memory + Postgres adapters. The exposure log is
  **idempotent per `(experiment, unit)`** (Postgres `ON CONFLICT DO NOTHING`; a
  set in memory), so a unit's first-seen variant sticks and `counts()` measures
  **unique units per variant** — the denominator for comparing outcomes.

### 5L.4 `app/application/experiment_service.py`
- The use cases: create/list/activate, `assignment(key, unit, record=True)`
  (resolve via `assign`, and record an exposure **only when the experiment is
  running** — control-by-default isn't a real exposure), and `results(key)`
  (per-variant counts + total). Unknown key → `ExperimentNotFound` (404).

### 5L.5 API — `routes_experiments.py`
- One router, two audiences: **admin** (X-Admin-Token) manages experiments and
  reads `/results`; a **tenant** (API key / JWT with `jobs:read`) resolves
  `/{key}/assignment` where the **unit is the tenant id** — so a tenant gets a
  stable variant and each call records (idempotently) that it was exposed.

> **Why the unit is the tenant here:** it makes a whole tenant's experience
> consistent (every request from that tenant sees the same variant) and lets the
> variant's `config` drive tenant-level behavior. A different granularity (per
> end-user, per job) is just a different `unit_id` into the same function.

> **Deferred:** *consuming* a variant's `config` to actually change behavior
> (e.g. the worker picking the variant's model per job) and a web UI to visualize
> results — both build straight on this assignment + exposure core.

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
| Transactional outbox (Stage 5) | atomic state-change + event; no lost publishes | extra table + a relay to run; delivery is at-least-once |
| Relay publishes-before-marks | never drops an event on a crash | duplicates possible → consumers must dedupe (idempotent) |
| Redpanda/Kafka event backbone | durable, replayable, many independent consumers | another stateful service to operate |
| CQRS: separate read model (Stage 5) | reads/writes optimized independently; rebuildable by replay | eventual consistency; projection code to maintain |
| Projection idempotent via version LWW | correct under at-least-once + reordering | needs the version carried on every event |
| LWW pushed into SQL (`WHERE version < excluded.version`) | concurrent/duplicate projectors can't race | Postgres-specific upsert (`ON CONFLICT`) |
| Projector commits after applying a batch | a crash re-polls, never skips | a batch may be re-applied (safe: idempotent) |
| API-key auth, keys stored **hashed** | a DB leak exposes no usable credentials | key shown once; lost = mint a new one |
| sha256 for keys (not bcrypt/argon2) | keys are high-entropy → fast hash is safe + cheap | would be wrong for human passwords |
| Cross-tenant access → **404, not 403** | never leak that another tenant's id exists | client can't tell "missing" from "forbidden" |
| Tenant isolation scoped in the repository | one choke point; routes just pass `tenant_id` | every query must remember to pass it (worker opts out as trusted) |
| Per-tenant **monthly quota** (usage ledger) | fairness/billing lever, distinct from rate limit | coarse; check+record not atomic → tiny overshoot possible |
| Separate **admin plane** (shared secret) | solves the credential bootstrap chicken-and-egg | a second secret to manage; blank ⇒ disabled |
| API keys now, OIDC/JWT later | ship isolation without an IdP dependency | no SSO/federation yet (slots into `get_principal`) |
| JWT as a 2nd scheme behind one choke point | new credential type touches only `get_principal` | two schemes to reason about; same `Principal` out |
| HS256 + pure stdlib (verify in-house) | hermetic tests; validation fully visible | symmetric only → RS256/JWKS is a later increment |
| Reject `alg:none`/non-HS256 explicitly | blocks JWT alg-confusion forgery | must pin the algorithm, not trust the header |
| Scope-based authZ (`require_scope`) | least privilege; authN vs authZ separated | scopes to define/assign; API keys are full-access for now |
| API keys granted **all** scopes | Stage 0–6 stays backward-compatible | no read-only API keys yet (a later refinement) |
| gRPC for the control plane, queue for the data plane | right comm style per concern; typed status RPC | two transports to operate |
| Typed `.proto` contract (schema-first) | client/server can't drift; polyglot-ready | a codegen build step |
| gRPC client/server behind a port (lazy) | tests need no grpc; adapter swappable | real path only exercised in Docker/e2e |
| Generated stubs built, not committed | git stays clean; stubs match the runtime | fresh local run needs codegen first |
| Worker unreachable → 503, gRPC start best-effort | honest failure; data plane never blocked on control plane | status can be briefly unavailable after boot |
| Helm chart (templated) over raw manifests | one values surface drives dev vs prod; versioned package | Helm templating to learn/maintain |
| Config in ConfigMap, secrets in Secret | 12-factor; secrets managed/rotated separately | Secret still needs a real backend in prod |
| readiness `/readyz` vs liveness `/healthz` | a dep-less pod leaves the Service, isn't killed | must keep readiness honest about deps |
| Checksum annotations roll pods on config change | edits actually take effect | every config change restarts pods |
| HPA owns replicas (Deployment omits them) | autoscaler and chart don't fight | manual scaling needs the HPA off |
| Bundled dev stores behind a toggle | one-command runnable stack | not HA/backed-up → managed services in prod |
| App code unchanged for k8s | proves the app was deployment-agnostic | packaging complexity moves to the chart |
| Operator pattern (CRD + controller) | tenants become declarative, GitOps-able k8s objects | an operator to run; k8s API extended |
| Reconcile keyed on status.tenantId (idempotent) | safe under repeated/level-triggered reconciliation | relies on status being persisted correctly |
| Pure reconcile logic, kopf as thin glue | unit-testable with no cluster/kopf | handler still needs live e2e for k8s I/O |
| Credentials → CR-owned Secret | one-time API key persisted; GC'd with the CR | secret sits in etcd (encrypt at rest in prod) |
| `kopf run --standalone` | no peering CRDs/RBAC; simple single instance | no multi-replica HA for the operator |
| No deprovision on delete (yet) | ships the create path now | delete leaves the tenant → needs a finalizer + admin DELETE |
| A/B assignment by hashing (not RNG/DB) | deterministic, sticky, stateless, replica-consistent | changing weights reshuffles buckets |
| Exposure idempotent per (experiment, unit) | results = unique units, no double-count | re-exposure not counted (by design) |
| Unit = tenant id | whole-tenant consistent experience | finer splits (per-user/job) need a different unit |
| Not-running → everyone gets control | clean on/off; safe default | no partial ramp state (draft/paused only) |
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

**Q: What's the dual-write problem, and how does the outbox solve it? (Stage 5)**
Writing to the DB and publishing to the bus are two systems with no shared
transaction — a crash between them loses the event or the write, and they
diverge. The outbox turns the publish into a *DB write*: I insert an `outbox`
row in the same transaction as the event, so they commit atomically. A separate
relay then reads the outbox and publishes. Correctness (the commit) is decoupled
from delivery (retried, best-effort).

**Q: Doesn't the relay then send duplicates?**
It can — it publishes a row, then marks it published, so a crash in between
re-publishes on restart. That's a deliberate **at-least-once** choice (never drop
an event). Consumers stay correct by being idempotent — deduping on the event/
outbox id, or applying updates that are naturally idempotent (upserts).

**Q: Why Kafka/Redpanda instead of just reusing Redis?**
The backbone needs a durable, replayable log that many independent consumers
(CQRS projector, CDC, analytics) can each read at their own offset and replay
from history. That's exactly Kafka's model; Redpanda is a drop-in
Kafka-compatible broker that's lighter to run locally. Redis Streams is fine for
the work queue but isn't the multi-consumer event log this needs.

**Q: What is CQRS, and why a separate read model? (Stage 5)**
CQRS = Command Query Responsibility Segregation: the write path and the read path
use different models. My write model (jobs + append-only job_events) is tuned for
correctness — a state machine, optimistic concurrency, an audit log. That shape is
bad for queries like "counts per status" or "one job's status, fast." So a
**projector** consumes the event backbone and folds events into a denormalized
**read model** the API serves. Each side scales and evolves independently, and the
read model can be **rebuilt from scratch** by replaying the log — I can change its
shape without touching the write side.

**Q: The projection is fed at-least-once and events can reorder — how is it
correct?**
Every event carries the job's `version`, and the read model applies
**last-writer-wins by version**: an event whose version is ≤ the stored one is a
no-op. So a duplicate delivery and a reordered older event both change nothing,
and the view converges to the true state regardless of delivery order or count.
In Postgres that rule is one atomic upsert —
`ON CONFLICT … DO UPDATE … WHERE job_read_model.version < excluded.version` —
so even concurrent projectors can't race it.

**Q: What's the consistency model, then?**
The write side is immediately consistent (`GET /v1/jobs/{id}` reads it directly).
The read model is **eventually consistent** — after a commit it takes a relay hop
plus a projector hop to update, so `/v1/jobs/{id}/view` may 404 for a moment right
after submit. That's the deliberate CQRS trade-off: you accept a small lag on the
query side in exchange for independently optimized, replayable reads.

**Q: How does the projector track its position, and what happens on a crash?**
It's a Kafka-style consumer group (`cqrs-projector`) with its own committed
offset. It applies a whole polled batch, *then* commits. Crash mid-batch → no
commit → the next poll redelivers that batch → the idempotent `apply` makes the
re-processing harmless. It never skips (commit-after-apply) and never corrupts
(version LWW).

**Q: How do you authenticate requests, and how are the keys stored? (Stage 6)**
Clients send an API key as `Authorization: Bearer csk_<id>_<secret>`. I store
only a **sha256 hash** of the secret half, plus a non-secret `key_id` I index by —
so auth is a single indexed lookup, and a database leak exposes hashes, not usable
keys. On each request I hash the presented secret and compare in **constant time**
(`hmac.compare_digest`). The key is shown to the user exactly once at creation.

**Q: Why sha256 and not bcrypt/argon2?**
Those slow KDFs exist to make *low-entropy human passwords* expensive to
brute-force. An API key's secret is 24+ random bytes — already infeasible to
guess — so a fast hash is the correct, standard choice for tokens. If I were
storing user passwords, I'd use argon2id instead.

**Q: How do you enforce tenant isolation — what stops tenant A reading tenant B's
job?**
Every job carries a `tenant_id`, and the repository scopes reads by it: `get(id,
tenant_id)` returns the row only if it belongs to that tenant, otherwise `None`.
The route turns `None` into a **404** — deliberately *not* a 403 — so I never even
leak that another tenant's id exists. Lists, the event log, cancel, and the CQRS
`/stats` and `/view` are all scoped the same way. The check lives in one place
(the repository), so routes can't forget it.

**Q: Why 404 rather than 403 for another tenant's resource?**
A 403 confirms "this id exists, you just can't have it" — an information leak
(enumeration). A 404 is indistinguishable from a genuinely missing id, so an
attacker probing ids learns nothing. Same reason auth returns one generic 401 for
unknown-key vs wrong-secret.

**Q: You have a Stage-4 rate limiter AND a Stage-6 quota — aren't those the same?**
No — different axes. The rate limiter smooths **bursts** (requests/second, global
across replicas via Redis) to protect the *system* from overload; it's stateless
about who you are. The quota bounds **volume over time** (submissions/month, per
tenant, via the usage ledger) — a *fairness/billing* lever. A tenant can be well
under the per-second limit yet out of monthly quota, or vice-versa; a submit must
pass both.

**Q: How is the usage count kept correct under concurrency?**
The Postgres ledger increments inside the database:
`INSERT … ON CONFLICT (tenant_id, period) DO UPDATE SET used = used + 1 RETURNING used`.
That serialises concurrent submits for a tenant, so no count is lost. The
quota *check* and the *record* aren't one transaction, so a burst could overshoot
the limit by a hair — an accepted trade-off for a coarse monthly quota; the counts
themselves are never wrong.

**Q: Where do the first credentials come from — isn't there a bootstrap problem?**
Yes: you need a key to call the platform, but no key exists yet. So there's a
separate **admin plane** — `POST /v1/admin/tenants` — authenticated by a shared
`ADMIN_TOKEN` (env), not by an API key. It creates a tenant and mints its first
key. If `ADMIN_TOKEN` is unset the admin API is **disabled** (403), never open —
a missing secret must not mean "no auth".

**Q: You support both API keys and OAuth2/OIDC JWTs — how, without duplicating
auth everywhere? (Stage 7)**
Because Stage 6 put authentication behind a single choke point, `get_principal`.
Adding JWTs touched only that function: it looks at the `Authorization: Bearer`
value, dispatches on the `csk_` prefix (API key) vs otherwise (JWT), and both
paths return the **same `Principal`**. Every route, the service, and the isolation
logic are unchanged — that's the payoff of the ports/choke-point design.

**Q: What exactly do you validate on a JWT?**
Four things: (1) the **signature** — recompute HMAC-SHA256 over `header.payload`
and constant-time compare, proving it wasn't forged or tampered; (2) **`exp`/
`nbf`** with a small clock-skew leeway; (3) **`iss`/`aud`** so a token minted for
a different service/issuer can't be replayed against us; (4) the **algorithm** —
I pin HS256 and explicitly reject `alg:none` and other algs, which defeats the
classic JWT alg-confusion attack. The tenant comes from a configured claim, and I
still confirm that tenant is active (so suspension applies to SSO users too).

**Q: Why HS256 here when real OIDC uses RS256?**
HS256 is symmetric HMAC — no crypto dependency — so the learning build stays
hermetic and the validation is fully visible in ~40 lines. Production uses
**RS256**: the IdP signs with a private key and I verify with its public key
fetched from a **JWKS** endpoint (selected by the token's `kid`). That slots in
behind the same `decode_and_verify` shape and will use a vetted library — you can
hand-roll an HMAC compare, but you must never hand-roll RSA.

**Q: Authentication vs authorization — where's the line in your code?**
`get_principal` is **authentication** (who are you? → 401 if we can't tell).
`require_scope(...)` is **authorization** (may you do this? → 403 if your scopes
don't include it). They're separate dependencies so the failure modes and status
codes stay distinct. Submit/cancel require `jobs:write`, reads require
`jobs:read`; API keys carry all scopes, JWTs carry whatever the `scope` claim
granted — least privilege for tokens.

**Q: The gateway and worker share a queue — why add gRPC too? (Stage 7 pt2)**
Different communication styles for different concerns. The **data plane**
(dispatching jobs) is asynchronous over the Redis queue — that's what gives
accept-fast and decoupling; making it a synchronous call would reintroduce
coupling and back-pressure. The **control plane** (health, in-flight count) is a
request/response *question*, which is exactly what synchronous **gRPC** is for. So
jobs flow over the queue; `GET /v1/admin/worker/status` calls the worker over gRPC.

**Q: Why gRPC/protobuf and not just another REST endpoint on the worker?**
A `.proto` is a single, **versioned, typed contract** that generates matching
client and server code for any language — so the two services can't drift, and I
could rewrite the worker in Go behind the *same* proto without the gateway
noticing. gRPC also gives HTTP/2 multiplexing and streaming for free. For a
strongly-typed internal service-to-service call, that beats hand-rolled JSON.

**Q: gRPC needs codegen and a C-extension — how do your tests still run without
it?**
The same ports pattern as every external dependency. The gateway depends on a
`WorkerControlClient` **port**; tests inject an in-memory fake, so `pytest` never
imports `grpc`. The real adapter imports gRPC and the generated stubs **lazily**,
and the stubs are generated at **Docker build time** (gitignored, not committed).
`grpcio` is only needed to *run* the real path, never to *test*.

**Q: What happens if the worker is down when the gateway asks for status?**
The gRPC call fails and the route maps it to a clean **503 `service_unavailable`**
(not a 500). And on the worker side, starting the gRPC server is best-effort: if
it can't start, the worker logs it and keeps processing jobs — the data plane
never depends on the control plane.

**Q: How do you deploy this to Kubernetes? (Stage 8)**
A Helm chart (`deploy/helm/cosmos`). The gateway and worker are Deployments of
the same image with different commands; a Service fronts each; the gateway has an
HPA. Config splits into a ConfigMap (non-secret) and a Secret, both consumed via
`envFrom`. Dev backing stores ship as toggleable single-replica Deployments. No
application code changed — the hexagonal app was already deployment-agnostic;
Kubernetes is just how it's run.

**Q: Liveness vs readiness — why two probes?**
Liveness (`/healthz`) answers "is the process alive?"; if it fails, k8s
**restarts** the pod. Readiness (`/readyz`) answers "can it serve requests right
now?" — it checks dependencies; if it fails, k8s **removes the pod from the
Service** but leaves it running. So a transient DB blip takes a pod out of
rotation instead of triggering a pointless restart loop. The worker has no HTTP,
so its readiness is a TCP check on the gRPC port.

**Q: How does one chart serve both dev and prod?**
`values.yaml`. `backingServices.enabled=true` runs bundled Postgres/Redis/Mongo/
Redpanda for a laptop; set it `false` and point `config.*Url` at managed services
for prod — the gateway/worker templates don't change. Replica counts, resources,
the HPA, and `secrets.create=false`+`existingSecret` are all values too. That's
the whole reason to template with Helm rather than hand-maintain manifests.

**Q: Why the checksum annotations on the pod template?**
Editing a ConfigMap or Secret doesn't restart anything on its own — pods read env
at start. Stamping a `sha256sum` of the rendered config/secret into the pod
template annotations changes the template on every content change, so a
`helm upgrade` performs a rolling restart and the new config actually takes effect.

**Q: What's the operator pattern, and what did you build with it? (Stage 8 pt2)**
An operator extends Kubernetes with a **Custom Resource** (via a CRD) plus a
**controller** that watches it and reconciles the real world to the resource's
spec. I defined a `CosmosTenant` CRD and a `kopf` controller: `kubectl apply` a
CosmosTenant and the operator calls the gateway admin API to provision the tenant,
stores the returned API key in a k8s Secret, and records the tenant id in the
CR's status. Tenants become declarative, `kubectl`-native, GitOps-able objects.

**Q: Controllers get called repeatedly — how do you avoid provisioning twice?**
Level-triggered, idempotent reconciliation. The handler fires on create, on
operator restart (*resume*), on every edit, and on re-syncs — so it must converge
without repeating side effects. I key on the CR's `status.tenantId`: if it's
already set, reconcile is a no-op. That's the single most important property of a
correct operator, and it's the thing my unit tests pin down.

**Q: The API key is shown only once — how does the operator not lose it?**
On a fresh provision the handler immediately writes the gateway's one-time key
into a Kubernetes Secret named `<cr>-credentials`, and uses `kopf.adopt()` to set
the CosmosTenant as the Secret's owner — so the Secret is garbage-collected when
the CR is deleted, and its lifecycle tracks the tenant's.

**Q: How is the operator testable without a cluster?**
The reconcile decision is a pure function (`reconcile.py`) over a `TenantSpec`,
the current status, and an `AdminClient` port — no kopf, no kubernetes client. The
kopf handlers are thin glue that do the k8s I/O around it. So `pytest` exercises
the create/idempotent/quota paths with a fake admin client and no cluster; the
handler's k8s calls are validated live on minikube.

**Q: How does your A/B assignment work, and why hashing? (Stage 9)**
Each unit's variant is a pure function of `(experiment_key, unit_id, weights)`:
hash "key:unit" to a 32-bit int, mod into `[0, total_weight)`, and walk the
variants by cumulative weight. No random rolls, no stored decision. That gives me
four properties for free: **deterministic** (reproducible), **sticky** (a tenant
always sees the same variant), **stateless** (every gateway replica computes the
same answer with zero coordination or DB lookups), and **independent** across
experiments (the key is in the hash, so buckets don't correlate). It's the same
idea as consistent-hashing a request to a variant.

**Q: How do you measure an experiment?**
An exposure log records `(experiment, unit) → variant`, idempotently — so a unit's
first-seen variant sticks and `results` counts **unique units per variant**.
That's the denominator; you'd join it against whatever outcome metric you care
about (conversion, latency, success rate) to compare arms. Assignment records an
exposure only while the experiment is actually running.

**Q: What happens when you pause or haven't started an experiment?**
Draft, paused, and completed all hand out **control** (the first variant) and
record no new exposures. So flipping an experiment off cleanly reverts everyone to
baseline — a safe kill switch — without deleting the definition or its results.

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
- **Transactional outbox + Kafka/Redpanda backbone** (for the event stream) → **Stage 5 part 1**. ✅ done
- **CQRS read model** (projector + `/v1/stats` + `/v1/jobs/{id}/view`, version-LWW idempotent) → **Stage 5 part 2**. ✅ done
- **Saga, CDC, schema registry** → **Stage 5 parts 3+** (optional later increments).
- **Outbox for job *dispatch*** (replace the direct Redis enqueue) → same pattern, a small follow-on.
- **Read model is eventually consistent** — `/view` can briefly 404 after submit; a client wanting strong reads uses the write-side `GET /v1/jobs/{id}`.
- **Multi-tenancy: API-key auth, tenant isolation, per-tenant quota + usage ledger, admin bootstrap** → **Stage 6**. ✅ done
- **OAuth2/OIDC (JWT) as a second principal source** + **scope-based authorization** → **Stage 7 part 1**. ✅ done
- **RS256 + JWKS** (verify tokens from a real OIDC provider, not just HS256) → Stage 7 later increment (slots into `decode_and_verify`).
- **Read-only / scoped API keys** and **key rotation/expiry** → later (API keys are full-access today).
- **Typed gRPC control-plane contract** (gateway↔worker status/health) → **Stage 7 part 2**. ✅ done
- **Rewrite the worker in Go** behind the same proto, **mutual-TLS**, **service mesh** → later.
- **Kubernetes packaging** (Helm chart: gateway+worker+stores, ConfigMap/Secret, probes, HPA) → **Stage 8 part 1**. ✅ done
- **Custom operator + CRD** (`CosmosTenant` reconciled to provision tenants/keys) → **Stage 8 part 2**. ✅ done
- **Operator teardown** (finalizer + an admin DELETE-tenant endpoint so deleting a CR deprovisions) → follow-on.
- **Production storage** (StatefulSets/managed DBs, backups, HA), **Ingress/TLS**, **NetworkPolicies**, **PodDisruptionBudgets** → hardening increments.
- **A/B experimentation & feature flags** (deterministic sticky assignment, exposure tracking, admin experiments) → **Stage 9**. ✅ done
- **Consuming a variant's config** to change behavior (e.g. worker picks the variant's model per job) + a **web UI** to visualize results → follow-ons on Stage 9.
- **Observability** (structured logs, Prometheus `/metrics`, distributed tracing) → **Stage 10**.
- **Per-tenant rate limits** (reuse the Stage-4 bucket keyed by tenant) → small follow-on.
- **Quota check + usage record aren't atomic** (tiny overshoot under bursts) → acceptable for a coarse monthly quota; a stricter limit would reserve-then-commit.

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
- **CQRS** — Command Query Responsibility Segregation: separate write and read models.
- **Read model / projection** — a denormalized view built by folding events, tuned for queries.
- **Projector** — the consumer that folds events into the read model.
- **Last-writer-wins (LWW)** — resolve conflicts by keeping the highest version; makes projection idempotent.
- **Eventual consistency** — the read model catches up to the write side after a short delay.
- **Consumer group / offset** — a Kafka consumer's independent, committed position in the log.
- **Multi-tenancy** — many isolated customers (tenants) on one shared deployment.
- **Tenant** — the unit of isolation, billing, and quota; every job belongs to one.
- **Principal** — the authenticated identity of a request (here: tenant + key id).
- **API key** — a bearer credential; stored hashed, presented as `csk_<id>_<secret>`.
- **Authentication vs authorization** — *who you are* (the key → principal) vs *what you may touch* (tenant-scoped queries).
- **Quota** — a per-tenant volume allowance over a period (here monthly submissions), tracked by the usage ledger.
- **Admin plane** — the separate, shared-secret-guarded surface that mints tenants/keys.
- **JWT** — a signed, self-describing bearer token: `header.payload.signature` (base64url).
- **Claims** — the JSON fields in a JWT payload (`iss`, `aud`, `exp`, `sub`, + our `tenant_id`/`scope`).
- **OAuth2 / OIDC** — the bearer-token authZ framework / the identity layer on top of it (SSO).
- **JWKS** — the JSON Web Key Set an OIDC provider publishes so verifiers can fetch its public keys (RS256).
- **Scope** — a permission string a credential carries (`jobs:read`); the unit of authorization.
- **alg confusion** — a JWT attack that swaps the signing algorithm (e.g. to `none`); defeated by pinning the expected alg.
- **gRPC** — a high-performance RPC framework over HTTP/2 with typed contracts.
- **protobuf / `.proto`** — the schema language defining messages + services; compiled by `protoc` into per-language stubs.
- **codegen / stub** — generated client/server code from the `.proto` (the `*_pb2*.py` files).
- **data plane vs control plane** — the path that carries the work (async queue) vs the path that manages/observes it (sync gRPC).
- **servicer / stub** — the server-side implementation of a gRPC service vs the client-side proxy that calls it.
- **Helm / chart / values** — the k8s package manager; a templated bundle of manifests; the file of overridable knobs driving them.
- **ConfigMap / Secret** — k8s objects holding non-secret / secret configuration, injected into pods as env or files.
- **liveness / readiness probe** — "restart me if I'm dead" vs "take me out of the Service until I can serve".
- **HPA (HorizontalPodAutoscaler)** — scales replica count on a metric (here gateway CPU).
- **PVC (PersistentVolumeClaim)** — a pod's request for durable storage (used by the stateful dev stores).
- **operator / CRD** — a custom controller that reconciles a Custom Resource toward desired state (Stage 8 pt2).
- **Custom Resource (CR) / CustomResourceDefinition (CRD)** — a user-defined k8s object kind / the schema that registers it.
- **reconciliation (level-triggered)** — repeatedly driving the world to match a resource's spec; must be idempotent.
- **status subresource** — the part of a CR a controller writes (separate from spec).
- **kopf** — a Python framework for writing k8s operators as decorated handler functions.
- **ownerReference / GC** — links a child object to its owner so k8s deletes it when the owner goes away.
- **finalizer** — a marker that blocks deletion until a controller runs teardown (a follow-on here).
- **A/B test / experiment** — splitting units into variants to compare outcomes.
- **variant / control** — an arm of an experiment / the baseline arm (first variant).
- **sticky / deterministic assignment** — the same unit always gets the same variant, computed (here) by hashing rather than stored.
- **exposure** — a recorded event that a unit was actually bucketed into a variant (the measurement denominator).
- **feature flag** — a toggle that turns behavior on/off (a running/paused single-variant experiment is the degenerate case).
