# Callbacks and Observability Verification

## 1. ADK Before/After-Agent Callbacks

**File:** `app/sre/pipeline.py`

**Implemented.** These are genuine Google ADK callbacks registered on the `SequentialAgent` via `before_agent_callback` and `after_agent_callback` parameters.

```python
def _before(callback_context: CallbackContext) -> None:
    """Record start time for the about-to-run agent."""
    _run(callback_context).step_started[callback_context.agent_name] = time.perf_counter()


def _after(callback_context: CallbackContext) -> None:
    """Record elapsed time; push to Prometheus + incident timeline."""
    run = _run(callback_context)
    name = callback_context.agent_name
    started = run.step_started.pop(name, None)
    if started is not None:
        ms = (time.perf_counter() - started) * 1000
        SRE_AGENT_DURATION.labels(agent=name).observe(ms / 1000)
        run.state.timings_ms[name] = round(run.state.timings_ms.get(name, 0) + ms, 2)
```

These are wired via:

```python
investigation_workflow = SequentialAgent(
    name="investigation_workflow",
    sub_agents=[...],
    before_agent_callback=_before,
    after_agent_callback=_after,
)
```

The `CallbackContext` is the actual ADK callback type (`google.adk.agents.callback_context.CallbackContext`). The callback captures agent name, start/stop time, and updates both `SRE_AGENT_DURATION` (Prometheus) and `state.timings_ms` (incident timeline record).

**Benchmark Evidence:** `evals/reports/sre_benchmark.json` shows per-agent `agent_ms` values recorded by these callbacks:

```json
"agent_ms": {
  "incident_detection_agent": 1.96,
  "context_discovery_agent": 271.28,
  "triage_agent": 1.20,
  "evidence_collection_agent": 304.23,
  "specialist_delegation_agent": 128.76,
  "evidence_synthesis_agent": 16.15,
  "remediation_planning_agent": 16.44
}
```

---

## 2. Prometheus Metrics

**File:** `app/core/telemetry.py`

**Implemented.** The following Prometheus instruments are defined and actively updated:

### Investigation Metrics

| Metric | Type | Labels | Updated in |
|---|---|---|---|
| `security_investigations_total` | Counter | severity, verdict | Legacy SOC investigation |
| `security_investigation_duration_seconds` | Histogram | — | Legacy SOC investigation |
| `security_agent_duration_seconds` | Histogram | agent | Legacy SOC per-agent |

### SRE Pipeline Metrics

| Metric | Type | Labels | Updated in |
|---|---|---|---|
| `aegis_incidents_total` | Counter | severity, source | `step_detection` |
| `aegis_agent_duration_seconds` | Histogram | agent | ADK `_after` callback |
| `aegis_investigation_duration_seconds` | Histogram | — | `step_planning` |
| `aegis_recovery_total` | Counter | result | `step_verification` |
| `aegis_approvals_total` | Counter | decision | `approve` / `reject` route |
| `aegis_application_up` | Gauge | application | `step_context` health check |

### MCP Metrics

| Metric | Type | Labels | Updated in |
|---|---|---|---|
| `incident_mcp_tool_calls_total` | Counter | tool, mode, status | Every `MCPToolbox.call()` and `LocalMCPToolRegistry.execute()` |
| `incident_mcp_tool_duration_seconds` | Histogram | tool, mode | Same as above |
| `incident_mcp_tools_available` | Gauge | mode | Registry initialisation |

### A2A Metrics

| Metric | Type | Labels | Updated in |
|---|---|---|---|
| `incident_a2a_handoffs_total` | Counter | source_agent, target_agent, domain, status | Every A2A handoff |
| `incident_a2a_messages_total` | Counter | source_agent, target_agent, task_type, status | Every A2A message |
| `incident_a2a_handoff_duration_seconds` | Histogram | source_agent, target_agent, domain | Every A2A handoff |
| `incident_active_agent_capabilities` | Gauge | agent, domain | Registry init |

### Metrics Endpoint

```python
# app/main.py
@app.get("/api/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

Prometheus scrapes this at `http://aegisops:8000/api/metrics` as configured in `docker/prometheus.yml`.

