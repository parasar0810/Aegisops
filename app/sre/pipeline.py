"""Google ADK orchestration of the AegisOps SRE lifecycle.

Two ADK ``SequentialAgent`` workflows run under an ADK ``Runner``:

  investigation_workflow:
    incident_detection -> context_discovery -> triage -> evidence_collection
    -> specialist_delegation (A2A -> CrewAI specialists) -> evidence_synthesis
    -> remediation_planning                       => awaiting human approval
  recovery_workflow (after approval):
    remediation_execution -> recovery_verification -> postmortem

Each step is a custom ADK ``BaseAgent``. All steps share one typed
``IncidentState`` which is persisted after every step; completed steps are
skipped on re-run, so an interrupted investigation resumes after a restart.
ADK before/after-agent callbacks record per-agent duration (Prometheus,
OpenTelemetry span, incident timeline).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx
from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from opentelemetry import trace

from app.core.telemetry import (
    SRE_AGENT_DURATION,
    SRE_APPROVALS_TOTAL,
    SRE_INCIDENTS_TOTAL,
    SRE_INVESTIGATION_DURATION,
    SRE_RECOVERY_TOTAL,
)
from app.sre import analysis
from app.sre.a2a_specialists import A2AClientPool, llm_enabled, llm_timeout
from app.sre.guardrails import sanitize_untrusted
from app.sre.mcp_server import MCPToolbox
from app.sre.models import (
    AlertmanagerPayload,
    ApprovalRecord,
    Evidence,
    ExecutionResult,
    IncidentPhase,
    IncidentState,
    IncomingEvent,
    RecoveryVerification,
    RegisteredApplication,
    Severity,
)
from app.sre.store import StateStore

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("aegisops.sre")

_PRIORITY = {Severity.critical: "P1", Severity.high: "P2", Severity.medium: "P3", Severity.low: "P4"}
_KEYWORDS = {
    "database_agent": re.compile(r"database|db|postgres|mysql|connection pool|sql", re.IGNORECASE),
    "cloud_devops_agent": re.compile(r"deploy|container|pod|node|network|dns|release", re.IGNORECASE),
    "security_agent": re.compile(r"traffic|ddos|attack|suspicious|bot|auth|401|403|429", re.IGNORECASE),
}


@dataclass
class RunContext:
    state: IncidentState
    app: RegisteredApplication | None
    toolbox: MCPToolbox
    a2a: A2AClientPool
    store: StateStore
    approved_option: str | None = None
    step_started: dict[str, float] = field(default_factory=dict)


_RUNS: dict[str, RunContext] = {}


def _run(ctx: InvocationContext | CallbackContext) -> RunContext:
    state = ctx.session.state if isinstance(ctx, InvocationContext) else ctx.state
    return _RUNS[state["incident_id"]]


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


class StepAgent(BaseAgent):
    """A deterministic, tool-driven ADK agent for one lifecycle step."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        run = _run(ctx)
        st = run.state
        if self.name in st.completed_steps:
            summary = "already completed (resumed run)"
        else:
            with tracer.start_as_current_span(f"agent.{self.name}", attributes={"incident.id": st.incident_id}):
                try:
                    summary = await STEPS[self.name](run)
                except Exception as exc:
                    st.phase = IncidentPhase.failed
                    st.log(self.name, f"FAILED: {type(exc).__name__}: {exc}"[:300])
                    run.store.save_incident(st)
                    raise
            st.completed_steps.append(self.name)
            st.log(self.name, summary)
            run.store.save_incident(st)
        yield Event(invocation_id=ctx.invocation_id, author=self.name,
                    content=genai_types.Content(role="model", parts=[genai_types.Part(text=summary)]),
                    actions=EventActions(state_delta={f"aegis:{self.name}": "done", "aegis:phase": st.phase.value}))


# ── Step implementations ────────────────────────────────────────────────────


def _add(st: IncidentState, ev: Evidence) -> Evidence:
    st.evidence.append(ev)
    return ev


