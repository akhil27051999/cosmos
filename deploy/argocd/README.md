# ArgoCD — GitOps for Cosmos

ArgoCD continuously reconciles the cluster to the **Helm chart in Git**
(`deploy/helm/cosmos`). This is the GitOps use case: Git is the source of truth,
ArgoCD is the sync engine — you deploy by committing, and drift is auto-corrected.

## Prerequisite — the images must be in the cluster

The chart uses local image tags (`cosmos-gateway:latest`, `cosmos-operator:latest`)
with `pullPolicy: IfNotPresent`, so build them into minikube first (ArgoCD only
applies manifests — it doesn't build images):
```bash
minikube start && minikube addons enable metrics-server
eval $(minikube docker-env)
docker build -t cosmos-gateway:latest services/gateway
docker build -t cosmos-operator:latest services/operator
```

## Install ArgoCD
```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server

# UI (https://localhost:8080) + admin password:
kubectl -n argocd port-forward svc/argocd-server 8080:443 &
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```
Log in at <https://localhost:8080> as `admin` with that password (accept the self-signed cert).

## Deploy Cosmos via GitOps
```bash
kubectl apply -f deploy/argocd/application.yaml
```
In the UI (or CLI) watch the `cosmos` Application go **Synced / Healthy** — ArgoCD
created the namespace and applied the whole chart from Git.
```bash
kubectl -n cosmos get pods            # gateway/worker/operator/stores come up
```

## Test the two GitOps use cases

**1. Auto-sync from Git.** Change a value in `deploy/helm/cosmos/values.yaml`
(e.g. `gateway.replicaCount: 3`), commit, and push. Within ~3 min (or click
**Refresh → Sync**) ArgoCD applies it:
```bash
kubectl -n cosmos get deploy cosmos-gateway   # replicas match what's in Git
```

**2. Drift detection + self-heal.** Hand-edit the live cluster and watch ArgoCD
revert it:
```bash
kubectl -n cosmos scale deploy/cosmos-gateway --replicas=5
# ArgoCD marks the app OutOfSync, then (selfHeal) scales it back to the Git value
kubectl -n cosmos get deploy cosmos-gateway -w
```

## Tear down
```bash
kubectl delete -f deploy/argocd/application.yaml
kubectl delete namespace argocd
```

## Notes
- `automated.selfHeal` is what reverts manual drift; `prune` deletes resources you
  remove from Git. Turn them off in `application.yaml` for a manual-sync workflow.
- Secrets are inlined in `application.yaml` for the demo — in production use the
  External Secrets Operator or a SecretGenerator, never plaintext in Git.
