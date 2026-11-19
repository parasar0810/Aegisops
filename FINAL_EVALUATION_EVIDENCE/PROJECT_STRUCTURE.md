# Project Structure — AegisOps Agentic AI Incident Investigation Platform

## Source Directory Tree

```
Agentic-AI-Incident-Investigation-Platform-main/
├── app/                            # Main application package
│   ├── agents/                     # Legacy SOC agents (BaseAgent pattern)
│   │   ├── base.py                 # Abstract BaseAgent class
│   │   ├── enrichment.py           # EnrichmentAgent — adds context to alerts
│   │   ├── evidence.py             # EvidenceAgent — retrieves evidence
│   │   ├── remediation.py          # RemediationAgent — plans remediation steps
│   │   └── triage.py               # TriageAgent — classifies severity/domain
│   ├── api/
│   │   └── routes.py               # Legacy SOC/RAG endpoints (/api/investigate, /api/ingest, etc.)
│   ├── core/
│   │   ├── config.py               # Pydantic Settings — all env-var config (get_settings())
│   │   ├── security.py             # JWT auth, Bearer scheme, RBAC, get_current_user()
│   │   └── telemetry.py            # Prometheus Counter/Histogram/Gauge metrics definitions
│   ├── data/
│   │   ├── knowledge_base/         # RAG knowledge base text files (runbooks, MITRE, past incidents)
│   │   └── sample_alerts/          # Sample JSON alert payloads for testing
│   ├── models/
│   │   ├── adk_crew_schemas.py     # Pydantic schemas: AegisIncidentBrief, CrewPostmortemReport, ADKCrewInvestigationResult
│   │   ├── generic_state.py        # GenericIncidentState for generic graph
│   │   ├── schemas.py              # Core Pydantic models: SecurityAlert, IncidentInput, UserRole, MCPToolManifest, AgentManifest, etc.
│   │   └── state.py                # InvestigationState for legacy SOC workflows
│   ├── rag/
│   │   ├── retriever.py            # RAG retriever: keyword search over knowledge_base documents
│   │   ├── uploaded_store.py       # In-memory store for uploaded documents
│   │   └── vector_store.py         # In-memory vector store (TF-IDF similarity)
│   ├── services/
│   │   ├── a2a.py                  # LocalA2ARegistry — capability-based A2A handoff registry (legacy SOC path)
│   │   ├── adk_orchestrator.py     # AegisOpsADKOrchestrator — Google ADK LlmAgent + Runner + InMemorySessionService
│   │   ├── crew_investigation.py   # AegisPostmortemCrew — 3-agent CrewAI crew (triage→synthesizer→postmortem writer)
│   │   ├── generic_incident_graph.py # Generic sequential incident graph (non-SRE path)
│   │   ├── ingestion.py            # Document ingestion for RAG
│   │   ├── integrations.py         # External integration stubs (Splunk, Sentinel, Okta, etc.)
│   │   ├── investigation_graph.py  # Legacy sequential investigation graph
│   │   ├── llm.py                  # LLM service wrapper (OpenAI/Gemini with fallback)
│   │   ├── mcp_tools.py            # LocalMCPToolRegistry — local MCP-style tool registry
│   │   ├── postmortem.py           # Postmortem generation service
│   │   ├── remediation_service.py  # Legacy remediation service (approval, execution, verification)
│   │   ├── report_store.py         # In-memory + disk report store
│   │   ├── session_memory.py       # Per-session context memory
│   │   └── tracing.py              # Basic tracing wrapper
│   ├── sre/                        # PRIMARY: AegisOps SRE pipeline
│   │   ├── a2a_specialists.py      # 5 CrewAI specialist agents exposed over A2A protocol (a2a-sdk)
│   │   ├── analysis.py             # Deterministic evidence scorer and RCA synthesizer
│   │   ├── console.html            # SRE web console (single-page HTML served by FastAPI)
│   │   ├── guardrails.py           # Prompt-injection patterns, secret redaction, rate limiter
│   │   ├── mcp_server.py           # Real MCP server (FastMCP) with 6 live tools + MCPToolbox client
│   │   ├── models.py               # Pydantic models: IncidentState, RegisteredApplication, Evidence, etc.
│   │   ├── monitor.py              # Built-in polling monitor (checks health, sends events)
│   │   ├── otel.py                 # OpenTelemetry setup (OTLP export or local)
│   │   ├── pipeline.py             # Google ADK SequentialAgent workflow (10 steps, callbacks, Runner)
│   │   ├── routes.py               # FastAPI /api/v1 router — webhook, approval, RBAC
│   │   └── store.py                # Filesystem StateStore — persist IncidentState + applications
│   ├── storage/
│   │   ├── db.py                   # SQLite/PostgreSQL database helper
│   │   ├── pgvector_store.py       # pgvector (PostgreSQL) vector store
│   │   └── postgres_pgvector.sql   # Schema init SQL for pgvector
│   ├── tests/
│   │   ├── test_adk_crew_workflow.py   # ~29 tests: ADK orchestrator + CrewAI crew (mocked LLM)
│   │   ├── test_investigation.py       # Legacy SOC investigation tests
│   │   ├── test_remediation_workflow.py # Legacy remediation approval tests
│   │   ├── test_sdk.py                 # SDK middleware tests
│   │   └── test_sre_pipeline.py        # 13 tests: SRE pipeline E2E (ASGI in-process)
│   └── main.py                     # FastAPI application factory, lifespan, startup
├── demo_app/
│   ├── Dockerfile
│   ├── main.py                     # Synthetic "ShopFront" demo target (FastAPI, fault injection endpoints)
│   └── registration.py             # Pre-built application registration manifest for demo target
├── docker/
│   ├── alertmanager/               # Alertmanager config (alerts forwarded to AegisOps webhook)
│   ├── grafana/provisioning/       # Grafana datasource + dashboard provisioning
│   ├── nginx/                      # Nginx reverse-proxy configs (HTTP-only and HTTPS with sslip.io)
│   ├── prometheus/alerts.yml       # Prometheus alerting rules (9 rules for SRE scenarios)
│   └── prometheus.yml              # Prometheus scrape config
├── docs/
│   ├── architecture.png            # Architecture diagram
│   └── screenshots/                # 3 live screenshots (console, ShopFront home, ShopFront products)
├── evals/
│   ├── cases/                      # 12 JSON evaluation test cases
│   ├── reports/
│   │   ├── latest.json             # 12/12 passed eval run (2026-11-11)
│   │   ├── platform_verification.json
│   │   └── sre_benchmark.json      # 9/9 scenario benchmark with timing data
│   └── run_eval.py                 # Evaluation harness script
├── infra/aws/terraform/            # Terraform for single EC2 (t4g.small) + SSM + Security Group
├── scripts/
│   ├── run_demo.py                 # End-to-end demo & benchmark harness
│   └── shopfront_demo.py           # ShopFront 6-scenario demo runner
├── sdk/                            # Optional Python SDK (client + ASGI middleware)
├── shopfront/
│   ├── api/                        # ShopFront FastAPI backend (products, orders, DB, fault injection)
│   │   └── shopfront/
│   │       ├── db.py               # SQLite/PostgreSQL ORM
│   │       ├── fault_routes.py     # Token-protected fault injection endpoints
│   │       ├── fault_state.py      # In-memory fault state
│   │       ├── main.py             # ShopFront FastAPI app
│   │       ├── supermetrics.py     # Prometheus metrics for ShopFront
│   │       └── telemetry.py        # OpenTelemetry setup for ShopFront
│   └── web/                        # ShopFront React frontend (Vite)
│       └── src/App.jsx             # React SPA
├── .env.example                    # Template for all environment variables (no real secrets)
├── ARCHITECTURE.md                 # Architecture documentation
├── APPLICATION_INTEGRATION.md      # Guide for integrating external applications
├── DEPLOYMENT.md                   # Docker Compose + AWS deployment guide
├── EVALUATION_REPORT.md            # Test counts, benchmark results, honest caveats
├── EVALUATION_RUNBOOK.md           # Manual live demo instructions
├── Dockerfile                      # Main application Docker image (Python 3.12, non-root)
├── docker-compose.yml              # Local full stack (AegisOps + demo + Prometheus + Alertmanager + Grafana)
├── docker-compose.aws.yml          # AWS stack variant
├── Makefile                        # Convenience targets (run, test, docker, etc.)
├── pyproject.toml                  # Project metadata + dependencies (uv/pip)
├── requirements.txt                # Minimal pinned requirements
└── README.md                       # Quick-start guide and repo map
```

