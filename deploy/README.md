# Deploying Cosmos on Kubernetes (Stage 8, part 1)

A Helm chart that runs the whole platform on Kubernetes: the **gateway** and
**worker** (same image, different commands) plus dev **backing stores**
(Postgres, Redis, MongoDB, Redpanda). The gateway and worker read their
configuration from a **ConfigMap** (non-secret) and a **Secret**.

```
deploy/helm/cosmos/
├── Chart.yaml            # chart metadata (chart version vs appVersion)
├── values.yaml          # all the knobs (image, replicas, resources, secrets, stores)
└── templates/
    ├── _helpers.tpl      # name + label helpers
    ├── configmap.yaml    # non-secret env; computes in-cluster connection URLs
    ├── secret.yaml       # ADMIN_TOKEN, JWT secret, provider keys
    ├── gateway-*.yaml    # Deployment + Service + HPA (probes on /healthz,/readyz)
    ├── worker-*.yaml     # Deployment + Service (gRPC :50051, tcp readiness)
    └── postgres|redis|mongo|redpanda.yaml   # dev stores (toggle with backingServices.enabled)
```

## Validate without a cluster

```bash
helm lint deploy/helm/cosmos
helm template rel deploy/helm/cosmos            # render every manifest to stdout
```

## Run it on minikube

```bash
# 1) Start a cluster with metrics (needed for the gateway HPA).
minikube start
minikube addons enable metrics-server

# 2) Build the app image straight into minikube's Docker daemon (no registry).
eval $(minikube docker-env)
docker build -t cosmos-gateway:latest services/gateway

# 3) Install the chart (override the default admin token!).
helm install cosmos deploy/helm/cosmos \
  --namespace cosmos --create-namespace \
  --set secrets.adminToken=$(openssl rand -hex 16) \
  --set secrets.jwtHs256Secret=$(openssl rand -hex 32)

kubectl -n cosmos get pods -w        # wait for Running/Ready

# 4) Reach the gateway.
kubectl -n cosmos port-forward svc/cosmos-gateway 8000:8000
curl -s localhost:8000/healthz
```

## Production shape

Set `backingServices.enabled=false` and point the URLs at managed services; the
gateway/worker manifests are unchanged:

```bash
helm upgrade --install cosmos deploy/helm/cosmos -n cosmos \
  --set backingServices.enabled=false \
  --set config.databaseUrl='postgresql+asyncpg://…@rds:5432/cosmos' \
  --set config.redisUrl='redis://elasticache:6379/0' \
  --set config.mongoUrl='mongodb://…atlas…' \
  --set config.redpandaBrokers='broker.msk:9092' \
  --set secrets.create=false --set secrets.existingSecret=cosmos-prod-secrets
```

See `docs/STAGES.md` (Stage 8) for the full test + troubleshooting runbook.
