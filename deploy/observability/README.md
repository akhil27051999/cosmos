# Observability stack — Prometheus + PLG + Alerting

Runs **alongside** the Cosmos Docker Compose stack and consumes the two feeds the
platform already emits: the Prometheus **`/metrics`** endpoint and the structured
**JSON logs**. Use it to learn Prometheus, Grafana, Loki/Promtail, and
Alertmanager *through Cosmos's own use cases*.

```
Cosmos gateway ──/metrics──▶ Prometheus ──▶ Alertmanager (rules fire)
      │                          ▲
      └── JSON stdout ─▶ Promtail─┼─▶ Loki ─┐
                                  └──────────┴─▶ Grafana (RED dashboard + logs)
```

## Run it

```bash
# 1) Cosmos must be up first (publishes :8000 on the host):
cd services/gateway
export ADMIN_TOKEN=dev-admin-secret JWT_HS256_SECRET=dev-jwt-secret
docker compose up -d

# 2) Start the observability stack:
cd ../../deploy/observability
docker compose -f docker-compose.observability.yaml up -d
```

Open:
- **Grafana** → <http://localhost:3000> (anonymous admin; dashboard **Cosmos → RED + Logs**)
- **Prometheus** → <http://localhost:9090>  (Status → Targets: `cosmos-gateway` should be **UP**)
- **Alertmanager** → <http://localhost:9093>

## Test each tool through a use case

**Prometheus (metrics + PromQL).** Generate traffic (create a key per the e2e
guide, submit jobs), then in Prometheus run:
```promql
sum(rate(http_requests_total[1m]))
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
```
Status → Targets shows the gateway scrape health — that's service discovery working.

**Grafana (dashboards).** The **RED + Logs** dashboard shows request rate, error %,
p95, in-flight, jobs submitted, and live gateway logs. Run the load test
(`services/gateway/scripts/loadtest.py`) and watch the panels move — that's the
metrics→dashboard use case.

**Loki + Promtail (logs).** In Grafana → Explore → **Loki**:
```logql
{container=~".*gateway.*"}                          # all gateway logs
{container=~".*gateway.*"} | json | http_status=`503`   # parse JSON, filter status
{container=~".*gateway.*"} | json | correlation_id=`<paste a request id>`
```
Send a request with `-H 'X-Request-ID: trace-me-1'`, then search that id — every
line for that one request. That's log aggregation + correlation.

**Alertmanager (alerting).** Turn on chaos so the error rate crosses the rule
threshold:
```bash
cd ../../services/gateway
export CHAOS_ENABLED=true CHAOS_FAULT_RATE=0.5 && docker compose up -d gateway
for i in $(seq 1 60); do curl -s -o /dev/null localhost:8000/healthz; done
```
Within ~1 min: **Prometheus → Alerts** shows `HighErrorRate` go Pending → Firing,
and it appears in **Alertmanager** (:9093). Turn it back off:
```bash
export CHAOS_ENABLED=false CHAOS_FAULT_RATE=0.0 && docker compose up -d gateway
```

### Slack notifications (real delivery), routed by severity

Alerts route to **two** Slack receivers by `severity` label:

| Route | Alerts | Receiver → channel | Behaviour |
|---|---|---|---|
| `severity="page"` | `HighErrorRate`, `GatewayDown` | `slack-page` → `#cosmos-pages` | notify immediately (`group_wait: 0s`), re-send every 30m |
| default (everything else) | `HighLatencyP95`, … | `slack` → `#cosmos-alerts` | standard cadence |

Each receiver reads its webhook from a secret file (`api_url_file`), so nothing
sensitive is inlined:

1. Create **two** Slack Incoming Webhooks (one per channel): Slack → Apps →
   *Incoming Webhooks* → add to `#cosmos-alerts` and to `#cosmos-pages` → copy each
   `https://hooks.slack.com/services/...` URL.
2. Put them in the two files (replace the placeholders) and keep them out of git:
   ```bash
   cd deploy/observability/alertmanager
   printf 'https://hooks.slack.com/services/XXX/YYY/ZZZ\n' > slack_api_url        # #cosmos-alerts
   printf 'https://hooks.slack.com/services/AAA/BBB/CCC\n' > slack_api_url_page   # #cosmos-pages
   git update-index --skip-worktree slack_api_url slack_api_url_page
   ```
   > Only have one channel/webhook? Point both files at the same URL — routing
   > still works, both just land in one channel.
3. Restart Alertmanager and fire a **page** (HighErrorRate is `severity=page`):
   ```bash
   cd deploy/observability && docker compose -f docker-compose.observability.yaml up -d alertmanager
   # trigger via chaos -> posts to #cosmos-pages, with a RESOLVED follow-up when it clears
   ```

> Until you set real URLs, Alertmanager still starts and shows alerts in its UI —
> it just 404s posting to the placeholders (harmless, logged). The `channel:` field
> matters only for Slack *app* tokens; incoming webhooks post to the channel the
> hook was created for.

## Tear down
```bash
cd deploy/observability && docker compose -f docker-compose.observability.yaml down -v
```

## Notes
- Uses `host.docker.internal` to scrape the host-published gateway, so no shared
  Docker network is required. On Linux the `extra_hosts: host-gateway` mapping
  makes that resolve.
- Promtail reads container logs via `/var/run/docker.sock` (read-only mount).
- Loki/Promtail are pinned to 2.9.x and Prometheus/Grafana to known-good tags;
  bump deliberately.