> **Excluded from this tree:** `uv.lock`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.git/`, `data/demo_state_*/`, `data/sre_state/` (runtime data), `node_modules/`.

---

## Important Files and Responsibilities

| File | Responsibility |
|---|---|
| `app/sre/pipeline.py` | **Core orchestrator.** Defines the Google ADK `SequentialAgent` with 10 `StepAgent` steps, the `_before`/`_after` ADK callbacks, `SREOrchestrator`, and the two workflows (investigation + recovery). |
| `app/sre/mcp_server.py` | **Real MCP server.** Uses `FastMCP` to expose 6 live diagnostic tools. Provides `MCPToolbox` in-process client and stdio server for external clients. |
| `app/sre/a2a_specialists.py` | **A2A interoperability boundary.** 5 CrewAI specialist agents (SRE, Backend, Cloud/DevOps, Security, Database) each exposed as an A2A server with Agent Card + JSON-RPC endpoint. ADK is the A2A client. |
| `app/sre/guardrails.py` | **Security guardrails.** Prompt-injection detection and neutralisation, secret redaction, `RateLimiter`. |
| `app/sre/analysis.py` | **Deterministic engine.** Evidence scoring and RCA synthesis used when LLM is unavailable or as the baseline. |
| `app/sre/models.py` | **Typed state.** `IncidentState`, `RegisteredApplication`, `Evidence`, `RemediationOption`, `SpecialistFinding` — all Pydantic models. |
| `app/sre/store.py` | **Persistence.** Filesystem `StateStore` serialises `IncidentState` and `RegisteredApplication` to JSON after every step. |
| `app/sre/routes.py` | **API surface.** `/api/v1` FastAPI router: webhook, Alertmanager payload, approval, incident CRUD, application registration, agent card proxy. |
| `app/core/security.py` | **Auth/RBAC.** JWT creation/validation, `get_current_user`, `ensure_domain_access`, `ROLE_DOMAIN_ACCESS` mapping. |
| `app/core/telemetry.py` | **Prometheus metrics.** All `Counter`, `Histogram`, `Gauge` instruments for investigations, MCP calls, A2A handoffs, agent durations. |
| `app/sre/otel.py` | **OpenTelemetry.** `configure_tracing()`: sets up `TracerProvider`; exports via OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. |
| `app/services/adk_orchestrator.py` | **Legacy SOC ADK path.** `AegisOpsADKOrchestrator` using `LlmAgent`, `Runner`, `InMemorySessionService`; MCP and A2A as ADK tools. |
| `app/services/crew_investigation.py` | **Legacy SOC CrewAI crew.** `AegisPostmortemCrew` with 3 agents (triage analyst → evidence synthesizer → postmortem writer). |
| `app/services/mcp_tools.py` | **Local MCP registry.** `LocalMCPToolRegistry` with structured tool manifests and simulated dry-run execution. |
| `app/services/a2a.py` | **Local A2A registry.** `LocalA2ARegistry` for legacy SOC domain-based handoffs (not using a2a-sdk). |
| `app/models/schemas.py` | **Core schemas.** `IncidentInput`, `SecurityAlert`, `UserRole`, `MCPToolManifest`, `AgentManifest`, all enum types. |
| `app/models/adk_crew_schemas.py` | **ADK↔CrewAI bridge schemas.** `AegisIncidentBrief` (ADK output / CrewAI input), `CrewPostmortemReport`. |
| `docker-compose.yml` | **Full local stack.** 7 services: aegisops, demo-app, prometheus, alertmanager, grafana, selfcheck, shopfront-check. |
| `infra/aws/terraform/main.tf` | **AWS deployment.** Single EC2 t4g.small, Security Group, SSM parameters for all secrets, IAM role, user-data bootstrap. |
| `evals/reports/sre_benchmark.json` | **Benchmark evidence.** Machine-readable results for 9/9 scenarios with timing, confidence and phase data. |
| `app/tests/test_sre_pipeline.py` | **Integration tests.** 13 tests running full pipeline in-process via ASGI with real HTTP handling. |

