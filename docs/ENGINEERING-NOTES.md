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

**Q: What are the known weaknesses?**
(1) Enqueue is outside the DB transaction → a crash window where a job is
committed but not queued; fixed by the transactional outbox in Stage 5.
(2) `create_all()` isn't migration-safe → Alembic later. (3) The worker shares the
gateway's package → split into its own service in Stage 7. Naming these
unprompted signals seniority.

---

## 8. Known gaps and what later stages fix
- **Transactional outbox** (enqueue-in-transaction) → **Stage 5**.
- **Alembic migrations** (vs `create_all`) → early Stage 2/3 housekeeping.
- **Real work** (Claude agent loop + RAG) replacing the placeholder → **Stage 3**.
- **Retries/backoff/circuit breaker** around failures → **Stage 4**.
- **MongoDB trajectories + Redis hot state** (polyglot) → **Stage 2**.
- **Kafka backbone, CQRS read models, CDC** → **Stage 5**.

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
