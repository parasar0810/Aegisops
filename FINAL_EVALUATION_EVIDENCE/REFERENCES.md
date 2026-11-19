# References

All technologies, frameworks, protocols, documentation sources and products actually used in this project.

---

## Agentic Frameworks

| Technology | Version | Usage |
|---|---|---|
| **Google ADK** (`google-adk`) | ≥ 1.0.0 | `SequentialAgent`, `BaseAgent`, `Runner`, `InMemorySessionService`, `CallbackContext`, `InvocationContext`, `Event`, `EventActions` — SRE pipeline orchestration |
| **CrewAI** (`crewai`) | ≥ 0.80.0 | `Agent`, `Task`, `Crew`, `Process`, `LLM` — 5 A2A specialist agents + legacy 3-agent postmortem crew |
| **A2A SDK** (`a2a-sdk[http-server]`) | ≥ 1.3.0 | `create_client`, `AgentCard`, `AgentExecutor`, `InMemoryTaskStore`, JSON-RPC routes, DataPart — interoperability between ADK and CrewAI specialists |

---

## Web Framework and API

| Technology | Version | Usage |
|---|---|---|
| **FastAPI** | ≥ 0.111.0 | Main API server, dependency injection, Pydantic integration, background tasks |
| **Uvicorn** (`uvicorn[standard]`) | ≥ 0.30.0 | ASGI server |
| **Starlette** | (FastAPI transitive) | ASGI application for A2A specialist servers |

---

## MCP

| Technology | Usage |
|---|---|
| **MCP SDK** (`mcp`) | `FastMCP`, `ClientSession`, `create_connected_server_and_client_session` — real MCP server and in-process client |

---

## Data Validation and Configuration

| Technology | Version | Usage |
|---|---|---|
| **Pydantic** | ≥ 2.7.0 | All data models, API schemas, `BaseSettings`, `output_pydantic` for CrewAI |
| **pydantic-settings** | ≥ 2.3.0 | Environment-variable configuration (`get_settings()`) |
| **python-dotenv** | ≥ 1.0.1 | `.env` file loading |

---

## Observability and Monitoring

| Technology | Version | Usage |
|---|---|---|
| **Prometheus** (`prometheus-client`) | ≥ 0.20.0 | Counter, Histogram, Gauge instruments; `/api/metrics` endpoint |
| **OpenTelemetry** (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`) | — | TracerProvider, BatchSpanProcessor, OTLP export, per-agent spans |
| **Grafana** | 11.1.0 | Auto-provisioned dashboard querying Prometheus; Docker image |
| **Prometheus** (server) | 2.53.0 | Metrics collection, alerting rules; Docker image |
| **Alertmanager** | 0.27.0 | Alert routing to AegisOps webhook; Docker image |

---

## Security

| Technology | Version | Usage |
|---|---|---|
| **PyJWT** | ≥ 2.10.0 | HS256 JWT token creation and validation |

---

## HTTP Client

| Technology | Version | Usage |
|---|---|---|
| **httpx** | ≥ 0.27.0 | Async HTTP client for MCP tools, A2A HTTP calls, test ASGITransport, remediation hook execution |

---

## Storage

| Technology | Version | Usage |
|---|---|---|
| **psycopg** (`psycopg[binary]`) | ≥ 3.2.0 | PostgreSQL driver for legacy SOC storage |
| **psycopg-pool** | ≥ 3.2.0 | Connection pooling for PostgreSQL |
| **pgvector** | (via Docker `pgvector/pgvector:pg16`) | Vector extension for PostgreSQL; schema in `postgres_pgvector.sql` |
| **Redis** (`redis`) | ≥ 5.0.0 | Optional CrewAI lock store (optional Docker profile) |
| **SQLite** | (stdlib) | ShopFront test database, demo target state |

---

## LLM Backends

| Provider | Usage |
|---|---|
| **Google Gemini** (`google-genai`) | Primary LLM for CrewAI specialists when `GEMINI_API_KEY` is set; `gemini-2.0-flash-lite` default |
| **OpenAI** | Alternative LLM backend (`OPENAI_API_KEY`); also used as eval quality judge (`gpt-4o-mini`) |
| **Ollama** | Local LLM fallback (optional) |

---

## Infrastructure and Deployment

| Technology | Usage |
|---|---|
| **Docker** | Containerisation; `Dockerfile` for AegisOps, demo-app, ShopFront API |
| **Docker Compose** | Local full-stack orchestration |
| **Terraform** (HashiCorp, ≥ 1.6) | AWS EC2 provisioning (`infra/aws/terraform/`) |
| **AWS EC2** (`t4g.small`, ap-south-1) | Compute — Amazon Linux 2023 (Graviton arm64) |
| **AWS SSM Parameter Store** | Secret management (SecureString parameters) |
| **Nginx** | HTTPS termination, reverse proxy; `docker/nginx/` |
| **sslip.io** | Wildcard DNS-over-IP for HTTPS without a registered domain |

---

## Frontend (ShopFront)

| Technology | Usage |
|---|---|
| **React** | ShopFront SPA (`shopfront/web/src/App.jsx`) |
| **Vite** | Build tool (`shopfront/web/vite.config.js`) |

---

## Testing and Quality

| Technology | Version | Usage |
|---|---|---|
| **pytest** | ≥ 8.2.0 | Test runner |
| **pytest-asyncio** | ≥ 1.4.1 | Async test support |
| **ruff** | ≥ 0.5.0 | Linting and formatting |

---

## Security and Protocols

| Standard/Protocol | Usage |
|---|---|
| **A2A Protocol** (Google Agent-to-Agent) | Interoperability standard — Agent Cards, JSON-RPC tasks, artifacts, DataPart messages |
| **MCP Protocol** (Model Context Protocol, Anthropic) | Tool discovery and invocation protocol |
| **JWT / RFC 7519** | Authentication token format |
| **OpenTelemetry** | Distributed tracing standard |
| **OTLP** (OpenTelemetry Protocol) | Span export format |
| **Prometheus Exposition Format** | Metrics format |
| **PromQL** | Prometheus query language (used in `run_promql` MCP tool) |
| **MITRE ATT&CK** | Knowledge base for security incident classification (`app/data/knowledge_base/mitre-attack-ttps.txt`) |

---

## Python Standard Library (Notable Usage)

| Module | Usage |
|---|---|
| `asyncio` | Concurrent evidence collection, A2A delegation, pipeline execution |
| `re` | Prompt-injection detection patterns, PromQL validation, keyword routing |
| `socket` | TCP database probe in MCP tool |
| `subprocess` / `shutil` | Docker inspect command in MCP tool |
| `logging` | Structured application logging throughout |
| `threading` | Rate limiter lock |
| `pathlib` | Filesystem StateStore paths |

