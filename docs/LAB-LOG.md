# Cosmos — Lab Log (every command we ran, in order, and why)

> **What this is.** A chronological record of the terminal work behind Stages
> 0–2: the concept behind each command, what it created or checked, and the
> gotchas we hit. It complements the other two docs:
> - [`ENGINEERING-NOTES.md`](ENGINEERING-NOTES.md) — the *code* (block-by-block + interview Q&A)
> - [`STAGES.md`](STAGES.md) — per-stage *test/troubleshoot* runbook
> - **this file** — the *commands* we ran and what they proved.

## 0. The one rule that bit us the most: which directory?

Two tools, two directories — get this right and most errors disappear:

| Tool | Run it from | Why |
|---|---|---|
| **git** | the **repo root** → `~/Desktop/cosmos-platform` | paths like `services/gateway/...` are relative to the root |
| **pytest** | the **service dir** → `~/Desktop/cosmos-platform/services/gateway` | that's where `app/`, `tests/`, `pytest.ini`, `.venv` live |
| **docker compose** | the **service dir** (where `docker-compose.yaml` is) | compose looks for the file in the current directory |

`pwd` tells you where you are. `cd "$(git rev-parse --show-toplevel)"` jumps to the repo root from anywhere inside it.

---

## 1. Python environment & running the tests

**Concept:** a *virtual environment* isolates this project's Python packages from the system. We install deps into it and run the test suite, which uses in-memory adapters (no databases needed).

```bash
cd ~/Desktop/cosmos-platform/services/gateway
python3.12 -m venv .venv          # create the venv (use 3.12 — see gotcha below)
source .venv/bin/activate         # activate it (prompt shows (.venv))
pip install -r requirements.txt   # install pinned deps
pytest -q                         # run all tests (fast, no DB)  -> "17 passed"
pytest -v                         # same, but lists each test name
```

**What this checks:** the whole app + worker logic — domain rules, state machine, idempotency, optimistic concurrency, the event log, at-least-once/idempotent worker, the Mongo document flow, and the cache — all against in-memory adapters.

> ⚠️ **Gotcha we hit — Python 3.14.** On Python 3.14, `pip install -r requirements.txt` fails building `pydantic-core`/`asyncpg` (no prebuilt wheels yet; needs a Rust/C build). Fix: use **Python 3.12** for the venv (we have it at `/opt/homebrew/bin/python3.12`) or just run everything in **Docker** (its image is 3.12). The pins target 3.12 on purpose, for reproducible builds.

---

## 2. Git — from empty repo to a clean stage-by-stage history

**Concept:** git records snapshots (*commits*). We commit in small, labelled sections so the history tells the story of the build. We use **Conventional Commits** (`feat:`, `test:`, `chore:`, `docs:`, `refactor:`).

### One-time setup
```bash
cd ~/Desktop/cosmos-platform          # REPO ROOT
git init                              # start a git repo
git branch -M main                    # name the branch "main"
git remote add origin https://github.com/akhil27051999/cosmos.git   # link GitHub
git status --short                    # see what's staged/untracked (no .venv — it's gitignored)
```

### The commit loop (repeat per section)
```bash
git add services/gateway/app/domain/  # stage a file or folder
git status --short                    # confirm what's staged
git commit -m "feat(gateway): add Job domain entity, state machine, and errors"
```

### Pushing to GitHub
```bash
git fetch origin                      # learn what's on the remote first
git push -u origin main               # first push sets the upstream
git push                              # subsequent pushes
```

**What we created:** a GitHub repo with the project’s history.

> ⚠️ **Gotchas we hit:**
> - *"pathspec did not match"* → we ran `git add services/gateway/...` from **inside** `services/gateway`, doubling the path. Fix: run git from the **repo root**.
> - *push "fetch first"/rejected* → the GitHub repo already had a starter README. Fix: `git fetch origin` then `git push --force-with-lease` (safe overwrite of a fresh repo), or `git pull --rebase origin main` to keep it.

### The clean history rebuild (a one-time repair)
We had accidentally mixed Stage 0 and Stage 1 into one set of commits, then rebuilt it into a clean Stage-0-then-Stage-1 history. **Concept:** you can rewrite local history safely if you keep a backup branch.
```bash
git branch backup-mixed               # SAFETY NET: keep the old commits on a branch
git update-ref -d refs/heads/main     # empty out main (commits still on backup-mixed)
git reset                             # clear the staging area
# ...rebuild Stage 0 files, commit them; then restore Stage 1 files and commit...
git checkout backup-mixed -- services/gateway   # pull files back from the backup
git log --oneline                     # verify the clean, ordered history
git branch -D backup-mixed            # delete the backup once happy
```
> This was a repair, not daily workflow. Normal work is just `add → commit → push`.

