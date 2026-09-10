# Cosmos — Complete End-to-End Testing Guide

How to verify the whole platform from your machine, top to bottom. Three tracks —
run them in order:

- **Track A — Automated tests** (~1 min, no infrastructure): proves the code.
- **Track B — Full stack on Docker Compose** (~10 min): exercises every feature over HTTP.
- **Track C — Kubernetes on minikube** (~15 min, optional): the Helm chart + operator.

Then **Track D** (load test + chaos + SLO loop) and a **5-minute smoke script**.

> Per-stage detail and a troubleshooting table for each feature live in
> [`STAGES.md`](STAGES.md). This guide is the runnable, end-to-end path.

---

## 0. Prerequisites

| Tool | Needed for | Check |
|------|-----------|-------|
| Docker + Compose v2 | Track B | `docker compose version` |
| Python 3.12 | Track A, load test | `python3.12 --version` |
| minikube, kubectl, helm | Track C | `minikube version` · `helm version` |
| (optional) `grpcurl`, `rpk` | poking gRPC / Redpanda directly | `grpcurl -version` |
| (optional) a free **Groq** API key | a *fully green* agent run (Stage 3) | from console.groq.com |

Clone + enter the repo:
```bash
git clone https://github.com/akhil27051999/cosmos.git
cd cosmos
```

> **About the LLM key:** the worker runs a real Claude/Groq agent. With a provider
> key, jobs reach **succeeded**. *Without* one, the pipeline still fully works —
> the job goes `pending → running → failed` with an agent error — which is a valid
> test of the failure path. Set a free Groq key to see green jobs (Track B, step 3).

---

## Track A — Automated tests (no infrastructure)

The fastest confidence: in-memory adapters, no DB/Redis/Kafka needed.

```bash
# gateway
cd services/gateway
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                      # expect: 108 passed
```
```bash
# operator (reuse the same venv or make one)
cd ../operator
pip install -r requirements.txt
pytest -q                      # expect: 3 passed
```
```bash
# helm chart (offline validation)
cd ../..
helm lint deploy/helm/cosmos              # 0 charts failed
helm template rel deploy/helm/cosmos >/dev/null && echo "chart renders OK"
```

✅ **Pass criteria:** `108 passed`, `3 passed`, chart lints + renders.

---

## Track B — Full stack on Docker Compose

Everything below runs from `services/gateway`.

### B.1 — Configure + bring the stack up
```bash
cd services/gateway

# Secrets/knobs the stack reads (export so compose forwards them):
export ADMIN_TOKEN=dev-admin-secret          # enables the admin API
export JWT_HS256_SECRET=dev-jwt-secret        # enables OIDC/JWT auth
export LLM_PROVIDER=groq                       # optional: real agent runs
export GROQ_API_KEY=gsk_...                    # optional: your free Groq key
export LOG_FORMAT=text                         # readable logs while testing

docker compose up -d --build
# wait for the API to be healthy:
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done; echo "gateway ready"
docker compose ps                              # postgres/redis/mongo/redpanda/gateway/worker up
```
Interactive API docs: <http://localhost:8000/docs>

### B.2 — Auth is required (Stage 6/7)
```bash
# No credential -> 401
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs \
  -H 'content-type: application/json' -d '{"payload":{"prompt":"hi"}}'      # 401
```
Create a tenant + its first API key (admin plane), capture the key:
```bash
curl -s -XPOST localhost:8000/v1/admin/tenants -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"name":"Acme","monthly_quota":1000}' | tee /tmp/tenant.json | python3 -m json.tool
export KEY=$(python3 -c 'import json;print(json.load(open("/tmp/tenant.json"))["api_key"])')
export TID=$(python3 -c 'import json;print(json.load(open("/tmp/tenant.json"))["tenant_id"])')
echo "KEY=$KEY  TID=$TID"
```