async def step_detection(run: RunContext) -> str:
    st = run.state
    st.priority = _PRIORITY[st.severity]
    st.phase = IncidentPhase.investigating
    safe, flags = sanitize_untrusted(f"{st.title}. {st.description}")
    _add(st, Evidence(source="alert", kind="alert", summary=safe,
                      data={"labels": st.initial_alert.get("labels", {}), "injection_flags": flags}))
    if metrics := st.initial_alert.get("metrics"):
        _add(st, Evidence(source="alert", kind="metric", summary="Metrics attached to the alert",
                          data={"values": metrics}))
    if logs := st.initial_alert.get("logs"):
        _add(st, Evidence(source="alert", kind="log", summary=f"{len(logs)} log lines attached to the alert",
                          data={"logs": [{"message": sanitize_untrusted(x, 400)[0]} for x in logs]}))
    SRE_INCIDENTS_TOTAL.labels(severity=st.severity.value, source=st.source).inc()
    return f"Incident {st.incident_id} opened: severity={st.severity.value} priority={st.priority}"


async def step_context(run: RunContext) -> str:
    st, app = run.state, run.app
    if app is None:
        st.application_context = {"registered": False}
        return f"Application '{st.application}' is not registered; investigating with alert data only"
    st.application_context = {
        "registered": True, "name": app.name, "description": app.description,
        "environment": app.environment, "tech_stack": app.tech_stack,
        "services": [s.model_dump() for s in app.services], "deployment": app.deployment,
        "infrastructure": app.infrastructure,
        "monitoring": {"prometheus": bool(app.monitoring.prometheus_url), "metrics_scrape": bool(app.monitoring.metrics_url),
                       "logs": bool(app.monitoring.logs_url), "container": app.monitoring.container_name,
                       "database": bool(app.monitoring.database_host)},
        "remediation_hooks": [{"name": h.name, "risk": h.risk.value, "addresses": [a.value for a in h.addresses]}
                              for h in app.remediation_hooks],
    }
    res = await run.toolbox.call("check_http_health", url=str(app.health_check_url))
    data = res.data if res.ok else {"error": res.error}
    code = data.get("status_code")
    app.last_health = {"status_code": code, "latency_ms": data.get("latency_ms"), "up": bool(code and code < 400)}
    run.store.save_application(app)
    _add(st, Evidence(source="check_http_health", kind="health",
                      summary=f"Health {app.health_check_url}: status={data.get('status_code')} "
                              f"latency={data.get('latency_ms')}ms", data=data))
    return f"Context loaded for {app.name} ({app.environment}, {len(app.services)} services); health={data.get('status_code')}"


async def step_triage(run: RunContext) -> str:
    st, app = run.state, run.app
    text = " ".join([st.title, st.description, " ".join(st.initial_alert.get("labels", {}).values())])
    specialists = ["sre_agent", "backend_agent"]
    arch = " ".join([*(s.kind for s in app.services), *app.tech_stack]) if app else ""
    for name, pat in _KEYWORDS.items():
        configured = app and (
            (name == "database_agent" and (app.monitoring.database_host or pat.search(arch)))
            or (name == "cloud_devops_agent" and (app.deployment or app.monitoring.container_name)))
        if pat.search(text) or configured:
            specialists.append(name)
    if "security_agent" not in specialists:
        specialists.append("security_agent")  # cheap; abnormal traffic is a frequent hidden cause
    unavailable = []
    if app:
        if not (app.monitoring.metrics_url or app.monitoring.prometheus_url):
            unavailable.append("metrics")
        if not app.monitoring.logs_url:
            unavailable.append("application log endpoint")
    st.triage = {"component": st.service or (app.services[0].name if app and app.services else st.application),
                 "specialists": specialists, "unavailable_tools": unavailable,
                 "brief": f"{st.severity.value.upper()} {st.title} on {st.application}"}
    return f"Triage: delegate to {', '.join(specialists)}"