---

## 3. Docker — running the full stack (Postgres + Redis + MongoDB + gateway + worker)

**Concept:** `docker compose` starts every service defined in `docker-compose.yaml` as containers, wired together on a private network. The gateway and worker are the **same image, two commands**.

```bash
open -a Docker                        # start the Docker daemon (Docker Desktop)
while ! docker info >/dev/null 2>&1; do sleep 2; done; echo "Docker is up"   # wait for it

cd ~/Desktop/cosmos-platform/services/gateway   # where docker-compose.yaml lives
docker compose up --build             # build images + start all 5 services (foreground)
docker compose up -d                  # ...or detached (frees your terminal)
docker compose logs -f worker         # follow the worker's logs
docker compose ps                     # list running services + health
docker compose down                   # stop everything (add -v to also wipe data volumes)
```

**What we created/checked:** 5 running containers, all healthy; gateway on `http://localhost:8000`; worker "waiting for jobs".

> ⚠️ **Gotchas we hit:**
> - *"cannot connect to the Docker API"* → the daemon wasn't running. Fix: `open -a Docker`, wait, retry.
> - *"no configuration file provided: not found"* → ran `docker compose` from `~`. Fix: `cd` to the service dir, or use `docker exec <container> ...` (works anywhere).
> - The flood of `mongo-1` connection logs is just the healthcheck pinging every 5s — harmless.
> - **A code change didn't take effect** → `docker compose up -d` reuses the old image. **Code needs `docker compose up -d --build`; env-only changes need just `up -d`.** (Rule of thumb: after editing `app/…`, always `--build`.)
> - **`curl` returned `000` right after `up -d`** → the app wasn't ready yet. Wait first: `until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done`.
> - **An `export` didn't reach the container** → `VAR=x` on its own line isn't exported; use `export VAR=x` so compose's `${VAR}` picks it up. Verify with `docker compose exec <svc> env | grep VAR`.

---

## 4. Exercising the API (curl) — submit a job and follow it

**Concept:** the platform is *accept-fast* — `POST` returns `202 + id` immediately; the worker processes the job asynchronously; you poll to see progress.

```bash
# submit a job (ONE line — a backslash-continued paste can break and send an empty body)
curl -s -X POST localhost:8000/v1/jobs -H 'content-type: application/json' -d '{"payload":{"task":"demo","n":21}}'

# capture the id into a shell variable, then reuse it
ID=$(curl -s -X POST localhost:8000/v1/jobs -H 'content-type: application/json' -d '{"payload":{"task":"demo","n":21}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo "job = $ID"

curl -s localhost:8000/v1/jobs/$ID | python3 -m json.tool            # metadata (status/version)
curl -s localhost:8000/v1/jobs/$ID/document | python3 -m json.tool   # payload + result (from Mongo)
curl -s localhost:8000/v1/jobs/$ID/events | python3 -m json.tool     # the event log
curl -s localhost:8000/healthz ; echo                                # liveness
curl -s localhost:8000/readyz  ; echo                                # readiness (checks the DB)
```
Or open the interactive docs (no quoting headaches): **http://localhost:8000/docs**

**What we checked (and saw work):**
- submit → `202`, `status: pending`, `version: 1`
- after ~1s → `status: succeeded`, `version: 3` (pending→running→succeeded)
- document → `payload` immediately, `result: {"echo":...,"processed":true}` after the worker ran
- events → `submitted → started → succeeded`, in order

> ⚠️ **Gotchas we hit:**
> - *`validation_error: JSON decode error`* → the multi-line curl paste sent an empty body. Fix: run curl on **one line**. (Bonus: this proved our Pydantic validation + error envelope work — an empty body is correctly rejected with a `422`.)

---

## 5. Inspecting the raw stores (prove the polyglot split)

**Concept:** each database holds what it's best at. We look directly into each one.

```bash
cd ~/Desktop/cosmos-platform/services/gateway   # so `docker compose exec` finds the file

# Postgres = transactional METADATA (note: NO payload column)
docker compose exec postgres psql -U cosmos -d cosmos -c "\d jobs"
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT id,status,version FROM jobs;"
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT type FROM job_events ORDER BY seq;"

# MongoDB = the big DOCUMENT (payload + result)
docker compose exec mongo mongosh cosmos --quiet --eval 'db.job_documents.find().pretty()'

# Redis = the queue stream + the hot-read cache
docker compose exec redis redis-cli XINFO STREAM cosmos:jobs
docker compose exec redis redis-cli --scan --pattern 'job:*'   # cached jobs (after a GET)
```
(From any directory, swap `docker compose exec <svc>` for `docker exec gateway-<svc>-1`.)

