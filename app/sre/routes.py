"""AegisOps SRE API (/api/v1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError

from app.core.security import CurrentUser, get_current_user
from app.models.schemas import UserRole
from app.sre.guardrails import RateLimiter, sanitize_untrusted
from app.sre.mcp_server import MCPToolbox
from app.sre.models import (
    AlertmanagerPayload,
    ApplicationRegistration,
    IncidentState,
    IncomingEvent,
    RegisteredApplication,
    RegistrationResponse,
    Severity,
)
from app.sre.pipeline import SREOrchestrator
from app.sre.store import StateStore

router = APIRouter(prefix="/v1", tags=["AegisOps SRE"])
_webhook_limiter = RateLimiter(max_calls=120, per_seconds=60)
_admin_limiter = RateLimiter(max_calls=60, per_seconds=60)


class SREContext:
    store: StateStore
    orch: SREOrchestrator
    tasks: set[asyncio.Task] = set()


ctx = SREContext()


def require_role(*roles: UserRole):
    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(403, f"role {user.role.value} may not perform this action")
        return user
    return dep


admin_or_sre = require_role(UserRole.admin, UserRole.sre)


def _limit(limiter: RateLimiter, key: str) -> None:
    if not limiter.allow(key):
        raise HTTPException(429, "rate limit exceeded")


def _get(incident_id: str) -> IncidentState:
    st = ctx.store.get_incident(incident_id)
    if st is None:
        raise HTTPException(404, "incident not found")
    return st


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    ctx.tasks.add(t)
    t.add_done_callback(ctx.tasks.discard)


# ── Applications ────────────────────────────────────────────────────────────


@router.post("/applications/register", response_model=RegistrationResponse)
def register_application(body: ApplicationRegistration, request: Request,
                         user: CurrentUser = Depends(admin_or_sre)) -> RegistrationResponse:
    _limit(_admin_limiter, request.client.host if request.client else "local")
    app, key = ctx.store.register_application(RegisteredApplication(**body.model_dump()))
    ctx.store.audit(user.username, "application_registered", app_id=app.app_id, name=app.name)
    return RegistrationResponse(app_id=app.app_id, name=app.name, ingest_key=key)


@router.get("/applications")
def list_applications(_: CurrentUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    return [a.public() for a in ctx.store.list_applications()]


@router.get("/applications/{name}")
def get_application(name: str, _: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    app = ctx.store.find_application(name)
    if app is None:
        raise HTTPException(404, "application not found")
    return app.public()


@router.get("/applications/{name}/health")
async def application_health(name: str, _: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    app = ctx.store.find_application(name)
    if app is None:
        raise HTTPException(404, "application not found")
    tb = MCPToolbox(ctx.store)
    async with tb.session():
        h = await tb.call("check_http_health", url=str(app.health_check_url))
        m = await tb.call("query_metrics", application=app.name) if (
            app.monitoring.metrics_url or app.monitoring.prometheus_url) else None
    return {"health": h.model_dump(), "metrics": m.model_dump() if m else None}


# ── Ingestion ───────────────────────────────────────────────────────────────


@router.post("/incidents/webhook")
async def incident_webhook(request: Request, x_aegisops_key: str | None = Header(default=None),
                           wait: bool = Query(False, description="Run the investigation inline")) -> dict[str, Any]:
    """Ingest a Prometheus Alertmanager payload or a generic application event.

    Authenticated with the per-application ingest key (X-AegisOps-Key or Bearer).
    """
    _limit(_webhook_limiter, request.client.host if request.client else "local")
    if not x_aegisops_key and (auth := request.headers.get("authorization", "")).lower().startswith("bearer "):
        x_aegisops_key = auth[7:]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    try:
        if isinstance(body, dict) and "alerts" in body:
            payload = AlertmanagerPayload.model_validate(body)
            states = ctx.orch.incidents_from_alertmanager(payload)
            event = None
        else:
            event = IncomingEvent.model_validate(body)
            states = []
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from None

    app_names = {s.application for s in states} | ({event.application} if event else set())
    for name in app_names:
        app = ctx.store.find_application(name)
        if app is None or not ctx.store.verify_ingest_key(app, x_aegisops_key):
            raise HTTPException(401, f"invalid or missing ingest key for application '{name}'")

    if event:
        if event.logs or event.event_type in ("exception", "deployment", "health"):
            ctx.store.append_logs(event.application, [
                {"timestamp": event.occurred_at.isoformat(), "service": event.service,
                 "level": "error" if event.event_type == "exception" else "info",
                 "message": sanitize_untrusted(f"[{event.event_type}] {event.title} {line}", 1000)[0],
                 "correlation_id": event.correlation_id}
                for line in (event.logs or [event.description])])
        if event.event_type in ("alert", "custom") or (
                event.event_type == "exception" and event.severity in (Severity.high, Severity.critical)):
            states = [ctx.orch.incident_from_event(event, source=f"app_{event.event_type}")]

    opened = [s for s in (ctx.orch.open_incident(s) for s in states) if s]
    for s in opened:
        if wait:
            await ctx.orch.investigate(s.incident_id)
        else:
            _spawn(ctx.orch.investigate(s.incident_id))
    return {"accepted": True, "incidents": [s.incident_id for s in opened],
            "deduplicated": len(states) - len(opened)}


class ManualIncident(BaseModel):
    application: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    title: str = Field(max_length=300)
    description: str = Field(default="", max_length=4000)
    severity: Severity = Severity.medium
    service: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list, max_length=200)


@router.post("/incidents")
async def create_incident(body: ManualIncident, wait: bool = Query(True),
                          user: CurrentUser = Depends(admin_or_sre)) -> dict[str, Any]:
    event = IncomingEvent(**body.model_dump())
    state = ctx.orch.open_incident(ctx.orch.incident_from_event(event, source=f"manual:{user.username}"))
    if state is None:
        raise HTTPException(409, "an equivalent incident is already open")
    if wait:
        state = await ctx.orch.investigate(state.incident_id)
    else:
        _spawn(ctx.orch.investigate(state.incident_id))
    return state.model_dump(mode="json")


# ── Incidents ───────────────────────────────────────────────────────────────


@router.get("/incidents")
def list_incidents(application: str | None = None, _: CurrentUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    return [{"incident_id": i.incident_id, "application": i.application, "title": i.title,
             "severity": i.severity, "priority": i.priority, "phase": i.phase, "created_at": i.created_at,
             "root_cause": i.rca.primary.category if i.rca else None,
             "confidence": i.rca.primary.confidence if i.rca else None}
            for i in ctx.store.list_incidents(application)]


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str, _: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    return _get(incident_id).model_dump(mode="json")


@router.get("/incidents/{incident_id}/rca")
def get_rca(incident_id: str, _: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    st = _get(incident_id)
    if st.rca is None:
        raise HTTPException(409, f"root cause analysis not ready (phase {st.phase.value})")
    return {"rca": st.rca.model_dump(mode="json"), "findings": [f.model_dump(mode="json") for f in st.findings],
            "evidence": [e.model_dump(mode="json") for e in st.evidence]}


@router.get("/incidents/{incident_id}/remediation")
def get_remediation(incident_id: str, _: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    st = _get(incident_id)
    return {"phase": st.phase, "options": [o.model_dump(mode="json") for o in st.remediation_options],
            "approvals": [a.model_dump(mode="json") for a in st.approvals],
            "executions": [x.model_dump(mode="json") for x in st.executions]}


@router.get("/incidents/{incident_id}/postmortem")
def get_postmortem(incident_id: str, _: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    st = _get(incident_id)
    if st.postmortem is None:
        raise HTTPException(409, f"postmortem not generated yet (phase {st.phase.value})")
    return st.postmortem


@router.post("/incidents/{incident_id}/investigate")
async def run_investigation(incident_id: str, _: CurrentUser = Depends(admin_or_sre)) -> dict[str, Any]:
    _get(incident_id)
    return (await ctx.orch.investigate(incident_id)).model_dump(mode="json")


class Decision(BaseModel):
    reason: str = Field(default="", max_length=1000)


@router.post("/incidents/{incident_id}/remediation/{option_id}/approve")
async def approve(incident_id: str, option_id: str, body: Decision | None = None,
                  user: CurrentUser = Depends(admin_or_sre)) -> dict[str, Any]:
    _get(incident_id)
    try:
        st = await ctx.orch.decide(incident_id, option_id, True, user.username, (body or Decision()).reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return st.model_dump(mode="json")


@router.post("/incidents/{incident_id}/remediation/{option_id}/reject")
async def reject(incident_id: str, option_id: str, body: Decision | None = None,
                 user: CurrentUser = Depends(admin_or_sre)) -> dict[str, Any]:
    _get(incident_id)
    try:
        st = await ctx.orch.decide(incident_id, option_id, False, user.username, (body or Decision()).reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return st.model_dump(mode="json")


# ── Platform ────────────────────────────────────────────────────────────────


@router.get("/agents")
def agents(_: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    from app.sre.a2a_specialists import SPECIALISTS, llm_enabled
    return {"adk_agents": SREOrchestrator.agent_catalog(),
            "a2a_specialists": [{"name": n, "framework": "crewai", "role": s["role"],
                                 "agent_card": f"/a2a/{n}/.well-known/agent-card.json"} for n, s in SPECIALISTS.items()],
            "reasoning_mode": "crewai_llm" if llm_enabled() else "deterministic (no LLM credentials)"}


@router.get("/mcp/tools")
async def mcp_tools(_: CurrentUser = Depends(get_current_user)) -> list[str]:
    tb = MCPToolbox(ctx.store)
    async with tb.session():
        return await tb.list_tools()


@router.get("/audit")
def audit(limit: int = Query(200, le=1000), _: CurrentUser = Depends(admin_or_sre)) -> list[dict[str, Any]]:
    return ctx.store.read_audit(limit)


_CONSOLE = Path(__file__).parent / "console.html"


@router.get("/console", include_in_schema=False)
def console() -> HTMLResponse:
    return HTMLResponse(_CONSOLE.read_text(encoding="utf-8"),
                        headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})


@router.get("/system/health")
def system_health() -> dict[str, Any]:
    from app.sre.a2a_specialists import llm_enabled
    return {"status": "ok", "applications": len(ctx.store.list_applications()),
            "active_investigations": len(ctx.tasks), "llm_mode": "llm" if llm_enabled() else "deterministic"}
