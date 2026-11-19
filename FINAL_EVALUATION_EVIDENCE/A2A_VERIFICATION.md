# Agent-to-Agent (A2A) Communication Verification

## Two A2A Layers

The project implements A2A in two distinct ways:

1. **SRE Path — Real a2a-sdk** (`app/sre/a2a_specialists.py`) — uses the Google `a2a-sdk` with JSON-RPC, Agent Cards, tasks and artifacts.
2. **Legacy SOC Path — Local A2A Registry** (`app/services/a2a.py`) — a capability-based in-process registry that mirrors A2A concepts without using the SDK.

---

## 1. SRE Path — Real A2A Protocol (a2a-sdk)

### SDK Imports

**File:** `app/sre/a2a_specialists.py` (lines 1–45)

```python
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import (
    get_data_parts, new_data_message, new_data_part, new_task_from_user_message,
)
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill,
    Role, SendMessageRequest,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT
```

### Agent Cards

Each specialist exposes an **Agent Card** at `/.well-known/agent-card.json`:

```python
AgentCard(
    name=name,
    description=f"AegisOps {SPECIALISTS[name]['role']} specialist for incident analysis.",
    url=f"{base_url}/a2a/{name}/",
    version="1.0",
    capabilities=AgentCapabilities(
        streaming=False,
        push_notifications=False,
    ),
    skills=[
        AgentSkill(
            id="incident_analysis",
            name="Incident Root-Cause Analysis",
            description="Analyses incident evidence and returns structured findings.",
            input_modes=["application/json"],
            output_modes=["application/json"],
        )
    ],
    interfaces=[AgentInterface(type="jsonrpc", url=f"{base_url}/a2a/{name}/")],
    protocol_version=PROTOCOL_VERSION_CURRENT,
)
```

### Registered Specialists

| Agent Name | Role | Focus |
|---|---|---|
| `sre_agent` | Site Reliability Engineer | Availability, latency, CPU, memory, infrastructure saturation |
| `backend_agent` | Backend Engineer | API errors, application exceptions, request failures |
| `cloud_devops_agent` | Cloud/DevOps Engineer | Deployments, containers, networking, infrastructure changes |
| `security_agent` | Security Analyst | Suspicious activity, abnormal traffic, injection attempts |
| `database_agent` | Database Reliability Engineer | Connection exhaustion, query latency, database availability |

### Message Format

**Sender:** `SREOrchestrator` (via `A2AClientPool.delegate()`)
**Receiver:** `SpecialistExecutor` (via A2A JSON-RPC endpoint)

The message is a **DataPart** containing the serialised `IncidentState` and `RegisteredApplication`:

```python
# Sender (pipeline.py → a2a_specialists.py A2AClientPool)
data_part = new_data_part(data={
    "incident": state.model_dump(mode="json"),
    "application": app.model_dump(mode="json") if app else None,
})
message = new_data_message(parts=[data_part], role=Role.user)
task = new_task_from_user_message(message=message)
response = await client.send_message(SendMessageRequest(message=message))
```

```python
# Receiver (SpecialistExecutor.execute)
parts = get_data_parts(context.message.parts)
payload = MessageToDict(parts[0])
state = IncidentState(**payload["incident"])
app = RegisteredApplication(**payload["application"]) if payload.get("application") else None
```

### Handoff Flow

```
ADK: step_delegation (specialist_delegation_agent)
  │
  ├─ Identify specialists from triage (keyword matching)
  │
  ├─ For each specialist (concurrent via asyncio.gather):
  │   │
  │   ▼ A2AClientPool.delegate(name, state, app)
  │       │
  │       ├─ [inprocess mode] Direct ASGI call to Starlette app serving /a2a/<name>/
  │       │   (uses httpx.ASGITransport — no network hop)
  │       │
  │       └─ [cloud mode] Real HTTP POST to configured remote URL
  │
  │   SpecialistExecutor.execute():
  │       ├─ Deserialise DataPart → IncidentState + RegisteredApplication
  │       ├─ sanitize_untrusted(evidence)  ← guardrails applied
  │       ├─ [LLM enabled] CrewAI Agent + Task + Crew.kickoff()
  │       └─ [LLM off] analysis.score_evidence() (deterministic)
  │       └─ Validate output: SpecialistFinding (Pydantic)
  │       └─ Return result as Task artifact (DataPart)
  │
  └─ Parse artifact → SpecialistFinding list → st.findings
```

### Protocol

- **Transport:** HTTP JSON-RPC (a2a-sdk `create_jsonrpc_routes`)
- **In-process transport:** `httpx.ASGITransport` (Starlette ASGI — same process, no TCP socket)
- **Cloud transport:** Real HTTP to `A2A_<AGENT>_URL` environment variable
- **Task lifecycle:** `InMemoryTaskStore`, `TaskUpdater` (per a2a-sdk spec)
- **Artifact format:** DataPart containing JSON-serialised `SpecialistFinding`

### Prometheus Instrumentation

```python
A2A_MESSAGES_TOTAL.labels(
    source_agent="aegisops_orchestrator",
    target_agent=name,
    task_type="incident_analysis",
    status="ok",
).inc()
```

---

## 2. Legacy SOC Path — LocalA2ARegistry

**File:** `app/services/a2a.py`

### Nature

This is a **capability-based in-process registry** that mirrors A2A concepts (manifests, capabilities, handoff protocol). It does **not** use the `a2a-sdk` and makes no network calls.

```python
class LocalA2ARegistry:
    """Capability-based local agent-to-agent handoff registry."""

    def handoff(self, request: AgentHandoffRequest) -> AgentHandoffResponse:
        # Resolves target agent from registry
        # Returns synthetic evidence + actions
        # Records Prometheus metrics
```

### Message Format (Legacy)

```python
class AgentHandoffRequest(BaseModel):
    parent_message_id: str
    source_agent: str
    target_agent: str
    task_type: str
    incident: IncidentInput
    priority: str

class AgentHandoffResponse(BaseModel):
    parent_message_id: str
    source_agent: str
    target_agent: str
    domain: IncidentDomain
    status: str
    summary: str
    evidence: list[EvidenceItem]
    remediation_steps: list[RemediationStep]
    duration_ms: float
```

### Agents (Legacy SOC)

| Agent Name | Domain |
|---|---|
| `soc_agent` | security |
| `sre_agent` | production |
| `cloud_agent` | cloud |
| `data_agent` | data |
| `it_agent` | it |

### Cloud Endpoint Support

The legacy registry also supports optional cloud endpoints via `AGENT_<NAME>_URL` env vars. When set, the `handoff()` method makes a real HTTP POST using `httpx`.

---

## 3. Comparison

| Aspect | SRE A2A (a2a-sdk) | Legacy A2A (LocalRegistry) |
|---|---|---|
| Protocol | JSON-RPC (a2a-sdk spec) | Custom in-process |
| Agent Cards | Yes (`/.well-known/agent-card.json`) | No |
| Task lifecycle | `InMemoryTaskStore`, `TaskUpdater` | Synchronous method call |
| Message format | DataPart (protobuf JSON) | Pydantic models |
| Network | ASGI in-process or real HTTP | In-process or optional HTTP |
| CrewAI integration | Yes (inside `SpecialistExecutor`) | No |
| Used by | SRE pipeline | Legacy SOC /api endpoints |