**What we saw:** the `jobs` table has `id, status, idempotency_key, version, created_at, updated_at` — **no `payload`** — plus a status index and the unique idempotency constraint; the same job's `payload` + `result` live in Mongo keyed by the job id.

---

## 5b. Stage 3 — running the agent (agent, SSE, provider setup)

**Concept:** the worker now runs a real LLM agent loop. It needs a model
provider. The LLM is behind a port, so you pick the provider with an env var —
Anthropic (paid) or **Groq (free, no card)** — without any code change.

**Choose a provider (env vars the worker reads):**
```bash
# Option 1 — Groq (free). Get a key at console.groq.com -> API Keys.
export LLM_PROVIDER=groq
export GROQ_API_KEY=gsk_...
export GROQ_MODEL=openai/gpt-oss-20b        # must be a tool-capable model on YOUR account

# Option 2 — Anthropic (paid). Key at console.anthropic.com.
# export LLM_PROVIDER=anthropic
# export ANTHROPIC_API_KEY=sk-ant-...
# export AGENT_MODEL=claude-haiku-4-5
```

**List the models your Groq key can actually use** (the catalog changes; a wrong
id returns 404):
```bash
curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY" \
  | python3 -c 'import sys,json;[print(m["id"]) for m in json.load(sys.stdin)["data"]]'
# pick a chat/tool model (e.g. openai/gpt-oss-20b, qwen/qwen3.8-27b);
# skip whisper-* (speech), orpheus-* (TTS), *guard*/*safeguard* (safety), groq/compound* (built-in tools)
```

**(Re)start the worker with the provider env, then run a task:**
```bash
cd ~/Desktop/cosmos-platform/services/gateway
docker compose up -d                        # recreates the worker with the new env

# submit a task (the agent reads payload.prompt or payload.task)
JOB=$(curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' \
  -d '{"payload":{"prompt":"What is 4839 * 271? Use the calculator tool, then state the result."}}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo "job=$JOB"
sleep 4
curl -s localhost:8000/v1/jobs/$JOB | python3 -m json.tool           # status -> succeeded
curl -s localhost:8000/v1/jobs/$JOB/document | python3 -m json.tool  # result.answer + trajectory + usage
```

**Watch the agent think live (SSE).** It's live-only, so submit **and stream in
one shot**; a multi-step prompt keeps it running long enough to catch the frames:
```bash
JOB=$(curl -s -XPOST localhost:8000/v1/jobs -H 'content-type: application/json' \
  -d '{"payload":{"prompt":"Use the calculator for 12*12, then 144+56, then 200*3, and report all three."}}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'); curl -N localhost:8000/v1/jobs/$JOB/stream
# streams: tool_call -> tool_result -> assistant -> final -> end   (Ctrl+C to stop)
```

**What to verify (learning checkpoints):**
- No-tool prompt -> `trajectory: [assistant, final]`. Math prompt -> `tool_call` -> `tool_result` -> `final` (the calculator ran).
- `result.usage` reports tokens; `stopped: "done"` on success.
- SSE frames arrive in real time, ending with `end`.

**Gotchas we hit (and fixes):**
| Symptom | Cause & fix |
|---|---|
| Job `failed`, event log shows `model ... does not exist or you do not have access` (404) | The `GROQ_MODEL` (or `AGENT_MODEL`) id isn't available to your key. List models (above) and pick a valid tool-capable one; `docker compose up -d` to reload. |
| `curl -N .../stream` hangs with no output | The job already finished — SSE is live-only. Ctrl+C. Submit-then-stream in one command (above), or read the stored trajectory from `/document`. |
| Job `failed` with an auth error | Wrong/missing provider key, or `LLM_PROVIDER` not set. Re-export and `docker compose up -d`. |

> 🔐 Never paste an API key into a terminal command you share, or into chat. If
> you do, treat it as compromised and rotate it (delete + recreate in the
> provider console). Keys can live in a gitignored `services/gateway/.env`.

---

## 6. Daily workflow cheat-sheet (once set up)

```bash
# work on code, then test:
cd ~/Desktop/cosmos-platform/services/gateway && source .venv/bin/activate && pytest -q

# commit + push (from the repo root):
cd ~/Desktop/cosmos-platform
git add <paths> && git commit -m "feat(...): ..." && git push

# run it for real:
cd ~/Desktop/cosmos-platform/services/gateway && docker compose up -d && docker compose logs -f worker
# ... exercise with curl / http://localhost:8000/docs ...
docker compose down
```

