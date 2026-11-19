from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.core.config import get_settings
from app.core.security import (
    AuthRequest,
    CurrentUser,
    TokenResponse,
    authenticate_demo_user,
    create_access_token,
    ensure_domain_access,
    get_current_user,
)
from app.models.schemas import (
    ActionDecisionRequest,
    ActionStatus,
    AgentHandoffRequest,
    AgentHandoffResponse,
    AgentExchangeRequest,
    AgentExchangeResponse,
    AgentManifest,
    HealthResponse,
    IncidentDomain,
    IngestDocumentRequest,
    IngestLogsRequest,
    IngestionResponse,
    IntegrationConfigRequest,
    IntegrationConfigResponse,
    IncidentInvestigationRequest,
    IncidentReport,
    InvestigationReport,
    InvestigationRequest,
    MCPToolCallRequest,
    MCPToolCallResponse,
    MCPToolManifest,
    PlatformMetricsSnapshot,
    PostmortemReport,
    RemediationAction,
    RemediationActionSummary,
    SecurityAlert,
    SessionMemoryResponse,
    StoredReportSummary,
)
from app.models.adk_crew_schemas import ADKCrewInvestigationResult
from app.services.a2a import LocalA2ARegistry
from app.services.adk_orchestrator import AegisOpsADKOrchestrator
from app.services.crew_investigation import AegisPostmortemCrew
from app.services.generic_incident_graph import GenericIncidentGraph
from app.services.ingestion import IngestionService
from app.services.integrations import IntegrationRegistry
from app.services.investigation_graph import InvestigationGraph
from app.services.mcp_tools import LocalMCPToolRegistry
from app.services.postmortem import PostmortemService
from app.services.remediation_service import RemediationService
from app.services.report_store import ReportStore
from app.services.session_memory import SessionMemory

router = APIRouter()
graph = InvestigationGraph()
generic_graph = GenericIncidentGraph()
memory = SessionMemory()
ingestion = IngestionService()
report_store = ReportStore()
integrations = IntegrationRegistry()
postmortems = PostmortemService()
mcp_tools = LocalMCPToolRegistry()
a2a_registry = LocalA2ARegistry()
remediations = RemediationService()
# ── Phase-2: Google ADK + CrewAI pipeline singletons ────────────────────────
adk_orchestrator = AegisOpsADKOrchestrator(mcp_service=mcp_tools, a2a_service=a2a_registry)
crew_postmortem = AegisPostmortemCrew()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", app=settings.app_name, env=settings.app_env)


