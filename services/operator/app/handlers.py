"""
kopf handlers — the thin k8s glue around the pure `reconcile()` logic.

kopf turns Python functions into a Kubernetes operator: it watches the
`CosmosTenant` custom resource and calls these handlers on create/resume/update.
Each handler:
  1. reads the desired spec off the CR body,
  2. calls the framework-free `reconcile()` to decide what to do,
  3. does the k8s I/O (create/patch the credentials Secret),
  4. returns a dict that kopf merges into the CR's `status`.

Run it with:  kopf run -m app.handlers --standalone --namespace <ns>

`--standalone` skips kopf's peering (multiple-operator coordination), so we don't
need extra peering CRDs/RBAC — one operator instance is enough here.
"""

from __future__ import annotations

import os

import kopf

from app.admin_client import HttpAdminClient
from app.reconcile import TenantSpec, reconcile

GROUP = "cosmos.dev"
VERSION = "v1"
PLURAL = "cosmostenants"


def _admin_client() -> HttpAdminClient:
    # Config comes from the operator's env (wired by the Helm chart).
    base_url = os.environ.get("GATEWAY_ADMIN_URL", "http://cosmos-gateway:8000")
    token = os.environ.get("ADMIN_TOKEN", "")
    return HttpAdminClient(base_url, token)


def _spec(body_spec: dict) -> TenantSpec:
    return TenantSpec(
        display_name=body_spec.get("displayName") or body_spec.get("display_name") or "",
        monthly_quota=body_spec.get("monthlyQuota"),
    )


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
async def reconcile_tenant(spec, status, name, namespace, patch, logger, **_):
    """Provision (once) the tenant declared by a CosmosTenant CR.

    Bound to create + resume + update so the resource converges no matter how the
    handler is triggered — the idempotency in `reconcile()` makes that safe.
    """
    secret_name = f"{name}-credentials"
    admin = _admin_client()

    try:
        result = await reconcile(_spec(spec), dict(status or {}), admin, secret_name)
    except Exception as exc:  # noqa: BLE001 - transient admin/network failure
        # Tell kopf to retry with backoff rather than marking a permanent failure.
        patch.status["phase"] = "Provisioning"
        raise kopf.TemporaryError(f"admin API call failed: {exc}", delay=15) from exc

    if result.secret_data is not None:
        _upsert_secret(namespace, secret_name, name, result.secret_data, logger)

    # Returned dict is merged by kopf into the CR's status.
    logger.info(result.message)
    return result.status_patch


@kopf.on.delete(GROUP, VERSION, PLURAL)
async def on_delete(name, status, logger, **_):
    """The gateway admin API has no delete-tenant endpoint yet, so we can't
    deprovision remotely — log it and let k8s garbage-collect the owned Secret.
    (A future admin DELETE would be called here to fully tear down.)"""
    tid = (status or {}).get("tenantId")
    logger.info(f"CosmosTenant {name} deleted (tenant {tid} left in place; no admin delete API)")


def _upsert_secret(namespace: str, secret_name: str, owner_name: str, data: dict, logger) -> None:
    """Create or update the k8s Secret holding the tenant's credentials.

    kopf.adopt() sets the CR as the Secret's owner, so deleting the CR
    garbage-collects the Secret automatically.
    """
    import base64

    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001 - allow running out-of-cluster (kubeconfig)
        config.load_kube_config()

    api = client.CoreV1Api()
    encoded = {k: base64.b64encode(str(v).encode()).decode() for k, v in data.items()}
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=secret_name),
        type="Opaque",
        data=encoded,
    )
    kopf.adopt(body)  # set ownerReference to the CosmosTenant

    try:
        api.read_namespaced_secret(secret_name, namespace)
        api.replace_namespaced_secret(secret_name, namespace, body)
        logger.info(f"updated credentials Secret {secret_name}")
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            api.create_namespaced_secret(namespace, body)
            logger.info(f"created credentials Secret {secret_name}")
        else:
            raise
