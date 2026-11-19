# Connecting an application to AegisOps

AegisOps needs no code changes per application. Any app, in any language, connects with plain HTTP. The Python SDK is optional.

## 1. What your app should expose

| Endpoint | Required | Purpose |
|---|---|---|
| Health check (e.g. `GET /health`) | yes | 2xx = healthy, 5xx = unhealthy |
| Prometheus metrics (`/metrics`) or a Prometheus server | recommended | numeric signals |
| Recent logs as JSON (`GET /logs/recent` → `[{timestamp, level, service, message}]`) | recommended | log evidence |
| Remediation hooks (`POST /admin/<action>`, bearer-protected) | optional | actions AegisOps may run after approval |

You can skip the log endpoint and send logs and exceptions through the webhook instead.

## 2. Register

Get an admin/sre token, then POST a manifest. The ShopFront example is `demo_app/registration.py`.

```bash
curl -X POST https://aegisops.example.com/api/auth/token -H "Content-Type: application/json" -d '{"username":"<user>","password":"<password>","role":"sre"}'
```

```json
POST /api/v1/applications/register
{
  "name": "my-shop",
  "description": "Customer storefront",
  "base_url": "https://shop.example.com",
  "environment": "production",
  "tech_stack": ["node", "express", "postgresql"],
  "health_check_url": "https://shop.example.com/health",
  "services": [{"name": "shop-api", "kind": "http-api", "depends_on": ["shop-db"], "endpoints": ["/api/products"]},
               {"name": "shop-db", "kind": "postgresql"}],
  "deployment": {"platform": "aws-ecs", "strategy": "rolling"},
  "monitoring": {
    "prometheus_url": "https://prometheus.internal.example.com",
    "logs_url": "https://shop.example.com/logs/recent",
    "metric_queries": {
      "error_rate": "sum(rate(http_requests_total{job=\"shop\",status=~\"5..\"}[1m])) / sum(rate(http_requests_total{job=\"shop\"}[1m]))",
      "p95_latency_seconds": "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job=\"shop\"}[5m])))",
      "cpu_percent": "100 * avg(rate(process_cpu_seconds_total{job=\"shop\"}[1m]))",
      "memory_percent": "100 * process_resident_memory_bytes{job=\"shop\"} / 536870912"
    },
    "thresholds": {"error_rate": 0.05, "p95_latency_seconds": 1.5}
  },
  "remediation_hooks": [
    {"name": "restart", "description": "Restart API tasks", "url": "https://shop.example.com/admin/restart",
     "risk": "high", "addresses": ["memory_exhaustion", "cpu_saturation", "service_unavailable"],
     "rollback_hint": "Tasks restart from the same image"}
  ]
}
```

The response contains `ingest_key`. It is shown once, so store it as a secret.

**Standard signals.** AegisOps reasons about these signals: `cpu_percent`, `memory_percent`, `error_rate` (0–1), `p95_latency_seconds`, `request_rate`, `db_pool_in_use_ratio`, `seconds_since_deploy`, `db_up` (1/0, the app's own database probe) and `up` (1/0, Prometheus' scrape result). Map any of them with PromQL (when `prometheus_url` is set) or a bare metric name (scrape mode with `metrics_url`). Unmapped signals are simply not used. `up` and `db_up` matter: without them an outage leaves only a failed health check, and AegisOps will (correctly) report insufficient evidence instead of acting.

**Per-app tuning** (inside `monitoring`):

| Field | Default | Use |
|---|---|---|
| `thresholds` | see `analysis.py` | override a signal's threshold, e.g. `{"p95_latency_seconds": 1.5}` |
| `verify_settle_seconds` | `AEGIS_VERIFY_SETTLE_SECONDS` | wait after a fix before verifying; set it longer than your PromQL `rate()` window |
| `log_window_minutes` | 10 | how far back log evidence reaches; keep it short if incidents can happen back to back |
| `database_host`, `database_port` | none | only if AegisOps can actually reach the database over TCP; a private database would produce a false "unreachable" finding |

