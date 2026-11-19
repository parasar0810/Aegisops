"""Typed shared state for the AegisOps SRE pipeline.

Every agent reads and writes one ``IncidentState``. It is persisted after each
step (see ``store.py``) so an investigation can resume after a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class Severity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class RiskLevel(StrEnum):
    read_only = "read_only"
    low = "low"
    high = "high"


class IncidentPhase(StrEnum):
    detected = "detected"
    investigating = "investigating"
    awaiting_approval = "awaiting_approval"
    remediating = "remediating"
    verifying = "verifying"
    resolved = "resolved"
    unresolved = "unresolved"
    closed_no_action = "closed_no_action"
    failed = "failed"


class FailureCategory(StrEnum):
    cpu_saturation = "cpu_saturation"
    memory_exhaustion = "memory_exhaustion"
    http_errors = "http_errors"
    high_latency = "high_latency"
    database_failure = "database_failure"
    service_unavailable = "service_unavailable"
    bad_deployment = "bad_deployment"
    abnormal_traffic = "abnormal_traffic"
    unknown = "unknown"


# ── Application registration ────────────────────────────────────────────────


class RemediationHook(BaseModel):
    """A remediation action the monitored application explicitly exposes.

    AegisOps never runs commands it invents; it can only POST to hooks the
    application registered, and only on the application's own host.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    description: str = Field(max_length=300)
    url: HttpUrl
    risk: RiskLevel = RiskLevel.high
    addresses: list[FailureCategory] = Field(default_factory=list)
    rollback_hint: str = Field(default="", max_length=300)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class MonitoringConfig(BaseModel):
    metrics_url: HttpUrl | None = None
    prometheus_url: HttpUrl | None = None
    logs_url: HttpUrl | None = None
    # signal name -> PromQL (when prometheus_url is set) or a bare metric name (scrape mode)
    metric_queries: dict[str, str] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    container_name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    database_host: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9.-]{1,253}$")
    database_port: int | None = Field(default=None, ge=1, le=65535)
    # Seconds to wait after a fix before verifying; rate()-based PromQL needs a window to drain.
    verify_settle_seconds: float | None = Field(default=None, ge=0, le=300)
    # How far back log evidence reaches; short windows keep earlier incidents' errors out.
    log_window_minutes: int = Field(default=10, ge=1, le=1440)


class ServiceInfo(BaseModel):
    name: str = Field(max_length=100)
    kind: str = Field(default="service", max_length=50)
    depends_on: list[str] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)


class ApplicationRegistration(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    description: str = Field(default="", max_length=1000)
    base_url: HttpUrl
    environment: Literal["development", "staging", "production", "demo"] = "production"
    tech_stack: list[str] = Field(default_factory=list, max_length=30)
    health_check_url: HttpUrl
    services: list[ServiceInfo] = Field(default_factory=list, max_length=50)
    deployment: dict[str, str] = Field(default_factory=dict)
    infrastructure: dict[str, str] = Field(default_factory=dict)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    remediation_hooks: list[RemediationHook] = Field(default_factory=list, max_length=20)

    @field_validator("remediation_hooks")
    @classmethod
    def _unique_hook_names(cls, hooks: list[RemediationHook]) -> list[RemediationHook]:
        names = [h.name for h in hooks]
        if len(names) != len(set(names)):
            raise ValueError("remediation hook names must be unique")
        return hooks

    @model_validator(mode="after")
    def _hooks_on_app_host(self) -> ApplicationRegistration:
        base_host = urlparse(str(self.base_url)).hostname
        for hook in self.remediation_hooks:
            if urlparse(str(hook.url)).hostname != base_host:
                raise ValueError(f"hook '{hook.name}' must target the application host {base_host}")
        return self


class RegisteredApplication(ApplicationRegistration):
    app_id: str = Field(default_factory=lambda: f"app-{uuid4().hex[:12]}")
    ingest_key_hash: str = ""
    registered_at: datetime = Field(default_factory=utcnow)
    last_health: dict[str, Any] = Field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"ingest_key_hash"})


class RegistrationResponse(BaseModel):
    app_id: str
    name: str
    ingest_key: str
    note: str = "Store ingest_key securely; send it as X-AegisOps-Key on webhooks. It is not shown again."


# ── Alert ingestion ─────────────────────────────────────────────────────────