async def step_evidence(run: RunContext) -> str:
    st, app, tb = run.state, run.app, run.toolbox
    if app is None:
        return "No registered telemetry; using alert evidence only"
    done = {e.source for e in st.evidence}
    calls: list[tuple[str, dict[str, Any], str]] = []
    if (app.monitoring.metrics_url or app.monitoring.prometheus_url) and "query_metrics" not in done:
        calls.append(("query_metrics", {"application": app.name}, "metric"))
    if "search_logs" not in done:
        calls.append(("search_logs", {"application": app.name, "since_minutes": app.monitoring.log_window_minutes,
                                      "limit": 100}, "log"))
    if "endpoint_probe" not in done:  # real user-facing latency/status, independent of metrics
        base = str(app.base_url).rstrip("/")
        for ep in [e for s in app.services for e in s.endpoints][:3]:
            calls.append(("check_http_health", {"url": base + "/" + ep.lstrip("/")}, "probe"))
    if app.monitoring.container_name and "inspect_container" not in done:
        calls.append(("inspect_container", {"application": app.name}, "container"))
    if app.monitoring.database_host and "check_database" not in done:
        calls.append(("check_database", {"application": app.name}, "database"))
    results = await asyncio.gather(*(tb.call(n, **a) for n, a, _ in calls))
    for (name, _, kind), res in zip(calls, results):
        data = res.data if res.ok else {"error": res.error}
        summary = {"metric": lambda d: f"Metrics: {d.get('values')}",
                   "log": lambda d: f"{d.get('count', 0)} log lines",
                   "probe": lambda d: f"Endpoint {d.get('url')}: status={d.get('status_code')} latency={d.get('latency_ms')}ms",
                   "container": lambda d: f"Container: {d.get('status', d.get('reason'))}",
                   "database": lambda d: f"Database reachable={d.get('reachable')}"}[kind](data)
        if kind == "probe":
            name, kind, data = "endpoint_probe", "health", {**data, "probe": True}
        _add(st, Evidence(source=name, kind=kind, summary=summary if res.ok else f"{name} failed: {res.error}",
                          data=data))
    return f"Collected {len(calls)} evidence sets via MCP: {', '.join(n for n, _, _ in calls)}"


async def step_delegation(run: RunContext) -> str:
    st = run.state
    names = st.triage.get("specialists", [])
    results = await asyncio.gather(*(run.a2a.delegate(n, st, run.app) for n in names), return_exceptions=True)
    st.findings = []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            st.a2a_log.append({"to": name, "error": f"{type(res).__name__}: {res}"[:300]})
            continue
        findings, log = res
        st.findings.extend(findings)
        st.a2a_log.append(log)
    return f"A2A: {len(names)} specialists returned {len(st.findings)} findings"


async def step_synthesis(run: RunContext) -> str:
    st = run.state
    past = [p for p in run.store.list_incidents() if p.incident_id != st.incident_id]
    st.rca = analysis.synthesize(st, past)
    p = st.rca.primary
    return f"Root cause: {p.category.value} (confidence {p.confidence:.2f}){' - INSUFFICIENT EVIDENCE' if st.rca.insufficient_evidence else ''}"


async def step_planning(run: RunContext) -> str:
    st = run.state
    past = [p for p in run.store.list_incidents() if p.incident_id != st.incident_id]
    st.remediation_options = analysis.plan_remediation(st.rca, run.app, past)
    actionable = [o for o in st.remediation_options if o.hook_name]
    st.phase = IncidentPhase.awaiting_approval if actionable else IncidentPhase.closed_no_action
    started = st.created_at.timestamp()
    SRE_INVESTIGATION_DURATION.observe(max(time.time() - started, 0))
    return f"{len(actionable)} remediation option(s) proposed; phase={st.phase.value}"