---

## 3. Grafana Dashboard

**File:** `docker/grafana/provisioning/dashboards/`

**Implemented.** A Grafana dashboard (`AegisOps - Incident Operations`) is provisioned automatically via the `grafana/provisioning` volume mount. It queries the Prometheus datasource for all the above metrics.

Dashboard panels include: active incidents, investigation duration, agent duration breakdown, MCP tool call rate, A2A handoff rate, recovery outcomes, and application health.

---

## 4. OpenTelemetry Tracing

**File:** `app/sre/otel.py`

**Implemented.** `configure_tracing()` is called at startup and sets up an `opentelemetry` `TracerProvider`.

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "aegisops"}))
if endpoint := os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
```

**File:** `app/sre/pipeline.py`

```python
tracer = trace.get_tracer("aegisops.sre")

# Inside each StepAgent._run_async_impl():
with tracer.start_as_current_span(f"agent.{self.name}",
                                  attributes={"incident.id": st.incident_id}):
    summary = await STEPS[self.name](run)
```

Every ADK agent step runs inside an OTel span labelled with the agent name and incident ID. If `OTEL_EXPORTER_OTLP_ENDPOINT` is set, spans are exported to Jaeger/Tempo/Langfuse. Otherwise, they remain local.

---

## 5. Incident Timeline

**File:** `app/sre/models.py` — `IncidentState.log()`

**Implemented.** Every pipeline step records a timestamped entry to `IncidentState.timeline`:

```python
def log(self, source: str, message: str) -> None:
    self.timeline.append({
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "message": message,
    })
```

This is not an ADK callback in name, but is called explicitly from `StepAgent._run_async_impl()` after each step completes. It provides a persistent, structured log of every agent action within an incident.

---

## 6. Structured Logging

**File:** All modules use Python's `logging` module with `logger = logging.getLogger(__name__)`.

**Note:** This is **standard Python logging**, not an ADK/CrewAI callback. It is NOT counted here as a callback. It provides runtime diagnostics but is not part of the agentic observability system.

---

## 7. What Is NOT Present

| Capability | Status |
|---|---|
| ADK tool-call events/streaming | Not implemented. `StepAgent` does not use `LlmAgent` tool calls; it calls Python functions directly. ADK streaming events are not consumed. |
| Langfuse LLM tracing | Configured (`LANGFUSE_PUBLIC_KEY` env var checked) but not activated in the submitted code — no `langfuse` package import found. OTel spans export to Langfuse's OTLP endpoint when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. |
| CrewAI callback hooks | CrewAI's `step_callback` / `task_callback` are not configured in `AegisPostmortemCrew` or `SpecialistExecutor`. Duration of CrewAI runs is measured externally via `time.perf_counter()` in `A2AClientPool`. |
| Alertmanager notifications | Alertmanager sends webhooks **to** AegisOps; AegisOps does not send notifications **out** to Slack/PagerDuty in the current implementation. |

---

## 8. Summary

| Mechanism | Implemented | Evidence |
|---|---|---|
| ADK before/after-agent callbacks | ✅ Yes | `pipeline.py` `_before`/`_after`, wired to `SequentialAgent` |
| Per-agent timing (Prometheus) | ✅ Yes | `aegis_agent_duration_seconds` histogram, benchmark JSON |
| MCP tool call metrics | ✅ Yes | `incident_mcp_tool_calls_total`, `incident_mcp_tool_duration_seconds` |
| A2A handoff metrics | ✅ Yes | `incident_a2a_handoffs_total`, `incident_a2a_handoff_duration_seconds` |
| Grafana dashboard (provisioned) | ✅ Yes | `docker/grafana/provisioning/dashboards/` |
| OpenTelemetry spans | ✅ Yes | `otel.py`, `tracer.start_as_current_span()` per step |
| Incident timeline | ✅ Yes | `IncidentState.log()`, persisted to disk |
| CrewAI native callbacks | ❌ No | Not configured |
| Langfuse LLM tracing | ❌ No | Env var checked but package not imported |

