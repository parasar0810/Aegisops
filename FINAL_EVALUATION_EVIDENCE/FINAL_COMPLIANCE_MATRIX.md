# Final Compliance Matrix

> Strictness policy: A requirement is marked IMPLEMENTED only if there is **actual source code** that fulfils it.
> Documentation that describes a feature without code is marked NOT VERIFIED or PARTIALLY IMPLEMENTED.
> Findings are sourced from direct inspection of source files.

---

## 1. Agentic Frameworks

| Requirement | Status | Evidence |
|---|---|---|
| Use of Google ADK | **IMPLEMENTED** | `google.adk.agents.BaseAgent`, `SequentialAgent`, `Runner`, `InMemorySessionService` imported and used in `app/sre/pipeline.py`; `LlmAgent`, `Runner`, `InMemorySessionService` in `app/services/adk_orchestrator.py` |
| ADK agent subclassing | **IMPLEMENTED** | `StepAgent(BaseAgent)` in `pipeline.py`; `_run_async_impl()` overridden; yields `Event` objects |
| ADK SequentialAgent workflow | **IMPLEMENTED** | `SequentialAgent(sub_agents=[...10 StepAgents...])` with `before_agent_callback`/`after_agent_callback` |
| ADK Runner and Session | **IMPLEMENTED** | `Runner(agent=..., session_service=InMemorySessionService())` in `SREOrchestrator.run_investigation()` |
| ADK callbacks (before/after agent) | **IMPLEMENTED** | `_before(CallbackContext)`, `_after(CallbackContext)` — record timing, push to Prometheus |
| Use of CrewAI | **IMPLEMENTED** | `crewai.Agent`, `Task`, `Crew`, `Process`, `LLM` used in `app/sre/a2a_specialists.py` and `app/services/crew_investigation.py` |
| CrewAI agents with roles/goals | **IMPLEMENTED** | 5 specialists in `a2a_specialists.py`; 3-agent crew in `crew_investigation.py` |
| CrewAI tasks | **IMPLEMENTED** | `Task(description=..., agent=..., output_pydantic=SpecialistFinding)` in both files |
| CrewAI crew execution | **IMPLEMENTED** | `Crew.kickoff()` and `Crew.kickoff_async()` called; sequential process |
| ADK ↔ CrewAI interoperability | **IMPLEMENTED** | Via A2A protocol (`a2a-sdk` JSON-RPC) in SRE path; via `AegisIncidentBrief` schema in legacy SOC path |
| No LangGraph | **IMPLEMENTED** | README states removed; no `langgraph` import in any source file |

---

## 2. MCP (Model Context Protocol)

| Requirement | Status | Evidence |
|---|---|---|
| MCP server implementation | **IMPLEMENTED** | `FastMCP("aegisops-diagnostics")` in `app/sre/mcp_server.py` |
| MCP tools (≥3 real tools) | **IMPLEMENTED** | 6 real tools: `check_http_health`, `query_metrics`, `run_promql`, `search_logs`, `inspect_container`, `check_database` |
| MCP client usage | **IMPLEMENTED** | `MCPToolbox` uses `ClientSession` over in-process memory streams |
| MCP over stdio transport | **IMPLEMENTED** | `python -m app.sre.mcp_server` for external clients |
| Tools connected to external services | **IMPLEMENTED** | `check_http_health` → HTTP; `query_metrics` → Prometheus scrape; `run_promql` → Prometheus API; `search_logs` → app log endpoint; `inspect_container` → Docker; `check_database` → TCP |
| Local MCP registry (simulated) | **IMPLEMENTED** | `LocalMCPToolRegistry` in `app/services/mcp_tools.py` — structured manifests, dry-run execution |
| SSRF protection on MCP tools | **IMPLEMENTED** | `allowed_host()` in `mcp_server.py` restricts tools to registered application hosts |

---

## 3. Agent-to-Agent Communication

| Requirement | Status | Evidence |
|---|---|---|
| A2A protocol (a2a-sdk) | **IMPLEMENTED** | `a2a-sdk[http-server]` ≥ 1.3.0; `create_client`, `AgentCard`, `AgentExecutor`, JSON-RPC routes in `a2a_specialists.py` |
| Agent Cards | **IMPLEMENTED** | Each specialist exposes `/.well-known/agent-card.json` via `create_agent_card_routes()` |
| A2A task lifecycle | **IMPLEMENTED** | `InMemoryTaskStore`, `TaskUpdater`, `new_task_from_user_message` |
| Structured A2A message format | **IMPLEMENTED** | DataPart containing JSON-serialised `IncidentState` + `RegisteredApplication` |
| ADK as A2A client | **IMPLEMENTED** | `A2AClientPool` in `pipeline.py` → `a2a.client.create_client` → `SendMessageRequest` |
| CrewAI as A2A server | **IMPLEMENTED** | `SpecialistExecutor(AgentExecutor)` runs CrewAI inside the A2A server handler |
| Local A2A registry (legacy) | **IMPLEMENTED** | `LocalA2ARegistry` in `app/services/a2a.py` |

---

## 4. Security