async def step_execution(run: RunContext) -> str:
    st, app = run.state, run.app
    option = next(o for o in st.remediation_options if o.option_id == run.approved_option)
    st.phase = IncidentPhase.remediating
    hook = next((h for h in (app.remediation_hooks if app else []) if h.name == option.hook_name), None)
    if hook is None:
        st.executions.append(ExecutionResult(option_id=option.option_id, hook_name=option.hook_name,
                                             status="skipped", detail="No allowlisted hook for this option."))
        return "Nothing executed (no allowlisted hook)"
    headers = {"X-AegisOps-Incident": st.incident_id}
    token = os.environ.get(f"AEGIS_HOOK_TOKEN_{app.name.upper().replace('-', '_')}")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        from app.sre import mcp_server  # shares the test transport hook
        async with httpx.AsyncClient(timeout=hook.timeout_seconds, transport=mcp_server.HTTP_TRANSPORT,
                                     follow_redirects=False) as c:
            r = await c.post(str(hook.url), headers=headers, json={"incident_id": st.incident_id})
        ok = 200 <= r.status_code < 300
        st.executions.append(ExecutionResult(option_id=option.option_id, hook_name=hook.name,
                                             status="succeeded" if ok else "failed", http_status=r.status_code,
                                             detail=sanitize_untrusted(r.text, 300)[0]))
    except httpx.HTTPError as exc:
        st.executions.append(ExecutionResult(option_id=option.option_id, hook_name=hook.name, status="failed",
                                             detail=type(exc).__name__))
    run.store.audit("aegisops", "remediation_executed", incident_id=st.incident_id, hook=hook.name,
                    result=st.executions[-1].status, http_status=st.executions[-1].http_status)
    return f"Executed hook {hook.name}: {st.executions[-1].status} (HTTP {st.executions[-1].http_status})"


async def step_verification(run: RunContext) -> str:
    st, app = run.state, run.app
    st.phase = IncidentPhase.verifying
    settle = app.monitoring.verify_settle_seconds if app and app.monitoring.verify_settle_seconds is not None else None
    await asyncio.sleep(settle if settle is not None else float(os.environ.get("AEGIS_VERIFY_SETTLE_SECONDS", "3")))
    before = st.metric_snapshot()
    checks, after, code = [], {}, None
    if app:
        h = await run.toolbox.call("check_http_health", url=str(app.health_check_url))
        code = h.data.get("status_code") if h.ok else None
        checks.append(f"health status {code}")
        app.last_health = {"status_code": code, "latency_ms": h.data.get("latency_ms"), "up": bool(code and code < 400)}
        run.store.save_application(app)
        if app.monitoring.metrics_url or app.monitoring.prometheus_url:
            m = await run.toolbox.call("query_metrics", application=app.name)
            after = m.data.get("values", {}) if m.ok else {}
    th = analysis._thresholds(app)
    breached_after = [k for k, v in after.items() if k in th and k != "seconds_since_deploy" and v > th[k]]
    for k in after:
        if k in before:
            checks.append(f"{k}: {before[k]:.3g} -> {after[k]:.3g}")
    exec_ok = bool(st.executions) and st.executions[-1].status == "succeeded"
    recovered = exec_ok and code is not None and code < 400 and not breached_after
    st.verification = RecoveryVerification(
        recovered=recovered, before=before, after=after, health_status_code=code, checks=checks,
        next_step="" if recovered else (
            f"Signals still breached: {breached_after}. " if breached_after else "")
        + ("Try the next-ranked remediation option or escalate to on-call." if not recovered else ""))
    st.phase = IncidentPhase.resolved if recovered else IncidentPhase.unresolved
    SRE_RECOVERY_TOTAL.labels(result="recovered" if recovered else "not_recovered").inc()
    return f"Recovery {'verified' if recovered else 'NOT verified'} (health={code}, breached={breached_after})"


