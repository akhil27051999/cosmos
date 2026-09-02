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

## 7. What exists now (created across Stages 0–2)
- **Services:** `gateway` (FastAPI API) + `worker` (queue consumer) — one codebase, two processes.
- **Stores:** Postgres (jobs + job_events), MongoDB (job_documents), Redis (job queue + cache).
- **Endpoints:** `POST /v1/jobs`, `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/document`, `GET /v1/jobs/{id}/events`, `POST /v1/jobs/{id}/cancel`, `GET /healthz`, `GET /readyz`.
- **Tests:** 17 passing (API, worker, documents, cache).
- **Docs:** this file + `ENGINEERING-NOTES.md` + `STAGES.md`.