### B.3 — Job lifecycle (Stages 0–3)
```bash
# submit -> 202 + id (accept-fast)
curl -s -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"payload":{"prompt":"What is 6*7? Use the calculator tool, then state the result."}}' \
  | tee /tmp/job.json | python3 -m json.tool
export JID=$(python3 -c 'import json;print(json.load(open("/tmp/job.json"))["id"])')

sleep 4   # let the worker pick it up and run the agent
curl -s localhost:8000/v1/jobs/$JID -H "Authorization: Bearer $KEY" | python3 -m json.tool     # status
curl -s localhost:8000/v1/jobs/$JID/events -H "Authorization: Bearer $KEY" | python3 -m json.tool  # submitted->started->succeeded/failed
curl -s localhost:8000/v1/jobs/$JID/document -H "Authorization: Bearer $KEY" | python3 -m json.tool # payload + result/trajectory (Mongo)
curl -s localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" | python3 -m json.tool               # list (this tenant only)
```
- With a Groq key: status → **succeeded**, document has the agent's answer + trajectory.
- Without: status → **failed**, events show `submitted → started → failed` (pipeline proven).

**Idempotency (Stage 0):** same `Idempotency-Key` returns the same job, 202 then 200:
```bash
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: demo-1' -H 'content-type: application/json' -d '{"payload":{"prompt":"x"}}'   # 202
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: demo-1' -H 'content-type: application/json' -d '{"payload":{"prompt":"x"}}'   # 200
```

**Live SSE stream (Stage 3):** submit a job, then in another terminal:
```bash
curl -N localhost:8000/v1/jobs/$JID/stream -H "Authorization: Bearer $KEY"   # data: {...} frames until end
```

**Cancel (Stage 1):**
```bash
NEW=$(curl -s -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d '{"payload":{"prompt":"long"}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -XPOST localhost:8000/v1/jobs/$NEW/cancel -H "Authorization: Bearer $KEY" | python3 -m json.tool
```

### B.4 — Polyglot persistence + cache (Stage 2)
```bash
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT id,tenant_id,status,version FROM jobs LIMIT 5;"
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT job_id,type FROM job_events ORDER BY at DESC LIMIT 8;"
docker compose exec mongo mongosh --quiet cosmos --eval 'db.job_documents.findOne()'
docker compose exec redis redis-cli KEYS 'job:*'      # cache-aside entries
```

### B.5 — Multi-tenant isolation + quota (Stage 6)
```bash
# second tenant/key can't see the first tenant's job -> 404 (never leaked)
curl -s -XPOST localhost:8000/v1/admin/tenants -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"name":"Other"}' > /tmp/t2.json
K2=$(python3 -c 'import json;print(json.load(open("/tmp/t2.json"))["api_key"])')
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/jobs/$JID -H "Authorization: Bearer $K2"   # 404

# quota: a tenant with quota 1, second submit -> 429 quota_exceeded
curl -s -XPOST localhost:8000/v1/admin/tenants -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"name":"Small","monthly_quota":1}' > /tmp/t3.json
K3=$(python3 -c 'import json;print(json.load(open("/tmp/t3.json"))["api_key"])')
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $K3" -H 'content-type: application/json' -d '{"payload":{"p":1}}'   # 202
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $K3" -H 'content-type: application/json' -d '{"payload":{"p":2}}'   # 429
```

### B.6 — OAuth2 / JWT + scopes (Stage 7 pt1)
Mint an HS256 token inside the container (so `app` is importable), then use it:
```bash
TOKEN=$(docker compose exec -T gateway python -c "
from app.auth.jwt import encode_hs256; import time
print(encode_hs256({'tenant_id':'$TID','scope':'jobs:read jobs:write','sub':'demo','exp':int(time.time())+3600}, 'dev-jwt-secret'))")
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"payload":{"p":1}}'   # 202

# a read-only token cannot submit -> 403
RO=$(docker compose exec -T gateway python -c "
from app.auth.jwt import encode_hs256; import time
print(encode_hs256({'tenant_id':'$TID','scope':'jobs:read','sub':'ro','exp':int(time.time())+3600}, 'dev-jwt-secret'))")
curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $RO" -H 'content-type: application/json' -d '{"payload":{"p":1}}'   # 403
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/jobs -H "Authorization: Bearer $RO"          # 200 (read allowed)
```