async def step_postmortem(run: RunContext) -> str:
    st = run.state
    rca = st.rca
    approval = st.approvals[-1] if st.approvals else None
    pm: dict[str, Any] = {
        "incident_id": st.incident_id,
        "summary": f"{st.title} on {st.application} ({st.severity.value}, {st.priority}).",
        "severity": st.severity.value, "affected_services": [st.triage.get("component")],
        "timeline": [f"{t.at.isoformat()} [{t.agent}] {t.event}" for t in st.timeline],
        "root_cause": rca.primary.statement if rca else "unknown",
        "root_cause_confidence": rca.primary.confidence if rca else 0,
        "confidence_explanation": rca.confidence_explanation if rca else "",
        "supporting_evidence": [e.summary for e in st.evidence if rca and e.evidence_id in rca.primary.supporting_evidence],
        "alternatives": [f"{h.category.value} ({h.confidence:.2f})" for h in (rca.alternatives if rca else [])],
        "investigation": [f"{a.get('to')}: {a.get('findings', a.get('error'))} findings ({a.get('mode')})" for a in st.a2a_log],
        "remediation": [x.model_dump(mode="json") for x in st.executions],
        "approval": approval.model_dump(mode="json") if approval else None,
        "recovery_verification": st.verification.model_dump(mode="json") if st.verification else None,
        "lessons_learned": [], "preventive_recommendations": [], "generated_by": "deterministic",
    }
    cat = rca.primary.category.value if rca else "unknown"
    pm["lessons_learned"].append(f"{cat} was detected and attributed in "
                                 f"{sum(st.timings_ms.values()) / 1000:.1f}s of agent time.")
    if rca and rca.missing_evidence:
        pm["lessons_learned"].append("Telemetry gaps slowed attribution: " + "; ".join(rca.missing_evidence[:3]))
    pm["preventive_recommendations"] = {
        "database_failure": ["Alert on DB pool utilisation above 80%", "Add connection timeouts and pool leak detection"],
        "memory_exhaustion": ["Set container memory limits with alerting at 80%", "Profile allocations in the leaking path"],
        "cpu_saturation": ["Autoscale on CPU above 70%", "Profile hot code paths"],
        "bad_deployment": ["Gate deploys behind canary analysis", "Keep one-click rollback tested"],
        "abnormal_traffic": ["Add rate limiting / WAF rules at the edge"],
        "high_latency": ["Define latency SLOs with burn-rate alerts"],
        "http_errors": ["Add error-budget alerting and exception tracking"],
        "service_unavailable": ["Add redundancy and readiness probes"],
    }.get(cat, ["Improve telemetry coverage for this service"])
    if llm_enabled():
        try:
            narrative = await asyncio.wait_for(_crewai_postmortem(st), llm_timeout())
            if narrative.get("crew_demo_mode"):  # the crew swallowed an LLM failure; don't claim LLM output
                pm["crewai_error"] = "llm_unavailable"
            else:
                pm["crewai_narrative"] = narrative
                pm["generated_by"] = "deterministic+crewai"
        except Exception as exc:
            pm["crewai_error"] = f"{type(exc).__name__}"[:100]
    st.postmortem = pm
    return "Postmortem generated"


async def _crewai_postmortem(st: IncidentState) -> dict[str, Any]:
    """Reuse the existing 3-agent CrewAI postmortem crew."""
    from app.models.adk_crew_schemas import AegisIncidentBrief
    from app.models.schemas import AlertSeverity, IncidentDomain
    from app.services.crew_investigation import AegisPostmortemCrew

    brief = AegisIncidentBrief(
        incident_id=st.incident_id, title=st.title, domain=IncidentDomain.production,
        severity=AlertSeverity(st.severity.value), description=st.description,
        logs=[e.summary for e in st.evidence if e.kind == "log"][:5], metrics=st.metric_snapshot(),
        risk_score={"low": 20, "medium": 45, "high": 70, "critical": 90}[st.severity.value],
        mcp_tool_used="aegisops-diagnostics", mcp_evidence=[e.summary for e in st.evidence][:10],
        a2a_primary_agent="adk_orchestrator", a2a_peer_agent=",".join(st.triage.get("specialists", [])),
        a2a_findings=[f.observation for f in st.findings][:10],
        timeline=[t.event for t in st.timeline][:20], adk_mode="sequential_agent")
    report = await AegisPostmortemCrew().run_async(brief)
    return report.model_dump(mode="json")


