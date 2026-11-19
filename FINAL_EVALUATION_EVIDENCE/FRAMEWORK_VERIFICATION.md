# Framework Verification — Google ADK & CrewAI

## 1. Google ADK — Verified Implementation

### Package Import (SRE Pipeline — Primary Path)

**File:** `app/sre/pipeline.py` (lines 1–50)

```python
from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
```

These are **unconditional hard imports** — not lazy, not guarded. If `google-adk` is not installed, the SRE pipeline module fails to load.

### Package Import (Legacy SOC Path — Lazy)

**File:** `app/services/adk_orchestrator.py` (lines 44–52)

```python
try:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    _ADK_AVAILABLE = True
except Exception:
    LlmAgent = None
    Runner = None
    InMemorySessionService = None
    _ADK_AVAILABLE = False
```

The legacy SOC path uses lazy imports so the application continues if the package is absent.

### ADK Agents — `StepAgent` (Subclasses `BaseAgent`)

**File:** `app/sre/pipeline.py`

```python
class StepAgent(BaseAgent):
    """A deterministic, tool-driven ADK agent for one lifecycle step."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        run = _run(ctx)
        st = run.state
        if self.name in st.completed_steps:
            summary = "already completed (resumed run)"
        else:
            with tracer.start_as_current_span(f"agent.{self.name}", ...):
                summary = await STEPS[self.name](run)
            st.completed_steps.append(self.name)
            st.log(self.name, summary)
            run.store.save_incident(st)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=genai_types.Content(role="model", parts=[genai_types.Part(text=summary)]),
            actions=EventActions(state_delta={f"aegis:{self.name}": "done", "aegis:phase": st.phase.value})
        )
```

Each of the 10 lifecycle steps is a `StepAgent` instance that yields an `Event` and updates `state_delta`.

### ADK SequentialAgent + Runner

**File:** `app/sre/pipeline.py` — `SREOrchestrator.run_investigation()`

```python
investigation_workflow = SequentialAgent(
    name="investigation_workflow",
    sub_agents=[
        StepAgent(name="incident_detection_agent"),
        StepAgent(name="context_discovery_agent"),
        StepAgent(name="triage_agent"),
        StepAgent(name="evidence_collection_agent"),
        StepAgent(name="specialist_delegation_agent"),
        StepAgent(name="evidence_synthesis_agent"),
        StepAgent(name="remediation_planning_agent"),
    ],
    before_agent_callback=_before,
    after_agent_callback=_after,
)

runner = Runner(
    agent=investigation_workflow,
    app_name="aegisops",
    session_service=InMemorySessionService(),
)
session = await runner.session_service.create_session(app_name="aegisops", user_id=st.incident_id)
async for event in runner.run_async(user_id=st.incident_id, session_id=session.id, new_message=...):
    pass
```

### ADK Callbacks

**File:** `app/sre/pipeline.py` (lines 95–110)

```python
def _before(callback_context: CallbackContext) -> None:
    _run(callback_context).step_started[callback_context.agent_name] = time.perf_counter()

def _after(callback_context: CallbackContext) -> None:
    run = _run(callback_context)
    name = callback_context.agent_name
    started = run.step_started.pop(name, None)
    if started is not None:
        ms = (time.perf_counter() - started) * 1000
        SRE_AGENT_DURATION.labels(agent=name).observe(ms / 1000)
        run.state.timings_ms[name] = round(run.state.timings_ms.get(name, 0) + ms, 2)
```

`before_agent_callback` and `after_agent_callback` are **real ADK callback hooks** wired to the `SequentialAgent`. They measure per-agent wall time and feed into the Prometheus histogram `aegis_agent_duration_seconds`.

### Ten ADK Pipeline Steps

| Step Name | Function | Description |
|---|---|---|
| `incident_detection_agent` | `step_detection` | Opens incident, sets priority, sanitises alert for injection |
| `context_discovery_agent` | `step_context` | Loads app registration, calls `check_http_health` via MCP |
| `triage_agent` | `step_triage` | Keyword-classifies incident, selects relevant A2A specialists |
| `evidence_collection_agent` | `step_evidence` | Calls MCP tools: metrics, logs, endpoint probes, container, DB |
| `specialist_delegation_agent` | `step_delegation` | Dispatches A2A tasks to specialists; collects findings |
| `evidence_synthesis_agent` | `step_synthesis` | Runs deterministic/LLM RCA via `analysis.synthesize()` |
| `remediation_planning_agent` | `step_planning` | Proposes allowlisted remediation options |
| `remediation_execution_agent` | `step_execution` | Calls the approved hook URL |
| `recovery_verification_agent` | `step_verification` | Waits settle time; re-checks health/metrics |
| `postmortem_agent` | `step_postmortem` | Writes final postmortem, closes incident |

---

## 2. CrewAI — Verified Implementation

### Package Import (SRE A2A Specialists)

**File:** `app/sre/a2a_specialists.py`

```python
from crewai import LLM        # used in build_crewai_llm()
from crewai import Agent, Crew, Task, Process  # used in each specialist executor
```

Imports are **conditional** (guarded by `if llm_enabled()`), but the classes are real `crewai` classes, not stubs.

### Package Import (Legacy SOC Crew)

**File:** `app/services/crew_investigation.py` (lines 44–53)

```python
try:
    from crewai import Agent, Crew, LLM, Process, Task
    _CREWAI_AVAILABLE = True
except Exception:
    Agent = Crew = LLM = Process = Task = None
    _CREWAI_AVAILABLE = False
```