## 7. What exists now (created across Stages 0–8 pt2)
- **Services:** `gateway` (FastAPI API) + `worker` (agent + outbox relay + CQRS projector + gRPC control-plane server) + `operator` (a `kopf` controller reconciling `CosmosTenant` CRs).
- **Stores:** Postgres (jobs + job_events + outbox + job_read_model + **tenants + api_keys + usage_ledger**), MongoDB (job_documents + trajectory), Redis (job queue + cache + SSE pub/sub + rate-limit bucket), Redpanda (event backbone: `job-events` topic).
- **Event backbone (Stage 5 pt1):** every event is staged in an `outbox` table in the same transaction, and a relay drains it to Redpanda at-least-once (transactional outbox).
- **CQRS read model (Stage 5 pt2):** a projector (`cqrs-projector` consumer group) folds `job-events` into a denormalized, **tenant-scoped** read model, idempotent under at-least-once via version last-writer-wins. Served by `/v1/stats` and `/v1/jobs/{id}/view`.
- **Multi-tenancy & identity (Stage 6):** every request authenticates with an **API key** (`Authorization: Bearer csk_...`; only the key's hash is stored); every read/write is **scoped to the caller's tenant** (cross-tenant id → 404); a per-tenant **monthly quota** + usage ledger governs submissions (429 `quota_exceeded`); an **admin API** (guarded by `ADMIN_TOKEN`) mints tenants/keys.
- **OAuth2/OIDC + scopes (Stage 7 pt1):** a caller may instead present an **OIDC JWT** on the same Bearer header (HS256 verified: signature + `exp`/`nbf`/`iss`/`aud`, `alg:none` refused), resolved to the same tenant `Principal`; **scope-based authorization** (`jobs:read`/`jobs:write`) gates each route — 401 (authN) and 403 (authZ) are distinct. Enable with `JWT_HS256_SECRET`.
- **gRPC control plane (Stage 7 pt2):** a typed **protobuf** contract (`proto/worker_control.proto`) between gateway and worker — data plane stays on the async queue, control plane is sync gRPC. The worker serves live status/health on **:50051**; `GET /v1/admin/worker/status` calls it (503 if unreachable). Stubs are generated at Docker build; behind a port so `pytest` needs no gRPC.
- **Kubernetes (Stage 8 pt1):** a Helm chart (`deploy/helm/cosmos/`) deploys gateway + worker + backing stores — ConfigMap/Secret split, `/healthz` & `/readyz` probes, gateway **HPA**, dev-stores toggle (`backingServices.enabled`) for prod. Validate offline with `helm lint`/`helm template`; run live on `minikube` (see `deploy/README.md`).
- **Operator (Stage 8 pt2):** a `CosmosTenant` **CRD** + a `kopf` controller (`services/operator/`) — `kubectl apply` a CosmosTenant and it provisions the tenant via the admin API, writes the API key into a CR-owned Secret, and records the tenant id in status (idempotent). Ships in the chart under `operator.enabled` with its CRD + RBAC. Pure reconcile logic is unit-tested (`cd services/operator && pytest` → 3 passed).
- **Agent:** Claude/Groq agent loop (tool use + token budget), LLM behind a port, provider-swappable via `LLM_PROVIDER`.
- **Resilience (Stage 4):** the provider call is wrapped in timeout → retry(backoff+jitter) → circuit breaker → fallback; `POST /v1/jobs` sheds excess load with **429** via a distributed token bucket.
- **Endpoints (all `/v1/*` need an API key *or* an OIDC JWT, with the right scope):** `POST /v1/jobs` (202; 429 when shed *or* over quota), `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/document`, `GET /v1/jobs/{id}/events`, `GET /v1/jobs/{id}/stream` (SSE), `POST /v1/jobs/{id}/cancel`, `GET /v1/stats`, `GET /v1/jobs/{id}/view`; **admin** (need `X-Admin-Token`): `POST /v1/admin/tenants`, `POST /v1/admin/tenants/{id}/keys`, `GET /v1/admin/worker/status` (gRPC to the worker); `GET /healthz`, `GET /readyz` (no auth).
- **Tests:** gateway **76 passing** (`cd services/gateway && pytest`); operator **3 passing** (`cd services/operator && pytest` — pure reconcile logic, no cluster). Chart validated with `helm lint` + `helm template`.
- **Docs:** this file + `ENGINEERING-NOTES.md` + `STAGES.md`.