STEPS = {
    "incident_detection_agent": step_detection,
    "context_discovery_agent": step_context,
    "triage_agent": step_triage,
    "evidence_collection_agent": step_evidence,
    "specialist_delegation_agent": step_delegation,
    "evidence_synthesis_agent": step_synthesis,
    "remediation_planning_agent": step_planning,
    "remediation_execution_agent": step_execution,
    "recovery_verification_agent": step_verification,
    "postmortem_agent": step_postmortem,
}
_DESCRIPTIONS = {
    "incident_detection_agent": "Receives alerts, assigns severity/priority, opens the incident.",
    "context_discovery_agent": "Loads the registered application's architecture and checks health.",
    "triage_agent": "Classifies the incident and selects specialist agents.",
    "evidence_collection_agent": "Collects metrics, logs, container and DB evidence through MCP tools.",
    "specialist_delegation_agent": "Delegates to CrewAI specialists over A2A and gathers findings.",
    "evidence_synthesis_agent": "Correlates findings into a root-cause analysis with confidence.",
    "remediation_planning_agent": "Ranks allowlisted remediation options by effectiveness and risk.",
    "remediation_execution_agent": "Executes only the human-approved, allowlisted remediation hook.",
    "recovery_verification_agent": "Re-checks health and metrics and decides whether recovery succeeded.",
    "postmortem_agent": "Writes the postmortem and stores incident knowledge.",
}
INVESTIGATION_STEPS = list(STEPS)[:7]
RECOVERY_STEPS = list(STEPS)[7:]


def _workflow(name: str, steps: list[str]) -> SequentialAgent:
    return SequentialAgent(name=name, sub_agents=[
        StepAgent(name=s, description=_DESCRIPTIONS[s], before_agent_callback=_before,
                  after_agent_callback=_after) for s in steps])