### B.7 — gRPC control plane (Stage 7 pt2)
```bash
curl -s localhost:8000/v1/admin/worker/status -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool
# -> {"worker_id":...,"uptime_seconds":...,"in_flight":0,"processed_total":N,"healthy":true}

# optional, straight to the worker's gRPC (published on :50051):
grpcurl -plaintext -import-path proto -proto worker_control.proto localhost:50051 cosmos.worker.v1.WorkerControl/GetStatus
```

### B.8 — Event backbone + CQRS (Stage 5)
```bash
sleep 3   # let the relay publish + projector fold
curl -s localhost:8000/v1/stats -H "Authorization: Bearer $KEY" | python3 -m json.tool          # counts per status (read model)
curl -s localhost:8000/v1/jobs/$JID/view -H "Authorization: Bearer $KEY" | python3 -m json.tool  # projected view of one job
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT job_id,status,version,event_count FROM job_read_model LIMIT 5;"
docker compose exec redpanda rpk group describe cqrs-projector 2>/dev/null | head    # consumer lag ~0
```

### B.9 — A/B experimentation (Stage 9)
```bash
curl -s -XPOST localhost:8000/v1/experiments -H "X-Admin-Token: $ADMIN_TOKEN" -H 'content-type: application/json' -d '{
  "key":"agent-model","description":"opus vs sonnet",
  "variants":[{"name":"control","weight":1,"config":{"model":"opus"}},
              {"name":"variant-a","weight":1,"config":{"model":"sonnet"}}]}' | python3 -m json.tool
curl -s -XPOST localhost:8000/v1/experiments/agent-model/status -H "X-Admin-Token: $ADMIN_TOKEN" -H 'content-type: application/json' -d '{"status":"running"}' >/dev/null
# same tenant -> same variant every time (sticky):
curl -s localhost:8000/v1/experiments/agent-model/assignment -H "Authorization: Bearer $KEY" | python3 -m json.tool
curl -s localhost:8000/v1/experiments/agent-model/assignment -H "Authorization: Bearer $KEY" | python3 -m json.tool
curl -s localhost:8000/v1/experiments/agent-model/results -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool
```

### B.10 — Saga / onboarding with compensation (Stage 11)
```bash
# happy path: provisions tenant + key + experiment atomically
curl -s -XPOST localhost:8000/v1/admin/onboarding -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"name":"SagaCorp","monthly_quota":300}' | python3 -m json.tool   # status "completed"

# failure path: watch it roll everything back
curl -s -XPOST "localhost:8000/v1/admin/onboarding?simulate_failure=true" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"name":"Doomed"}' | python3 -m json.tool                          # status "compensated"
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT name,status FROM sagas ORDER BY created_at DESC LIMIT 3;"
docker compose exec postgres psql -U cosmos -d cosmos -c "SELECT name FROM tenants;"   # 'Doomed' is NOT here (rolled back)
```

### B.11 — Observability (Stage 10)
```bash
curl -s localhost:8000/metrics | grep -E '^http_requests_total|^http_request_duration_seconds_(count|sum)|^jobs_submitted_total' | head
docker compose logs --tail=15 gateway     # structured logs (text mode here; JSON in prod)
```

### B.12 — SLO + chaos + graceful shutdown (Stage 12)
```bash
# baseline SLO (healthy)
curl -s localhost:8000/v1/admin/slo -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool   # availability ~1.0, budget ~1.0

# turn chaos ON (fail 30% of requests) — needs a recreate to read new env:
export CHAOS_ENABLED=true CHAOS_FAULT_RATE=0.3
docker compose up -d gateway
for i in $(seq 1 40); do curl -s -o /dev/null localhost:8000/healthz; done
curl -s localhost:8000/v1/admin/slo -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool   # availability < 1.0, budget DROPPED

# turn chaos OFF again
export CHAOS_ENABLED=false CHAOS_FAULT_RATE=0.0
docker compose up -d gateway

# graceful shutdown: worker finishes its current job on SIGTERM
docker compose stop worker
docker compose logs --tail=5 worker        # "drained; shutting down cleanly"
docker compose start worker
```