### Legacy SOC: 3-Agent Sequential Crew

**File:** `app/services/crew_investigation.py` — `AegisPostmortemCrew._build_crew()`

```python
triage_analyst = Agent(
    role="Incident Triage Analyst",
    goal="Classify the risk level and form a root-cause hypothesis for the incident.",
    backstory="...",
    llm=self._llm,
    verbose=False,
)
evidence_synthesizer = Agent(
    role="Evidence Synthesizer",
    goal="Review MCP + A2A findings; identify contributing factors.",
    backstory="...",
    llm=self._llm,
)
postmortem_writer = Agent(
    role="Postmortem Writer",
    goal="Write a structured postmortem report.",
    backstory="...",
    llm=self._llm,
)

triage_task = Task(description=..., agent=triage_analyst, expected_output=...)
synthesis_task = Task(description=..., agent=evidence_synthesizer, expected_output=...)
postmortem_task = Task(description=..., agent=postmortem_writer, expected_output=...)

crew = Crew(
    agents=[triage_analyst, evidence_synthesizer, postmortem_writer],
    tasks=[triage_task, synthesis_task, postmortem_task],
    process=Process.sequential,
    verbose=False,
)
output = crew.kickoff(inputs=inputs)
```

### SRE Path: 5 CrewAI Specialist Agents (via A2A)

**File:** `app/sre/a2a_specialists.py`

Each of the 5 specialists (`sre_agent`, `backend_agent`, `cloud_devops_agent`, `security_agent`, `database_agent`) is a Starlette app:

```python
class SpecialistExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # ... parse DataPart from A2A message ...
        if llm_enabled():
            llm = build_crewai_llm()
            agent = Agent(
                role=SPECIALISTS[self.name]["role"],
                goal=f"Investigate the incident focusing on {SPECIALISTS[self.name]['focus']}.",
                llm=llm,
            )
            task = Task(
                description=wrapped_prompt,
                agent=agent,
                output_pydantic=SpecialistFinding,
            )
            crew = Crew(agents=[agent], tasks=[task], process=Process.sequential)
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, crew.kickoff),
                timeout=llm_timeout(),
            )
        else:
            # Deterministic fallback via analysis.py
            result = analysis.score_evidence(state, app)
```

### LLM Selection (Priority Order)

```
GEMINI_API_KEY  →  LLM(model="gemini/<GEMINI_MODEL>")
OPENAI_API_KEY  →  LLM(model="openai/<OPENAI_MODEL>")
Ollama          →  LLM(model="ollama/llama3.1")
(none)          →  Deterministic analysis.py fallback
```

---

## 3. Other Agentic Frameworks

| Framework | Status |
|---|---|
| **LangGraph** | **Removed.** README explicitly states: *"LangGraph was removed."* No `langgraph` import exists in any source file. |
| **LangChain** | Not used. No `langchain` import in any source file. |
| **AutoGen** | Not used. |
| **OpenAI Agents** | Not used directly (OpenAI is used as an LLM backend via CrewAI). |

---

## 4. ADK ↔ CrewAI Workflow

### Primary SRE Pipeline (a2a-sdk based)

```
Alert / Alertmanager webhook
        │
        ▼
SREOrchestrator.run_investigation()
        │
        ▼ [Google ADK]
SequentialAgent (10 StepAgents)
        │
        ├─ step_context    → MCPToolbox.call("check_http_health")   [MCP over memory streams]
        ├─ step_evidence   → MCPToolbox.call("query_metrics", "search_logs", ...)
        │
        ├─ step_delegation → A2AClientPool.delegate(specialist_name, state, app)
        │                         │
        │                         ▼ [A2A protocol — a2a-sdk JSON-RPC]
        │                   SpecialistExecutor (CrewAI Agent + Task + Crew)
        │                         │
        │                         ▼ LLM or deterministic analysis
        │                   SpecialistFinding (Pydantic) → Task artifact → A2A response
        │
        ├─ step_synthesis   → analysis.synthesize(state, past_incidents)
        └─ step_planning    → analysis.plan_remediation(rca, app, past)
                │
                ▼  [human approval required]
        step_execution → HTTP POST to allowlisted hook URL
        step_verification → re-check health/metrics
        step_postmortem → write postmortem
```

### Legacy SOC Pipeline (LocalMCPToolRegistry / LocalA2ARegistry)

```
POST /api/adk/investigate  →  AegisOpsADKOrchestrator
        │
        ├─ ADK InMemorySessionService (always)
        │
        ├─ LLM mode:  LlmAgent + Runner → MCP & A2A as ADK tool definitions
        └─ Demo mode: direct LocalMCPToolRegistry.execute() + LocalA2ARegistry.handoff()
                │
                ▼
        AegisIncidentBrief (Pydantic, schema-validated)
                │
                ▼  POST /api/adk/crew_investigate
        AegisPostmortemCrew.run(brief)
                │
        CrewAI: 3 agents → sequential crew → crew.kickoff()
                │
                ▼
        CrewPostmortemReport (Pydantic)
```

**Key distinction:** In the SRE path, ADK and CrewAI communicate via the **a2a-sdk** (JSON-RPC, Agent Cards, Task artifacts). In the legacy SOC path, the handoff is a **direct Python method call** with a Pydantic schema as the bridge object (`AegisIncidentBrief`).

