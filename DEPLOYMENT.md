# Deployment

## Local Docker Compose (verified)

```bash
docker compose up -d --build
```

| Service | Image | Port | Notes |
|---|---|---|---|
| aegisops | built from `Dockerfile` (uv lockfile, non-root) | 8000 | state in volume `aegis-data`, health check `/api/v1/system/health` |
| demo-app | `demo_app/Dockerfile` | 9000 | ShopFront demo target |
| prometheus | prom/prometheus:v2.53.0 | 9090 | scrapes both, rules in `docker/prometheus/alerts.yml` |
| alertmanager | prom/alertmanager:v0.27.0 | 9093 | routes to the AegisOps webhook |
| grafana | grafana/grafana:11.1.0 | 3000 | provisioned datasource and dashboard |
| postgres, redis | profile `storage` | – | only for the legacy SOC endpoints (`USE_POSTGRES=true`) |

All services use `restart: unless-stopped`. The aegisops logs rotate (10 MB × 3).

Run the demo inside the stack. It registers ShopFront in Prometheus mode and writes the ingest key that Alertmanager reads:

```bash
docker compose exec aegisops python scripts/run_demo.py --docker --scenario db --auto-approve
```

LLM keys come from your shell or `.env` (`GEMINI_API_KEY`, `OPENAI_API_KEY`). Set `AEGIS_LLM_MODE=off` to force deterministic mode.

Stop the stack:

```bash
docker compose down
```

## Production configuration

| Setting | Production value |
|---|---|
| `JWT_SECRET_KEY` | long random secret (secret manager) |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | change; these are the operator login |
| `AUTH_REQUIRED` | `true` |
| `AEGIS_PUBLIC_URL` | public HTTPS URL of AegisOps |
| `AEGIS_MONITOR_INTERVAL_SECONDS` | `30` if you are not using Alertmanager |
| `AEGIS_HOOK_TOKEN_<APP>` | per-app secret shared with the app's hooks |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | optional LLM reasoning |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | optional trace export |

* Terminate TLS in front of AegisOps (ALB/nginx). The console and API use bearer tokens, so serve them only over HTTPS.
* Expose only port 8000 behind the proxy. Prometheus, Alertmanager and Grafana should stay internal.
* Persist `/app/data` (incident state + audit log). Run **one** replica; the file store is single-writer.
* Back up `/app/data/sre_state` regularly; it holds incident history and the audit trail.

## AWS

Deployed as a single EC2 `t4g.small` in ap-south-1 running this same Compose stack (≈ $13.65/month). Terraform, costs, access, update/restart/stop/destroy commands and security notes are in [infra/aws/README.md](infra/aws/README.md). ECS Fargate (≈ $42/month) and Lightsail (no savings from stopping) were evaluated and rejected on cost.

### ShopFront on the same instance

`infra/aws/redeploy.sh` also starts ShopFront (`shopfront/`) as its own Compose project (`-p shopfront`) on the shared `aegis-net` network, at https://shop.13-127-211-52.sslip.io/. It adds about 200 MiB of memory at rest (PostgreSQL, API, web, synthetic shoppers) and no AWS resources; its secrets (`SHOPFRONT_DB_PASSWORD`, `SHOPFRONT_FAULT_TOKEN`, the ops token `AEGIS_HOOK_TOKEN_SHOPFRONT`) live in SSM Parameter Store with the others. Manage it independently:

```bash
cd /opt/aegisops/app/shopfront && sudo docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.aegis.yml ps
```

See [shopfront/README.md](shopfront/README.md) for fault injection and the demonstration sequence.
