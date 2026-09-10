# The Cosmos operator image — runs `kopf` against the CosmosTenant CRD.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /operator

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# --standalone: no peering (single operator instance). Namespace is injected by
# the chart via WATCH_NAMESPACE; default to all namespaces if unset.
CMD ["sh", "-c", "kopf run -m app.handlers --standalone --namespace=${WATCH_NAMESPACE:-*}"]