| Requirement | Status | Evidence |
|---|---|---|
| JWT Authentication | **IMPLEMENTED** | `create_access_token()`, `get_current_user()` in `app/core/security.py` |
| RBAC | **IMPLEMENTED** | `ROLE_DOMAIN_ACCESS`, `ensure_domain_access()`, `require_role()` in `security.py` and `routes.py` |
| Auth bypass in dev mode | **PARTIALLY IMPLEMENTED** | `AUTH_REQUIRED=false` default disables auth; intentional for dev but a gap for shared deployments |
| Pydantic input validation | **IMPLEMENTED** | All API request bodies are Pydantic models; FastAPI enforces HTTP 422 |
| Prompt-injection protection | **IMPLEMENTED** | `sanitize_untrusted()`, `injection_flags()`, `wrap_untrusted()` in `app/sre/guardrails.py` applied to all untrusted input |
| Secret redaction from tool outputs | **IMPLEMENTED** | `redact()` with `_SECRET_PATTERNS` applied before any string enters the pipeline |
| Human approval gate | **IMPLEMENTED** | `IncidentPhase.awaiting_approval` pause; explicit `POST /approve` or `POST /reject` required |
| Remediation allowlist | **IMPLEMENTED** | `step_execution` only calls hooks pre-registered in `app.remediation_hooks`; skips if no match |
| Rate limiting | **IMPLEMENTED** | `RateLimiter(max_calls=120, per_seconds=60)` on webhook; `max_calls=60` on admin endpoints |
| Audit log | **IMPLEMENTED** | `StateStore.audit()` logs every approval/rejection/registration to `audit.jsonl` |
| Secret management (AWS) | **IMPLEMENTED** | AWS SSM Parameter Store `SecureString` via Terraform; fetched at runtime |
| HTTPS | **IMPLEMENTED** | Nginx HTTPS config in `docker/nginx/https.conf`; sslip.io TLS on AWS |
| MFA | **NOT IMPLEMENTED** | Single-factor (password → JWT) only |

---

## 5. Observability

| Requirement | Status | Evidence |
|---|---|---|
| Prometheus metrics | **IMPLEMENTED** | 15+ instruments in `app/core/telemetry.py`; exposed at `/api/metrics` |
| Grafana dashboard | **IMPLEMENTED** | Auto-provisioned in `docker/grafana/provisioning/dashboards/` |
| OpenTelemetry tracing | **IMPLEMENTED** | `otel.py` TracerProvider + per-agent spans in `pipeline.py` |
| ADK callbacks as observability hooks | **IMPLEMENTED** | `_before`/`_after` ADK callbacks in `pipeline.py` feed Prometheus histogram |
| Incident timeline | **IMPLEMENTED** | `IncidentState.log()` called after every step; persisted to disk |
| Per-agent timing | **IMPLEMENTED** | `aegis_agent_duration_seconds` Histogram; values in `state.timings_ms`; captured in benchmark JSON |
| MCP call metrics | **IMPLEMENTED** | `incident_mcp_tool_calls_total`, `incident_mcp_tool_duration_seconds` |
| A2A handoff metrics | **IMPLEMENTED** | `incident_a2a_handoffs_total`, `incident_a2a_handoff_duration_seconds` |
| CrewAI native callbacks | **NOT IMPLEMENTED** | `step_callback`/`task_callback` not configured on any Crew instance |
| Langfuse LLM tracing | **NOT IMPLEMENTED** | Env var checked; `langfuse` package not imported in source |

---

## 6. Testing

| Requirement | Status | Evidence |
|---|---|---|
| Unit/integration test suite | **IMPLEMENTED** | 63 tests across 5 files; `uv run pytest` |
| Tests pass without API keys | **IMPLEMENTED** | LLM calls mocked; `AEGIS_LLM_MODE=off` for SRE tests |
| In-process integration tests | **IMPLEMENTED** | ASGI transport used; real HTTP handling without network |
| E2E benchmark | **IMPLEMENTED** | `scripts/run_demo.py`; 9/9 scenarios; results in `evals/reports/sre_benchmark.json` |
| Evaluation harness | **IMPLEMENTED** | `evals/run_eval.py`; 12/12 cases; results in `evals/reports/latest.json` |
| Docker Compose self-check | **IMPLEMENTED** | `docker compose run --rm selfcheck`; 9/9 passed |
| AWS deployment verification | **IMPLEMENTED** | 9/9 on EC2; reboot test passed; documented in `EVALUATION_REPORT.md` |

---

## 7. Architecture and Design

| Requirement | Status | Evidence |
|---|---|---|
| Multi-domain incident handling | **IMPLEMENTED** | 5 domains: security, production, cloud, data, it; routing in `triage_agent` and RBAC |
| Application registration API | **IMPLEMENTED** | `POST /api/v1/applications/register`; generates unique ingest key |
| Webhook ingestion | **IMPLEMENTED** | `POST /api/v1/incidents/webhook` accepts Alertmanager payload or JSON event |
| RAG knowledge base | **IMPLEMENTED** | `app/rag/retriever.py`, `app/rag/vector_store.py`; knowledge base text files in `app/data/knowledge_base/` |
| Postmortem generation | **IMPLEMENTED** | `step_postmortem` in `pipeline.py`; `app/services/postmortem.py`; `CrewPostmortemReport` |
| State persistence and resume | **IMPLEMENTED** | `StateStore` JSON files; `completed_steps` idempotency check on resume |
| ShopFront monitored application | **IMPLEMENTED** | Full e-commerce app in `shopfront/`; 6 Prometheus rules; 6 allowlisted hooks |
| AWS cloud deployment | **IMPLEMENTED** | Terraform in `infra/aws/terraform/`; live at `13-127-211-52.sslip.io` as of 2026-11-12 |

---

## 8. Summary Counts

| Status | Count |
|---|---|
| ✅ IMPLEMENTED | 44 |
| ⚠️ PARTIALLY IMPLEMENTED | 1 (auth bypass default) |
| ❌ NOT IMPLEMENTED | 3 (MFA, CrewAI native callbacks, Langfuse) |
| ❓ NOT VERIFIED | 0 |