@router.post("/auth/token", response_model=TokenResponse)
def token(request: AuthRequest) -> TokenResponse:
    if not authenticate_demo_user(request.username, request.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")
    return TokenResponse(access_token=create_access_token(request.username, request.role))


@router.post("/investigate", response_model=InvestigationReport)
async def investigate(
    request: InvestigationRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> InvestigationReport:
    ensure_domain_access(current_user, domain=IncidentDomain.security)
    if request.session_id:
        memory.append(request.session_id, "user", request.alert.model_dump_json())
    report = await graph.investigate_async(request.alert, session_id=request.session_id)
    if not request.include_references:
        report.references = []
        for finding in report.findings:
            finding.references = []
    if request.session_id:
        memory.append(request.session_id, "assistant", report.executive_summary)
    report.alert.tags = sorted(set(report.alert.tags + [f"analyst:{request.analyst_id or current_user.username}"]))
    action = remediations.create_action_for_report(report)
    report.remediation_action = action
    report.remediation_action_id = action.id
    await report_store.save_security_report_async(report, session_id=request.session_id)
    return report


@router.post("/incidents/investigate", response_model=IncidentReport)
async def investigate_incident(
    request: IncidentInvestigationRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> IncidentReport:
    ensure_domain_access(current_user, request.incident.domain)
    if request.session_id:
        memory.append(request.session_id, "user", request.incident.model_dump_json())
    report = await generic_graph.investigate_async(request.incident, session_id=request.session_id)
    if not request.include_references:
        report.references = []
        for finding in report.findings:
            finding.references = []
    if request.session_id:
        memory.append(request.session_id, "assistant", report.executive_summary)
    report.incident.tags = sorted(
        set(report.incident.tags + [f"analyst:{request.analyst_id or current_user.username}"])
    )
    action = remediations.create_action_for_report(report)
    report.remediation_action = action
    report.remediation_action_id = action.id
    await report_store.save_incident_report_async(report, session_id=request.session_id)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Google ADK + CrewAI investigation endpoint
# Framework chain: google.adk (LlmAgent / demo) -> crewai (Crew, 3 agents)
# ─────────────────────────────────────────────────────────────────────────────


class ADKInvestigationRequest(IncidentInvestigationRequest):
    """Same fields as IncidentInvestigationRequest; routed through ADK+CrewAI."""


@router.post("/adk/investigate", response_model=ADKCrewInvestigationResult)
async def adk_investigate(
    request: ADKInvestigationRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ADKCrewInvestigationResult:
    """
    Investigate an incident using the Google ADK -> CrewAI pipeline.

    Step 1 (Google ADK): LlmAgent calls MCP and A2A tools, produces AegisIncidentBrief.
    Step 2 (CrewAI):     3-agent Crew (Triage, Evidence, Postmortem) synthesises the brief
                         into a structured CrewPostmortemReport.

    Existing /investigate and
    /incidents/investigate endpoints are unchanged.
    """
    ensure_domain_access(current_user, request.incident.domain)

    # ── Step 1: Google ADK orchestration ────────────────────────────────────
    brief = await adk_orchestrator.investigate(request.incident)

    # ── Step 2: CrewAI 3-agent crew (native async via kickoff_async) ──────────
    postmortem = await crew_postmortem.run_async(brief)

    # ── Derive status from risk score ────────────────────────────────────────
    risk = brief.risk_score
    if risk >= 75:
        status_str = "major_incident"
    elif risk >= 50:
        status_str = "degraded"
    elif risk >= 25:
        status_str = "investigating"
    else:
        status_str = "informational"

    # ── Compose final validated result ───────────────────────────────────────
    result = ADKCrewInvestigationResult(
        investigation_id=brief.investigation_id,
        incident_id=brief.incident_id,
        title=brief.title,
        domain=brief.domain,
        status=status_str,
        risk_score=brief.risk_score,
        executive_summary=postmortem.postmortem_summary,
        timeline=brief.timeline,
        mcp_tools_used=[brief.mcp_tool_used],
        a2a_agents_consulted=[brief.a2a_primary_agent, brief.a2a_peer_agent],
        postmortem=postmortem,
        framework_chain=["google_adk", "crewai"],
        adk_demo_mode=brief.adk_demo_mode,
        crew_demo_mode=postmortem.crew_demo_mode,
    )

    # ── Optionally queue a remediation action (HITL approval required) ────────
    try:
        from app.models.schemas import IncidentReport, IncidentStatus, RemediationStep
        # Build a minimal IncidentReport shell for create_action_for_report
        shell = IncidentReport(
            investigation_id=brief.investigation_id,
            incident=request.incident,
            status=IncidentStatus(status_str) if status_str in IncidentStatus.__members__ else IncidentStatus.investigating,
            risk_score=brief.risk_score,
            executive_summary=postmortem.postmortem_summary,
            findings=[],
            evidence=[],
            timeline=brief.timeline,
            recommended_actions=[
                RemediationStep(
                    action=postmortem.remediation_recommendation,
                    rationale=postmortem.root_cause_hypothesis,
                    priority=1,
                )
            ],
            references=[],
        )
        action = remediations.create_action_for_report(shell)
        # Action ID is stored but not surfaced in the ADK result schema to
        # keep the response clean.  Retrieve via GET /api/remediations.
    except Exception:
        pass  # Remediation creation is best-effort for the ADK path

    return result


@router.post("/ingest/document", response_model=IngestionResponse)
async def ingest_document(
    request: IngestDocumentRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> IngestionResponse:
    ensure_domain_access(current_user, request.domain)
    return await ingestion.ingest_document_async(request)


@router.post("/ingest/logs", response_model=IngestionResponse)
async def ingest_logs(
    request: IngestLogsRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> IngestionResponse:
    ensure_domain_access(current_user, request.domain)
    return await ingestion.ingest_logs_async(request)


@router.get("/reports", response_model=list[StoredReportSummary])
def list_reports(current_user: CurrentUser = Depends(get_current_user)) -> list[StoredReportSummary]:
    summaries = []
    for summary in report_store.list_summaries():
        try:
            ensure_domain_access(current_user, summary.domain)
        except HTTPException:
            continue
        summaries.append(summary)
    return summaries


@router.get("/reports/{investigation_id}")
def get_report(investigation_id: str, current_user: CurrentUser = Depends(get_current_user)):
    report = report_store.get(investigation_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found.")
    ensure_domain_access(current_user, _report_domain(report))
    return report


@router.post("/reports/{investigation_id}/postmortem", response_model=PostmortemReport)
def generate_postmortem(
    investigation_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> PostmortemReport:
    report = report_store.get(investigation_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found.")
    ensure_domain_access(current_user, _report_domain(report))
    return postmortems.generate(report)


@router.post("/integrations", response_model=IntegrationConfigResponse)
def register_integration(
    request: IntegrationConfigRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> IntegrationConfigResponse:
    if current_user.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin can configure integrations.")
    return integrations.register(request)


@router.get("/integrations", response_model=list[IntegrationConfigResponse])
def list_integrations(_: CurrentUser = Depends(get_current_user)) -> list[IntegrationConfigResponse]:
    return integrations.list()


@router.get("/integrations/catalog")
def integration_catalog(_: CurrentUser = Depends(get_current_user)) -> list[dict[str, object]]:
    return integrations.catalog()


@router.get("/integrations/{integration_id}/health")
def integration_health(
    integration_id: str,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, object]:
    return integrations.health(integration_id)


@router.post("/integrations/{integration_id}/collect-preview")
def collect_integration_preview(
    integration_id: str,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, str]:
    return integrations.collect_preview(integration_id)


@router.get("/mcp/tools", response_model=list[MCPToolManifest])
def list_mcp_tools(_: CurrentUser = Depends(get_current_user)) -> list[MCPToolManifest]:
    return mcp_tools.list_tools()


@router.post("/mcp/tools/call", response_model=MCPToolCallResponse)
def call_mcp_tool(
    request: MCPToolCallRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> MCPToolCallResponse:
    domain = request.incident.domain if request.incident is not None else IncidentDomain.security
    ensure_domain_access(current_user, domain)
    return mcp_tools.execute(request)


@router.get("/a2a/agents", response_model=list[AgentManifest])
def list_a2a_agents(_: CurrentUser = Depends(get_current_user)) -> list[AgentManifest]:
    return a2a_registry.list_agents()


@router.post("/a2a/handoff", response_model=AgentHandoffResponse)
def a2a_handoff(
    request: AgentHandoffRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> AgentHandoffResponse:
    ensure_domain_access(current_user, request.incident.domain)
    return a2a_registry.handoff(request)


@router.post("/a2a/exchange", response_model=AgentExchangeResponse)
def a2a_exchange(
    request: AgentExchangeRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> AgentExchangeResponse:
    ensure_domain_access(current_user, request.incident.domain)
    return a2a_registry.exchange(request)


@router.post("/a2a/agents/{agent_name}/handoff", response_model=AgentHandoffResponse)
def a2a_agent_handoff(
    agent_name: str,
    request: AgentHandoffRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> AgentHandoffResponse:
    ensure_domain_access(current_user, request.incident.domain)
    request.target_agent = agent_name
    return a2a_registry.handoff(request)


@router.get("/platform/metrics-snapshot", response_model=PlatformMetricsSnapshot)
def platform_metrics_snapshot(_: CurrentUser = Depends(get_current_user)) -> PlatformMetricsSnapshot:
    mcp = mcp_tools.capability_metrics()
    a2a = a2a_registry.capability_metrics()
    return PlatformMetricsSnapshot(
        **mcp,
        **a2a,
        automated_response_steps=[
            "domain routing",
            "MCP tool discovery",
            "MCP evidence collection",
            "A2A task/result exchange",
            "RAG context retrieval",
            "risk scoring",
            "response recommendation",
            "report persistence",
            "postmortem generation",
        ],
    )


@router.get("/sessions/{session_id}/memory", response_model=SessionMemoryResponse)
def session_memory(
    session_id: str,
    _: CurrentUser = Depends(get_current_user),
) -> SessionMemoryResponse:
    return SessionMemoryResponse(session_id=session_id, messages=memory.get(session_id))


@router.delete("/sessions/{session_id}")
async def clear_session(
    session_id: str,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, bool | str]:
    memory.clear(session_id)
    return {"session_id": session_id, "memory_cleared": True}


@router.get("/sample-alert", response_model=SecurityAlert)
def sample_alert() -> SecurityAlert:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "sample_alerts" / "login_anomaly.json"
    return SecurityAlert.model_validate(json.loads(path.read_text(encoding="utf-8")))


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/remediations", response_model=list[RemediationActionSummary])
def list_remediations(
    investigation_id: str | None = None,
    status: ActionStatus | None = None,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[RemediationActionSummary]:
    actions = remediations.list_actions(investigation_id=investigation_id, status_filter=status)
    allowed = []
    for action in actions:
        try:
            ensure_domain_access(current_user, action.domain)
            allowed.append(action)
        except HTTPException:
            continue
    return allowed


@router.get("/remediations/{action_id}", response_model=RemediationAction)
def get_remediation(
    action_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> RemediationAction:
    action = remediations.get_action(action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Remediation action '{action_id}' not found.",
        )
    ensure_domain_access(current_user, action.domain)
    return action


@router.post("/remediations/{action_id}/approve", response_model=RemediationAction)
def approve_remediation(
    action_id: str,
    request: ActionDecisionRequest = ActionDecisionRequest(),
    current_user: CurrentUser = Depends(get_current_user),
) -> RemediationAction:
    action = remediations.get_action(action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Remediation action '{action_id}' not found.",
        )
    ensure_domain_access(current_user, action.domain)
    return remediations.approve_action(
        action_id=action_id,
        decided_by=current_user.username,
        comment=request.comment,
        simulate_outcome=request.simulate_outcome,
    )


@router.post("/remediations/{action_id}/reject", response_model=RemediationAction)
def reject_remediation(
    action_id: str,
    request: ActionDecisionRequest = ActionDecisionRequest(),
    current_user: CurrentUser = Depends(get_current_user),
) -> RemediationAction:
    action = remediations.get_action(action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Remediation action '{action_id}' not found.",
        )
    ensure_domain_access(current_user, action.domain)
    return remediations.reject_action(
        action_id=action_id,
        decided_by=current_user.username,
        comment=request.comment,
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>AegisOps — Multi-Agent Incident Response Platform</title>
          <style>
            :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
            body { margin: 0; background: #eef2f7; color: #172033; }
            header { display: flex; justify-content: space-between; gap: 18px; align-items: center; padding: 20px 28px; background: #0f172a; color: white; border-bottom: 2px solid #3b82f6; }
            header h1 { margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.02em; }
            header p { margin: 4px 0 0; color: #94a3b8; font-size: 13px; }
            main { padding: 20px 28px 32px; }
            .shell { display: grid; grid-template-columns: 320px 1fr; gap: 18px; }
            .panel { background: white; border: 1px solid #d9e1ec; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
            .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
            .tab { width: auto; padding: 9px 14px; background: #e2e8f0; color: #1e293b; border: 1px solid #cbd5e1; border-radius: 6px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
            .tab.active { background: #2563eb; color: white; border-color: #2563eb; }
            .view { display: none; }
            .view.active { display: block; }
            label { display: block; font-size: 12px; font-weight: 750; margin-top: 10px; color: #334155; }
            input, select, textarea, button { width: 100%; box-sizing: border-box; margin-top: 6px; padding: 10px 12px; border-radius: 6px; border: 1px solid #b9c5d5; font: inherit; }
            textarea { min-height: 110px; resize: vertical; }
            button { background: #2563eb; color: white; border: 0; font-weight: 750; cursor: pointer; border-radius: 6px; }
            button:hover { opacity: 0.95; }
            button.secondary { background: #475569; }
            button.ghost { background: white; color: #1e293b; border: 1px solid #cbd5e1; }
            button.approve { background: #16a34a; font-weight: 700; }
            button.approve:hover { background: #15803d; }
            button.reject { background: #dc2626; font-weight: 700; }
            button.reject:hover { background: #b91c1c; }
            pre { white-space: pre-wrap; background: #0f172a; color: #dbeafe; border-radius: 8px; padding: 14px; overflow: auto; min-height: 200px; font-size: 12px; }
            .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
            .two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
            .metric { border: 1px solid #d9e1ec; border-radius: 8px; padding: 12px; background: #f8fafc; }
            .metric span { display: block; font-size: 12px; color: #64748b; }
            .metric strong { display: block; font-size: 22px; margin-top: 4px; }
            .list { display: grid; gap: 8px; margin-top: 10px; max-height: 520px; overflow-y: auto; }
            .item { border: 1px solid #d9e1ec; border-radius: 8px; padding: 12px; background: #fbfdff; cursor: pointer; transition: all 0.15s; }
            .item:hover { border-color: #93c5fd; background: #eff6ff; }
            .item strong { display: block; font-size: 14px; }
            .item small { color: #64748b; font-size: 12px; }
            .badge { display: inline-block; padding: 3px 8px; border-radius: 9999px; font-size: 11px; font-weight: 750; text-transform: uppercase; margin-right: 4px; }
            .badge-pending_approval { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
            .badge-approved { background: #dcfce7; color: #166534; border: 1px solid #86efac; }
            .badge-rejected { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
            .badge-completed { background: #e0f2fe; color: #075985; border: 1px solid #7dd3fc; }
            .badge-failed { background: #fee2e2; color: #991b1b; }
            .badge-verified { background: #dcfce7; color: #166534; border: 1px solid #86efac; }
            .badge-inconclusive { background: #fef3c7; color: #92400e; border: 1px solid #fde047; }
            .badge-critical { background: #fee2e2; color: #991b1b; }
            .badge-high { background: #ffedd5; color: #9a3412; }
            .badge-medium { background: #fef9c3; color: #854d0e; }
            .badge-low { background: #f1f5f9; color: #334155; }
            .metric-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }
            .metric-table th, .metric-table td { border: 1px solid #cbd5e1; padding: 8px 10px; text-align: left; }
            .metric-table th { background: #f1f5f9; font-weight: 700; color: #334155; }
            .sim-notice { background: #eff6ff; border-left: 4px solid #3b82f6; padding: 12px 14px; margin-bottom: 14px; border-radius: 4px; font-size: 13px; color: #1e3a8a; }
            .action-card { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px; margin-top: 12px; }
            @media (max-width: 980px) { header { display: block; } .shell, .two { grid-template-columns: 1fr; } .grid { grid-template-columns: repeat(2, 1fr); } main { padding: 16px; } }
          </style>
        </head>
        <body>
          <header>
            <div>
              <h1>🛡️ AegisOps — Multi-Agent Incident Response Platform</h1>
              <p>Autonomous multi-agent investigation, RAG runbooks, human-in-the-loop remediation, and verified recovery.</p>
            </div>
            <div style="min-width: 280px;">
              <input id="token" placeholder="Bearer token" />
              <button onclick="demoAuth()">Demo Admin Auth</button>
            </div>
          </header>
          <main>
            <div class="tabs">
              <button class="tab active" onclick="showView('investigate', this)">Investigate</button>
              <button class="tab" onclick="showView('remediations', this); loadRemediations()">🛡️ Remediations (HITL)</button>
              <button class="tab" onclick="showView('reports', this); loadReports()">Reports</button>
              <button class="tab" onclick="showView('ingest', this)">Ingest</button>
              <button class="tab" onclick="showView('integrations', this); loadCatalog(); loadIntegrations()">Integrations</button>
              <button class="tab" onclick="showView('platformMetrics', this); loadPlatformMetrics()">Metrics</button>
            </div>
            <div class="shell">
              <section class="panel">
                <div class="grid">
                  <div class="metric"><span>Status</span><strong id="status">-</strong></div>
                  <div class="metric"><span>Risk</span><strong id="risk">-</strong></div>
                  <div class="metric"><span>Actions</span><strong id="actions">-</strong></div>
                  <div class="metric"><span>Refs</span><strong id="refs">-</strong></div>
                </div>
                <div id="timeline" class="list"></div>
              </section>
              <section class="panel">
                <div id="investigate" class="view active">
                  <h2>Submit Incident for Multi-Agent Investigation</h2>
                  <div class="two">
                    <div>
                      <label>Domain</label>
                      <select id="domain"><option>production</option><option>security</option><option>cloud</option><option>data</option><option>it</option></select>
                    </div>
                    <div>
                      <label>Severity</label>
                      <select id="severity"><option>high</option><option>critical</option><option>medium</option><option>low</option></select>
                    </div>
                  </div>
                  <label>Title</label>
                  <input id="title" value="Checkout API returning elevated 503 errors" />
                  <label>Description / Logs</label>
                  <textarea id="description">checkout-api ERROR upstream payment-gateway timeout
checkout-api failed request status=503 route=/checkout
upstream database connection pool exhausted max=50 active=50</textarea>
                  <button onclick="investigate()">Start Investigation</button>
                  <div id="investigateBanner" style="display:none; margin-top:12px;" class="sim-notice"></div>
                </div>
                <div id="remediations" class="view">
                  <h2>🛡️ Remediation Actions & Human Approval Queue</h2>
                  <div class="sim-notice">
                    <strong>DEMO / SAFE SIMULATED MODE:</strong> Remediation actions require human authorization before execution. Approved actions execute a deterministic simulation handler that computes pre- and post-action measurements against documented thresholds to verify recovery.
                  </div>
                  <button class="ghost" onclick="loadRemediations()">Refresh Action Queue</button>
                  <div class="two" style="margin-top: 14px;">
                    <div>
                      <h3>Action Queue</h3>
                      <div id="remediationList" class="list"><small>No actions yet. Investigate an incident to create one.</small></div>
                    </div>
                    <div id="remediationDetail" class="panel" style="border: 1px solid #cbd5e1; min-height: 400px;">
                      <p style="color: #64748b; font-size: 13px;">Select an action from the queue to review and decide.</p>
                    </div>
                  </div>
                </div>
                <div id="reports" class="view">
                  <h2>Reports</h2>
                  <button class="ghost" onclick="loadReports()">Refresh Reports</button>
                  <div id="reportList" class="list"></div>
                </div>
                <div id="ingest" class="view">
                  <h2>Ingest Runbook or Logs</h2>
                  <label>Title</label>
                  <input id="ingestTitle" value="Checkout rollback runbook" />
                  <label>Domain</label>
                  <select id="ingestDomain"><option>production</option><option>security</option><option>cloud</option><option>data</option><option>it</option></select>
                  <label>Source</label>
                  <input id="ingestSource" value="dashboard-upload" />
                  <label>Content</label>
                  <textarea id="ingestContent">If checkout 503 errors increase after deployment, inspect payment dependencies, compare recent deploys, and rollback if customer impact continues.</textarea>
                  <button onclick="ingestDocument()">Ingest Document</button>
                </div>
                <div id="integrations" class="view">
                  <h2>Integrations</h2>
                  <div class="two">
                    <div>
                      <label>Type</label>
                      <select id="integrationType"></select>
                    </div>
                    <div>
                      <label>Domain</label>
                      <select id="integrationDomain"><option>security</option><option>production</option><option>cloud</option><option>data</option><option>it</option></select>
                    </div>
                  </div>
                  <label>Name</label>
                  <input id="integrationName" value="Primary evidence source" />
                  <label>Base URL</label>
                  <input id="integrationUrl" value="https://example.local" />
                  <label>Metadata JSON</label>
                  <textarea id="integrationMetadata">{"channel":"#incidents","project_key":"INC","region":"us-east-1","site":"us5","index":"main","sourcetype":"json"}</textarea>
                  <button onclick="registerIntegration()">Register Dry-Run Connector</button>
                  <div id="integrationList" class="list"></div>
                </div>
                <div id="platformMetrics" class="view">
                  <h2>MCP + A2A Metrics</h2>
                  <button class="ghost" onclick="loadPlatformMetrics()">Refresh Metrics</button>
                  <div id="platformMetricList" class="list"></div>
                </div>
                <pre id="output">No report yet.</pre>
                <div class="two">
                  <button class="secondary" onclick="copyActions()">Copy Actions</button>
                  <button class="secondary" onclick="generatePostmortem()">Generate Postmortem</button>
                </div>
              </section>
            </div>
          </main>
          <script>
            let lastReport = null;
            let currentAction = null;
            function authHeaders() { return {'Content-Type':'application/json', Authorization:'Bearer '+document.getElementById('token').value}; }
            function showView(id, button) {
              document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
              document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
              document.getElementById(id).classList.add('active');
              if (button) button.classList.add('active');
            }
            function renderOutput(value) { document.getElementById('output').textContent = JSON.stringify(value, null, 2); }
            function statusBorderColor(s) {
              return {pending_approval:'#f59e0b',approved:'#3b82f6',executing:'#8b5cf6',completed:'#16a34a',rejected:'#dc2626',failed:'#ef4444'}[s] || '#cbd5e1';
            }
            function renderSummary(report) {
              lastReport = report;
              document.getElementById('status').textContent = report.status || report.verdict || '-';
              document.getElementById('risk').textContent = String(report.risk_score || '-');
              document.getElementById('actions').textContent = String((report.recommended_actions || []).length);
              document.getElementById('refs').textContent = String((report.references || []).length);
              document.getElementById('timeline').innerHTML = (report.timeline || []).slice(-6).map(item => `<div class="item"><small>${item}</small></div>`).join('');
              if (report.remediation_action) {
                const banner = document.getElementById('investigateBanner');
                banner.style.display = 'block';
                banner.innerHTML = `<strong>⚡ Remediation Action Generated:</strong> "${report.remediation_action.proposed_action}" <br><button style="margin-top:8px; width:auto; padding:6px 12px;" onclick="goToRemediation('${report.remediation_action.id}')">Review & Decide in Remediation Tab →</button>`;
              }
              renderOutput(report);
            }
            async function demoAuth() {
              const res = await fetch('/api/auth/token', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:'analyst', password:'analyst', role:'admin'})});
              const body = await res.json();
              document.getElementById('token').value = body.access_token;
            }
            async function investigate() {
              const domain = document.getElementById('domain').value;
              const title = document.getElementById('title').value;
              const description = document.getElementById('description').value;
              const severity = document.getElementById('severity').value;
              const payload = {incident:{title, domain, severity, source:'dashboard', service:'dashboard-input', description, logs:description.split('\\n'), tags:['dashboard']}};
              const res = await fetch('/api/incidents/investigate', {method:'POST', headers:authHeaders(), body: JSON.stringify(payload)});
              renderSummary(await res.json());
            }
            async function loadReports() {
              const res = await fetch('/api/reports', {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              const reports = await res.json();
              document.getElementById('reportList').innerHTML = reports.map(report => `<div class="item" onclick="loadReport('${report.investigation_id}')"><strong>${report.title}</strong><small>${report.domain} · ${report.status} · risk ${report.risk_score}</small></div>`).join('');
              renderOutput(reports);
            }
            async function loadReport(id) {
              const res = await fetch('/api/reports/'+id, {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              renderSummary(await res.json());
            }
            async function ingestDocument() {
              const payload = {title:document.getElementById('ingestTitle').value, domain:document.getElementById('ingestDomain').value, source:document.getElementById('ingestSource').value, content:document.getElementById('ingestContent').value, tags:['dashboard']};
              const res = await fetch('/api/ingest/document', {method:'POST', headers:authHeaders(), body: JSON.stringify(payload)});
              renderOutput(await res.json());
            }
            async function loadCatalog() {
              const res = await fetch('/api/integrations/catalog', {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              const catalog = await res.json();
              document.getElementById('integrationType').innerHTML = catalog.map(item => `<option value="${item.integration_type}">${item.integration_type} · ${item.category}</option>`).join('');
            }
            async function loadIntegrations() {
              const res = await fetch('/api/integrations', {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              const items = await res.json();
              document.getElementById('integrationList').innerHTML = items.map(item => `<div class="item" onclick="selectIntegration('${item.id}')"><strong>${item.name}</strong><small>${item.integration_type} · ${item.status}</small></div>`).join('');
            }
            async function registerIntegration() {
              const payload = {integration_type:document.getElementById('integrationType').value, name:document.getElementById('integrationName').value, base_url:document.getElementById('integrationUrl').value, default_domain:document.getElementById('integrationDomain').value, metadata:JSON.parse(document.getElementById('integrationMetadata').value)};
              const res = await fetch('/api/integrations', {method:'POST', headers:authHeaders(), body: JSON.stringify(payload)});
              renderOutput(await res.json());
              await loadIntegrations();
            }
            async function selectIntegration(id) {
              const health = await fetch('/api/integrations/'+id+'/health', {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              const preview = await fetch('/api/integrations/'+id+'/collect-preview', {method:'POST', headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              renderOutput({health: await health.json(), preview: await preview.json()});
            }
            async function loadPlatformMetrics() {
              const headers = {Authorization:'Bearer '+document.getElementById('token').value};
              const snapshot = await fetch('/api/platform/metrics-snapshot', {headers});
              const tools = await fetch('/api/mcp/tools', {headers});
              const agents = await fetch('/api/a2a/agents', {headers});
              const body = {snapshot: await snapshot.json(), mcp_tools: await tools.json(), a2a_agents: await agents.json()};
              const metrics = body.snapshot;
              document.getElementById('platformMetricList').innerHTML = [
                ['MCP tools', metrics.mcp_tools_available],
                ['A2A agents', metrics.a2a_agents_available],
                ['A2A provider', metrics.a2a_provider],
                ['Cloud-ready A2A agents', metrics.cloud_ready_a2a_agents],
                ['A2A messages/investigation', metrics.a2a_messages_per_investigation],
                ['Capabilities', metrics.advertised_capabilities],
                ['Integration-ready tools', metrics.integration_ready_tools],
                ['Workflows', metrics.operational_workflows],
                ['Automated steps', metrics.automated_response_steps.length]
              ].map(item => `<div class="item"><strong>${item[1]}</strong><small>${item[0]}</small></div>`).join('');
              renderOutput(body);
            }
            async function generatePostmortem() {
              if (!lastReport?.investigation_id) return;
              const res = await fetch('/api/reports/'+lastReport.investigation_id+'/postmortem', {method:'POST', headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              renderOutput(await res.json());
            }
            async function copyActions() {
              const text = (lastReport?.recommended_actions || []).map(a => `${a.priority}. ${a.action}`).join('\\n');
              await navigator.clipboard.writeText(text);
            }

            // Remediation & Recovery (HITL) Workflow
            async function loadRemediations() {
              const res = await fetch('/api/remediations', {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              const items = await res.json();
              if (!Array.isArray(items) || items.length === 0) {
                document.getElementById('remediationList').innerHTML = '<small>No actions found. Submit an incident to generate one.</small>';
                renderOutput(items);
                return;
              }
              document.getElementById('remediationList').innerHTML = items.map(a => `
                <div class="item" onclick="loadRemediationDetail('${a.id}')" style="border-left:4px solid ${statusBorderColor(a.status)};">
                  <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:4px;">
                    <span class="badge badge-${a.risk_level}">${a.risk_level}</span>
                    <span class="badge badge-${a.status}">${a.status}</span>
                  </div>
                  <strong style="margin-top:6px; display:block; font-size:13px;">${a.title}</strong>
                  <small style="display:block; margin-top:4px; color:#475569;">${a.proposed_action.slice(0, 80)}${a.proposed_action.length > 80 ? '\u2026' : ''}</small>
                  <div style="margin-top:7px; display:grid; grid-template-columns:1fr 1fr; row-gap:2px; column-gap:6px; font-size:10.5px; color:#64748b; font-family:ui-monospace,monospace; background:#f8fafc; border:1px solid #e2e8f0; border-radius:5px; padding:6px 8px;">
                    <span title="Action ID: ${a.id}">&#128273; <b>Act</b> ${a.id.slice(0,8)}</span>
                    <span title="Investigation ID: ${a.investigation_id}">&#128300; <b>Inv</b> ${a.investigation_id.slice(0,8)}</span>
                    <span title="Incident ID: ${a.incident_id}">&#128203; <b>Inc</b> ${a.incident_id.slice(0,8)}</span>
                    <span title="Domain">&#127991;&#65039; <b>${a.domain}</b></span>
                  </div>
                  <div style="margin-top:5px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:4px;">
                    <small style="color:#64748b; font-size:10.5px;">&#128336; ${new Date(a.created_at).toLocaleString()}</small>
                    <small style="font-size:11px;">Recovery: <span class="badge badge-${a.recovery_status}">${a.recovery_status}</span></small>
                  </div>
                </div>
              `).join('');
              renderOutput(items);
            }

            async function loadRemediationDetail(id) {
              const res = await fetch('/api/remediations/'+id, {headers:{Authorization:'Bearer '+document.getElementById('token').value}});
              const action = await res.json();
              currentAction = action;
              renderOutput(action);

              let decisionHtml = '';
              if (action.status === 'pending_approval') {
                decisionHtml = `
                  <div class="action-card">
                    <h4 style="margin:0 0 8px;">Human Operator Authorization</h4>
                    <p style="font-size:12px; color:#475569; margin:0 0 10px;">Approval executes the SAFE SIMULATED remediation handler. Rejection permanently locks the action from execution.</p>
                    <label>Operator Comment (Optional)</label>
                    <input id="actionComment" placeholder="e.g. Authorized by Incident Commander morgan.lee" />
                    <label>Simulation Outcome Scenario</label>
                    <select id="simOutcome">
                      <option value="success">Expected Recovery (Verified - All metrics pass thresholds)</option>
                      <option value="failure">Flawed Action (Failed - Post-action metrics exceed threshold)</option>
                      <option value="inconclusive">Telemetry Drop (Inconclusive - Incomplete telemetry stream)</option>
                    </select>
                    <div class="two" style="margin-top:12px;">
                      <button class="approve" onclick="decideAction('${action.id}', 'approve')">✓ Approve & Execute Simulation</button>
                      <button class="reject" onclick="decideAction('${action.id}', 'reject')">✕ Reject Action</button>
                    </div>
                  </div>
                `;
              } else if (action.status === 'rejected') {
                decisionHtml = `
                  <div style="background:#fee2e2; border-left:4px solid #ef4444; padding:12px; border-radius:6px; margin-top:12px;">
                    <strong style="color:#991b1b;">⛔ ACTION REJECTED</strong>
                    <p style="margin:4px 0 0; font-size:12px; color:#7f1d1d;">Rejected by <strong>${action.decided_by || 'operator'}</strong> at ${action.decided_at || '-'}. Execution is permanently blocked.</p>
                    <p style="margin:4px 0 0; font-size:12px; color:#7f1d1d;">Comment: <em>${action.decision_comment || 'No comment provided'}</em></p>
                  </div>
                `;
              } else {
                let metricsRows = '';
                (action.simulated_metrics || []).forEach(m => {
                  const statusBadge = m.passed ? '<span class="badge badge-verified">PASS</span>' : '<span class="badge badge-failed">FAIL</span>';
                  metricsRows += `
                    <tr>
                      <td><strong>${m.metric_name}</strong> (${m.unit})</td>
                      <td>${m.before_value}</td>
                      <td>${m.after_value}</td>
                      <td>${m.operator} ${m.target_threshold}</td>
                      <td>${statusBadge}</td>
                    </tr>
                  `;
                });

                decisionHtml = `
                  <div class="action-card">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                      <h4 style="margin:0;">Simulated Execution & Recovery Verification</h4>
                      <span class="badge badge-${action.recovery_status}">${action.recovery_status}</span>
                    </div>
                    <p style="font-size:12px; margin:6px 0;">Approved by <strong>${action.decided_by || 'operator'}</strong> · Status: <span class="badge badge-${action.status}">${action.status}</span></p>
                    <p style="font-size:12px; color:#1e3a8a; background:#e0f2fe; padding:8px 10px; border-radius:4px; margin:8px 0;">${action.recovery_details || 'Safe simulation executed.'}</p>
                    <h5 style="margin:12px 0 6px;">Telemetry Pre vs Post Action Measurements</h5>
                    <table class="metric-table">
                      <thead>
                        <tr><th>Metric</th><th>Before</th><th>After</th><th>Threshold</th><th>Result</th></tr>
                      </thead>
                      <tbody>${metricsRows || '<tr><td colspan="5">No metric telemetry attached.</td></tr>'}</tbody>
                    </table>
                  </div>
                `;
              }

              document.getElementById('remediationDetail').innerHTML = `
                <div>
                  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span class="badge badge-${action.domain}">${action.domain}</span>
                    <span class="badge badge-${action.risk_level}">Risk: ${action.risk_level}</span>
                    <span class="badge badge-${action.status}">${action.status}</span>
                  </div>
                  <h3 style="margin:6px 0 2px;">${action.title}</h3>
                  <p style="font-size:11px; color:#64748b; margin:0 0 10px;">Action ID: ${action.id} · Incident ID: ${action.incident_id}</p>
                  
                  <div style="background:#f1f5f9; padding:10px 12px; border-radius:6px; margin-bottom:10px;">
                    <strong style="font-size:12px; color:#475569;">Root-Cause Hypothesis:</strong>
                    <p style="margin:4px 0 0; font-size:13px;">${action.root_cause_hypothesis}</p>
                  </div>

                  <label>Recommended Remediation Action</label>
                  <div style="font-size:13px; font-weight:600; padding:8px 10px; background:#f8fafc; border:1px solid #cbd5e1; border-radius:6px;">
                    ${action.proposed_action}
                  </div>

                  <label>Rationale & Evidence</label>
                  <p style="font-size:12px; color:#334155; margin:4px 0;">${action.reasoning}</p>
                  ${(action.supporting_evidence || []).map(e => `<small style="display:block; color:#64748b;">• ${e}</small>`).join('')}

                  ${decisionHtml}
                </div>
              `;
            }

            async function decideAction(actionId, decision) {
              const comment = document.getElementById('actionComment')?.value || '';
              const outcome = document.getElementById('simOutcome')?.value || 'success';
              const endpoint = `/api/remediations/${actionId}/${decision}`;
              const payload = decision === 'approve' ? {comment, simulate_outcome: outcome} : {comment};
              
              const res = await fetch(endpoint, {
                method: 'POST',
                headers: authHeaders(),
                body: JSON.stringify(payload)
              });
              
              if (!res.ok) {
                const err = await res.json();
                alert(`Error: ${err.detail || 'Failed to update action.'}`);
                return;
              }
              const updated = await res.json();
              await loadRemediations();
              await loadRemediationDetail(actionId);
            }

            function goToRemediation(actionId) {
              const tabBtn = document.querySelectorAll('.tab')[1];
              showView('remediations', tabBtn);
              loadRemediations().then(() => {
                if (actionId) loadRemediationDetail(actionId);
              });
            }
          </script>
        </body>
        </html>
        """
    )


def _report_domain(report) -> IncidentDomain:
    if hasattr(report, "incident"):
        return report.incident.domain
    return IncidentDomain.security