class SREOrchestrator:
    def __init__(self, store: StateStore, a2a_base_url: str,
                 a2a_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.a2a = A2AClientPool(a2a_base_url, a2a_transport)
        self.sessions = InMemorySessionService()
        self.runners = {
            "investigation": Runner(app_name="aegisops", agent=_workflow("investigation_workflow", INVESTIGATION_STEPS),
                                    session_service=self.sessions),
            "recovery": Runner(app_name="aegisops", agent=_workflow("recovery_workflow", RECOVERY_STEPS),
                               session_service=self.sessions),
        }
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def agent_catalog() -> list[dict[str, str]]:
        return [{"name": n, "framework": "google_adk", "description": d} for n, d in _DESCRIPTIONS.items()]

    # ── incident creation ──
    def incident_from_event(self, ev: IncomingEvent, source: str = "webhook") -> IncidentState:
        return IncidentState(application=ev.application, service=ev.service, title=ev.title,
                             description=ev.description, severity=ev.severity, source=source,
                             initial_alert=ev.model_dump(mode="json"))

    def incidents_from_alertmanager(self, payload: AlertmanagerPayload) -> list[IncidentState]:
        out = []
        for a in payload.alerts:
            if a.status != "firing":
                continue
            labels = {**payload.commonLabels, **a.labels}
            sev = labels.get("severity", "medium").lower()
            out.append(IncidentState(
                application=labels.get("application", labels.get("job", "unknown")),
                service=labels.get("service"), title=a.annotations.get("summary", labels.get("alertname", "Alert")),
                description=a.annotations.get("description", ""),
                severity=Severity(sev) if sev in Severity.__members__ else Severity.medium,
                source="prometheus_alertmanager", initial_alert={"labels": labels, "annotations": a.annotations}))
        return out

    def open_incident(self, state: IncidentState) -> IncidentState | None:
        """Persist a new incident, or correlate it into the app's already-open incident."""
        open_phases = {IncidentPhase.detected, IncidentPhase.investigating, IncidentPhase.awaiting_approval,
                       IncidentPhase.remediating, IncidentPhase.verifying}
        for existing in self.store.list_incidents(state.application):
            if existing.phase in open_phases:
                related = existing.initial_alert.setdefault("related_alerts", [])
                if len(related) < 50:
                    related.append({"title": state.title, "source": state.source, "at": state.created_at.isoformat()})
                existing.log("incident_detection_agent", f"Correlated alert '{state.title[:80]}' into open incident")
                self.store.save_incident(existing)
                return None
        self.store.save_incident(state)
        self.store.audit("aegisops", "incident_opened", incident_id=state.incident_id, application=state.application)
        return state

    # ── ADK execution ──
    async def _execute(self, workflow: str, state: IncidentState, approved_option: str | None = None) -> IncidentState:
        lock = self._locks.setdefault(state.incident_id, asyncio.Lock())
        async with lock:
            app = self.store.find_application(state.application)
            toolbox = MCPToolbox(self.store)
            async with toolbox.session():
                _RUNS[state.incident_id] = RunContext(state=state, app=app, toolbox=toolbox, a2a=self.a2a,
                                                      store=self.store, approved_option=approved_option)
                try:
                    session = await self.sessions.create_session(
                        app_name="aegisops", user_id="aegisops", state={"incident_id": state.incident_id})
                    msg = genai_types.Content(role="user", parts=[genai_types.Part(text=f"Run {workflow} for {state.incident_id}")])
                    async for _ in self.runners[workflow].run_async(user_id="aegisops", session_id=session.id,
                                                                    new_message=msg):
                        pass
                except Exception as exc:
                    logger.exception("workflow %s failed for %s", workflow, state.incident_id)
                    state.phase = IncidentPhase.failed
                    state.log("orchestrator", f"workflow {workflow} failed: {type(exc).__name__}: {exc}"[:300])
                finally:
                    _RUNS.pop(state.incident_id, None)
            self.store.save_incident(state)
            return state

    async def investigate(self, incident_id: str) -> IncidentState:
        state = self.store.get_incident(incident_id)
        if state is None:
            raise KeyError(incident_id)
        return await self._execute("investigation", state)

    async def decide(self, incident_id: str, option_id: str, approve: bool, user: str, reason: str = "") -> IncidentState:
        state = self.store.get_incident(incident_id)
        if state is None:
            raise KeyError(incident_id)
        if state.phase != IncidentPhase.awaiting_approval:
            raise ValueError(f"incident is {state.phase.value}, not awaiting_approval")
        option = next((o for o in state.remediation_options if o.option_id == option_id), None)
        if option is None or option.hook_name is None:
            raise ValueError("unknown or non-executable remediation option")
        state.approvals.append(ApprovalRecord(option_id=option_id, decision="approved" if approve else "rejected",
                                              decided_by=user, reason=reason))
        SRE_APPROVALS_TOTAL.labels(decision="approved" if approve else "rejected").inc()
        self.store.audit(user, "remediation_" + ("approved" if approve else "rejected"),
                         incident_id=incident_id, option_id=option_id, hook=option.hook_name, risk=option.risk.value)
        state.log("human_approval", f"{user} {'approved' if approve else 'rejected'} {option.hook_name}: {reason}"[:300])
        if not approve:
            remaining = [o for o in state.remediation_options if o.hook_name and o.option_id not in
                         {a.option_id for a in state.approvals}]
            if not remaining:
                state.phase = IncidentPhase.unresolved
            self.store.save_incident(state)
            return state
        for s in RECOVERY_STEPS:  # allow a second attempt after a failed recovery
            if s in state.completed_steps:
                state.completed_steps.remove(s)
        return await self._execute("recovery", state, approved_option=option_id)

    async def resume_pending(self) -> int:
        """Resume investigations interrupted by a restart."""
        pending = [i for i in self.store.list_incidents()
                   if i.phase in (IncidentPhase.detected, IncidentPhase.investigating)]
        for inc in pending:
            await self.investigate(inc.incident_id)
        return len(pending)