class IncomingEvent(BaseModel):
    """Generic application/monitoring event (non-Alertmanager)."""

    application: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    event_type: Literal["alert", "exception", "health", "deployment", "custom"] = "alert"
    title: str = Field(max_length=300)
    description: str = Field(default="", max_length=4000)
    severity: Severity = Severity.medium
    service: str | None = Field(default=None, max_length=100)
    labels: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list, max_length=200)
    correlation_id: str | None = Field(default=None, max_length=100)
    occurred_at: datetime = Field(default_factory=utcnow)


class AlertmanagerAlert(BaseModel):
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None


class AlertmanagerPayload(BaseModel):
    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    alerts: list[AlertmanagerAlert] = Field(default_factory=list, max_length=100)
    commonLabels: dict[str, str] = Field(default_factory=dict)


# ── Investigation artefacts ─────────────────────────────────────────────────


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: f"ev-{uuid4().hex[:8]}")
    source: str  # mcp tool name or "alert"
    kind: Literal["metric", "log", "health", "trace", "deployment", "container", "database", "alert"]
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=utcnow)
    untrusted: bool = True


class SpecialistFinding(BaseModel):
    agent: str
    category: FailureCategory
    observation: str
    supporting_evidence: list[str] = Field(default_factory=list)  # evidence ids
    weight: float = Field(ge=0.0, le=1.0)
    reasoning_mode: Literal["crewai_llm", "deterministic"] = "deterministic"


class Hypothesis(BaseModel):
    category: FailureCategory
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)


class RootCauseAnalysis(BaseModel):
    primary: Hypothesis
    alternatives: list[Hypothesis] = Field(default_factory=list)
    confirmed_observations: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence_explanation: str
    insufficient_evidence: bool = False
    similar_past_incidents: list[str] = Field(default_factory=list)


class RemediationOption(BaseModel):
    option_id: str = Field(default_factory=lambda: f"rem-{uuid4().hex[:8]}")
    hook_name: str | None = None  # None = manual / investigate-only recommendation
    description: str
    risk: RiskLevel
    expected_impact: str
    side_effects: str = ""
    rollback: str = ""
    rank: int = 1
    requires_approval: bool = True


class ApprovalRecord(BaseModel):
    option_id: str
    decision: Literal["approved", "rejected"]
    decided_by: str
    reason: str = Field(default="", max_length=1000)
    decided_at: datetime = Field(default_factory=utcnow)


class ExecutionResult(BaseModel):
    option_id: str
    hook_name: str | None
    status: Literal["succeeded", "failed", "skipped"]
    http_status: int | None = None
    detail: str = ""
    executed_at: datetime = Field(default_factory=utcnow)


class RecoveryVerification(BaseModel):
    recovered: bool
    before: dict[str, float] = Field(default_factory=dict)
    after: dict[str, float] = Field(default_factory=dict)
    health_status_code: int | None = None
    checks: list[str] = Field(default_factory=list)
    next_step: str = ""
    verified_at: datetime = Field(default_factory=utcnow)


class TimelineEntry(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    agent: str
    event: str
    duration_ms: float | None = None


class IncidentState(BaseModel):
    incident_id: str = Field(default_factory=lambda: f"inc-{uuid4().hex[:10]}")
    phase: IncidentPhase = IncidentPhase.detected
    application: str
    service: str | None = None
    title: str
    description: str = ""
    severity: Severity
    priority: Literal["P1", "P2", "P3", "P4"] = "P3"
    source: str = "webhook"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    initial_alert: dict[str, Any] = Field(default_factory=dict)
    application_context: dict[str, Any] = Field(default_factory=dict)
    triage: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[SpecialistFinding] = Field(default_factory=list)
    a2a_log: list[dict[str, Any]] = Field(default_factory=list)
    rca: RootCauseAnalysis | None = None
    remediation_options: list[RemediationOption] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    executions: list[ExecutionResult] = Field(default_factory=list)
    verification: RecoveryVerification | None = None
    postmortem: dict[str, Any] | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)

    def log(self, agent: str, event: str, duration_ms: float | None = None) -> None:
        self.timeline.append(TimelineEntry(agent=agent, event=event, duration_ms=duration_ms))
        self.updated_at = utcnow()

    def metric_snapshot(self) -> dict[str, float]:
        """Latest numeric value per metric signal from collected evidence."""
        out: dict[str, float] = {}
        for ev in self.evidence:
            if ev.kind == "metric":
                for k, v in ev.data.get("values", {}).items():
                    if isinstance(v, (int, float)):
                        out[k] = float(v)
        return out
