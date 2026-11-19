# Deployment Verification

## 1. Local Docker Compose Stack

**File:** `docker-compose.yml`

### Services

| Service | Image | Port | Description |
|---|---|---|---|
| `aegisops` | `aegisops:local` (built from `Dockerfile`) | 8000 | Main FastAPI application |
| `demo-app` | `aegisops-demo-app:local` (built from `demo_app/Dockerfile`) | 9000 | Synthetic demo target (ShopFront simulator) |
| `prometheus` | `prom/prometheus:v2.53.0` | 9090 | Metrics collection and alerting |
| `alertmanager` | `prom/alertmanager:v0.27.0` | 9093 | Alert routing → AegisOps webhook |
| `grafana` | `grafana/grafana:11.1.0` | 3000 | Dashboards (provisioned automatically) |
| `selfcheck` (profile `check`) | `aegisops:local` | — | One-shot E2E verification (9 scenarios) |
| `shopfront-check` (profile `check`) | `aegisops:local` | — | One-shot ShopFront 6-scenario demo |
| `postgres` (profile `storage`) | `pgvector/pgvector:pg16` | 5432 | Optional PostgreSQL + pgvector |
| `redis` (profile `storage`) | `redis:7-alpine` | 6379 | Optional Redis |

### Networking

```yaml
networks:
  default:
    name: aegis-net   # ShopFront (shopfront/docker-compose.aegis.yml) joins this network
```

The ShopFront e-commerce stack joins the same Docker network, making it reachable from AegisOps.

### Volumes

| Volume | Purpose |
|---|---|
| `aegis-data` | Persistent `IncidentState` and `RegisteredApplication` JSON files |
| `am-key` | Per-app ingest key shared between AegisOps and Alertmanager |
| `grafana-data` | Grafana persistent state |
| `pgdata` | PostgreSQL data (optional profile) |

### Dockerfile (Main Application)

**File:** `Dockerfile`

```dockerfile
FROM python:3.12-slim
RUN useradd -m aegis
USER aegis
WORKDIR /app
COPY --chown=aegis . .
RUN pip install uv && uv sync --no-dev
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s CMD curl -f http://localhost:8000/api/v1/system/health || exit 1
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

The container runs as a non-root user (`aegis`).

---

## 2. Nginx Reverse Proxy

**Files:** `docker/nginx/http-only.conf`, `docker/nginx/https.conf`, `docker/nginx/start.sh`

- **HTTP-only config:** Simple proxy to `aegisops:8000`
- **HTTPS config:** TLS termination using Let's Encrypt / sslip.io certificates with HSTS headers and security headers
- The Nginx container is **not in `docker-compose.yml`** (it's added separately for HTTPS deployments or on the EC2 instance)

---

## 3. Monitoring Configuration

### Prometheus Scrape Config

**File:** `docker/prometheus.yml`

Scrapes:
- `aegisops:8000/api/metrics` — main application metrics
- `demo-app:9000/metrics` — demo target metrics

### Alerting Rules

**File:** `docker/prometheus/alerts.yml`

9 Prometheus alert rules covering:
- `DemoTargetDown`, `DemoTargetHighError5xx`, `DemoTargetHighLatency`
- `DemoTargetHighCpu`, `DemoTargetHighMemory`
- `DemoTargetDatabaseUnhealthy`, `DemoTargetBadDeploy`, `DemoTargetAbnormalTraffic`
- `DemoTargetHighErrorRate`

### Alertmanager

**File:** `docker/alertmanager/alertmanager.yml`

Routes all alerts to `http://aegisops:8000/api/v1/incidents/webhook` with the per-app ingest key from `am-key` volume.

---

## 4. ShopFront Stack (Monitored Application)

**Directory:** `shopfront/`

The ShopFront project has its own Docker Compose file and joins `aegis-net`. Components:

| Component | Technology | Description |
|---|---|---|
| ShopFront API | FastAPI + SQLite/PostgreSQL | Products, orders, cart, checkout endpoints |
| ShopFront Web | React (Vite) + Nginx | E-commerce SPA |
| ShopFront DB | PostgreSQL | Product and order data |

ShopFront is registered via the AegisOps registration API with:
- 6 Prometheus alert rules
- 6 allowlisted remediation hooks (db pool reset, cache clear, rollback, rate-limit, restart, error-clear)
- Monitoring endpoints for health, metrics, and logs

---

## 5. AWS Deployment

**Files:** `infra/aws/terraform/main.tf`, `variables.tf`, `outputs.tf`, `user_data.sh.tftpl`

### Architecture

- **Single EC2 instance:** `t4g.small` (Graviton2, 2 vCPU, 2 GiB RAM), `ap-south-1`
- **AMI:** Amazon Linux 2023 (Graviton arm64, latest, via SSM parameter)
- **Storage:** Default 20 GiB gp3 EBS root volume
- **Network:** Default VPC, default subnets, single Security Group
- **No load balancer, no NAT gateway, no Elastic IP, no RDS, no EKS**

### Security Group

Ports allowed inbound:
- `443` (HTTPS) — from configured CIDR list
- `8000` (AegisOps direct) — from configured CIDR list
- All ports blocked from internet by default

### Secret Management

All secrets stored in AWS SSM Parameter Store as `SecureString`:
- `JWT_SECRET_KEY`, `DEMO_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`
- `AEGIS_HOOK_TOKEN_SHOPFRONT`, `DEMO_ADMIN_TOKEN`, `SHOPFRONT_FAULT_TOKEN`
- `SHOPFRONT_DB_PASSWORD`, `GEMINI_API_KEY` (optional)

Fetched by user-data script at EC2 startup — not baked into the Docker image.

### User Data Bootstrap (`user_data.sh.tftpl`)

- Installs Docker Engine and Docker Compose plugin
- Fetches secrets from SSM
- Writes `.env` and starts the Docker Compose stack
- Configures Nginx for HTTPS with sslip.io domain

### Live URL (as of 2026-11-12)

| URL | Access |
|---|---|
| `https://13-127-211-52.sslip.io/` | AegisOps console (requires operator login + allowed IP) |
| `https://shop.13-127-211-52.sslip.io/` | ShopFront store (allowed IPs only) |

---

## 6. Local vs. Deployed Components

| Component | Local Docker Compose | AWS EC2 |
|---|---|---|
| AegisOps FastAPI app | ✅ | ✅ |
| Demo target (ShopFront simulator) | ✅ | ✅ |
| ShopFront e-commerce app | ✅ (separate compose) | ✅ |
| Prometheus | ✅ | ✅ |
| Alertmanager | ✅ | ✅ |
| Grafana | ✅ | ✅ |
| Nginx HTTPS | ❌ (not in default compose) | ✅ |
| PostgreSQL (SRE state) | ❌ (filesystem JSON store used) | ❌ (filesystem JSON store) |
| PostgreSQL (ShopFront) | ✅ (shopfront compose) | ✅ |
| pgvector (legacy SOC) | Optional profile only | Not deployed |
| Redis | Optional profile only | Not deployed |

---

## 7. Persistence and State Recovery

**File:** `app/sre/store.py` — `StateStore`

All `IncidentState` and `RegisteredApplication` objects are serialised to JSON and written to the `aegis-data` volume after every pipeline step. On restart:
- Applications are reloaded automatically
- In-progress incidents resume from their last completed step (idempotent `completed_steps` check in `StepAgent`)

**AWS Reboot Test (from EVALUATION_REPORT.md):** After an EC2 reboot, all 5 containers came back automatically. API was healthy 42 seconds after the reboot request. All 9 previous incidents were still stored.