### B.13 — Tear down
```bash
docker compose down            # keep volumes
# docker compose down -v        # also wipe Postgres/Mongo data for a clean slate
```

---

## Track C — Kubernetes (minikube + Helm + operator) — optional

```bash
minikube start
minikube addons enable metrics-server          # for the gateway HPA

# build BOTH images into minikube's Docker (no registry needed):
eval $(minikube docker-env)
docker build -t cosmos-gateway:latest services/gateway
docker build -t cosmos-operator:latest services/operator

helm install cosmos deploy/helm/cosmos -n cosmos --create-namespace \
  --set secrets.adminToken=$(openssl rand -hex 16) \
  --set secrets.jwtHs256Secret=$(openssl rand -hex 32)
kubectl -n cosmos get pods -w                  # wait until all Running/Ready (Ctrl-C to stop watching)
```
Reach it + drive the operator:
```bash
kubectl -n cosmos port-forward svc/cosmos-gateway 8000:8000 &
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done; echo ready

# operator: declare a tenant as a k8s object; the controller provisions it
kubectl -n cosmos apply -f deploy/helm/cosmos/samples/cosmostenant-example.yaml
kubectl -n cosmos get cosmostenants               # PHASE -> Ready, TENANT id fills in
kubectl -n cosmos get secret acme-credentials -o jsonpath='{.data.api_key}' | base64 -d; echo
```
Tear down:
```bash
kubectl -n cosmos delete -f deploy/helm/cosmos/samples/cosmostenant-example.yaml
helm uninstall cosmos -n cosmos
minikube stop
```

✅ **Pass criteria:** all pods Ready; `kubectl get ctenant` shows `Ready`; the
operator-created Secret's API key authenticates against the gateway.

---

## Track D — Load test (Stage 12)

With the Compose stack up and `$KEY` set (Track B.2):
```bash
cd services/gateway
python scripts/loadtest.py --url http://localhost:8000 --key "$KEY" --n 500 --concurrency 50
# prints throughput + p50/p95/p99 + status split — compare against /v1/admin/slo
```

---

## 5-minute smoke (the essentials)

```bash
cd services/gateway
export ADMIN_TOKEN=dev-admin-secret JWT_HS256_SECRET=dev-jwt-secret
docker compose up -d --build
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done
KEY=$(curl -s -XPOST localhost:8000/v1/admin/tenants -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"name":"Smoke"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
curl -s -XPOST localhost:8000/v1/jobs -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d '{"payload":{"prompt":"hi"}}' | python3 -m json.tool
curl -s localhost:8000/v1/admin/worker/status -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool
curl -s localhost:8000/metrics | grep -c http_requests_total
curl -s localhost:8000/v1/admin/slo -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool
docker compose down
```

---

## Troubleshooting (quick index)

| Symptom | Where to look |
|---|---|
| A curl returns `000` | server not ready — re-run the `until curl -sf .../healthz` wait loop |
| Everything is `401` | that's auth working — create a tenant/key (B.2) and send `Authorization: Bearer $KEY` |
| Admin call `403 "admin API is disabled"` | `ADMIN_TOKEN` not set before `up` — `export ADMIN_TOKEN=... && docker compose up -d gateway` |
| Job stuck `pending` | worker not consuming — `docker compose logs worker`; ensure the `worker` container is up |
| Job `failed` immediately | no/invalid LLM key — set `LLM_PROVIDER=groq` + `GROQ_API_KEY`, `docker compose up -d worker` (this is expected without a key) |
| Env change ignored | Compose only reads env at (re)create — `export VAR=... && docker compose up -d <service>` |
| `/v1/stats` empty or `/view` 404 | eventual consistency — wait a few seconds for relay + projector |
| Chaos won't turn on/off | `export CHAOS_ENABLED=... && docker compose up -d gateway` (recreate reads new env) |
| Per-feature deep dive | each feature's own test + troubleshoot table is in [`STAGES.md`](STAGES.md) |

Full architecture + the "why" behind every component: [`ENGINEERING-NOTES.md`](ENGINEERING-NOTES.md).