**Failure categories** for `addresses`: `cpu_saturation`, `memory_exhaustion`, `http_errors`, `high_latency`, `database_failure`, `service_unavailable`, `bad_deployment`, `abnormal_traffic`.

**Hook rules.** Hooks must be on the same host as `base_url`. AegisOps POSTs `{"incident_id": "..."}` with `Authorization: Bearer $AEGIS_HOOK_TOKEN_<NAME>`, where the name is uppercased and `-` becomes `_` (e.g. `AEGIS_HOOK_TOKEN_MY_SHOP`). Your hook must verify that token. `risk: high` and `risk: low` hooks always need human approval.

## 3. Send incidents and events

**Prometheus Alertmanager:**

```yaml
receivers:
  - name: aegisops
    webhook_configs:
      - url: https://aegisops.example.com/api/v1/incidents/webhook
        http_config:
          authorization: {type: Bearer, credentials_file: /etc/alertmanager/aegis_ingest_key}
```

Alerts need the label `application: <registered name>`. `severity` (low/medium/high/critical) and `service` are optional.

**Generic event** (any language):

```bash
curl -X POST https://aegisops.example.com/api/v1/incidents/webhook -H "X-AegisOps-Key: $AEGISOPS_KEY" -H "Content-Type: application/json" -d '{"application":"my-shop","event_type":"alert","title":"Checkout 5xx spike","severity":"high","service":"shop-api"}'
```

| `event_type` | Effect |
|---|---|
| `alert`, `custom` | opens an incident (or joins the app's open incident) and starts the investigation |
| `exception` | stored as log evidence; also opens an incident when severity is high/critical |
| `deployment` | stored as log evidence ("Deployment completed: release vX") |
| `health` | stored as log evidence |

Optional fields: `description`, `labels`, `metrics` (`{signal: value}`), `logs` (≤200 lines), `correlation_id`. Add `?wait=true` to get the finished investigation in the response.

## 4. Optional Python SDK

```bash
pip install ./sdk
```

```python
from aegisops_sdk import AegisOpsClient, AegisOpsMiddleware
aegis = AegisOpsClient("https://aegisops.example.com", app_name="my-shop", ingest_key=os.environ["AEGISOPS_KEY"])
app.add_middleware(AegisOpsMiddleware, client=aegis, service="shop-api")  # X-Correlation-ID + exception reporting
aegis.report_deployment("v2.3.1")
```

Delivery failures are logged and never raised into your request path.

## 5. Checklist for an AWS-hosted site

1. AegisOps must reach your health, metrics/Prometheus and log URLs (security groups / private networking).
2. Register the app with its public or internal URLs.
3. Put the ingest key in Alertmanager (or your app's secrets) and the hook token in both AegisOps (`AEGIS_HOOK_TOKEN_<NAME>`) and your app.
4. Send one test event and check the investigation in the console.

## 6. Worked example: ShopFront

[`shopfront/`](shopfront/README.md) is a complete integration you can copy:

- **Telemetry:** `/health` (checks the database), `/internal/metrics` (request counter + latency histogram + `db_up` + container CPU/memory), `/internal/logs` (structured JSON).
- **Registration:** `registration()` in [`scripts/shopfront_demo.py`](scripts/shopfront_demo.py), with Prometheus-mode PromQL for every standard signal, `up`/`db_up`, `verify_settle_seconds: 45`, `log_window_minutes: 2`, and six allowlisted hooks (five low-risk targeted fixes plus a high-risk restart).
- **Alerts:** six rules in `docker/prometheus/alerts.yml` labelled `application: shopfront`. Alertmanager routes them with ShopFront's own key (`/etc/aegis-secrets/shopfront_ingest_key`). Each application gets its own route and key file.
- **Hooks:** AegisOps sends `Authorization: Bearer $AEGIS_HOOK_TOKEN_SHOPFRONT`; ShopFront compares it in constant time, and unknown actions return 404.
- **Network:** the API and web tier join `aegis-net`; the database does not.
