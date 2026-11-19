"""
AegisOps -- Pydantic schemas for the Google ADK -> CrewAI pipeline.

Data contract:
  IncidentInput  ->  [Google ADK]  ->  AegisIncidentBrief
                                            |
                               [CrewAI Crew (3 agents)]
                                            |
                                   CrewPostmortemReport
                                            |
                               ADKCrewInvestigationResult  (returned to caller)

No unvalidated raw model output is passed between frameworks or into the
remediation system.  Every boundary is a Pydantic model_validate() call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.models.schemas import ActionRiskLevel, AlertSeverity, IncidentDomain


class AegisIncidentBrief(BaseModel):
    """
    Typed context produced by the Google ADK orchestrator after calling
    MCP and A2A tools.  Passed as a validated input to the CrewAI crew.
    """

    investigation_id: UUID = Field(default_factory=uuid4)
    incident_id: str
    title: str
    domain: IncidentDomain
    severity: AlertSeverity
    description: str
    logs: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    risk_score: int = Field(ge=0, le=100)

    mcp_tool_used: str
    mcp_evidence: list[str] = Field(default_factory=list)

    a2a_primary_agent: str
    a2a_peer_agent: str
    a2a_findings: list[str] = Field(default_factory=list)

    timeline: list[str] = Field(default_factory=list)
    orchestrated_by: str = "google_adk"
    adk_session_id: str | None = None
    adk_mode: str = "llm"
    adk_demo_mode: bool = False


class CrewPostmortemReport(BaseModel):
    """
    Structured postmortem produced by the CrewAI investigation crew.
    Three sequential agents: Triage Analyst -> Evidence Synthesizer -> Postmortem Writer.
    """

    investigation_id: UUID
    root_cause_hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    contributing_factors: list[str] = Field(default_factory=list)
    timeline_summary: str
    risk_level: ActionRiskLevel
    remediation_recommendation: str
    postmortem_summary: str
    lessons_learned: list[str] = Field(default_factory=list)
    crew_agents_used: list[str] = Field(default_factory=list)

    generated_by: str = "crewai_postmortem_crew"
    crew_demo_mode: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ADKCrewInvestigationResult(BaseModel):
    """
    Final Pydantic-validated result returned by POST /api/adk/investigate.
    Combines the ADK orchestration output and the CrewAI postmortem.
    """

    investigation_id: UUID
    incident_id: str
    title: str
    domain: IncidentDomain
    status: str
    risk_score: int = Field(ge=0, le=100)
    executive_summary: str
    timeline: list[str] = Field(default_factory=list)
    mcp_tools_used: list[str] = Field(default_factory=list)
    a2a_agents_consulted: list[str] = Field(default_factory=list)
    postmortem: CrewPostmortemReport
    framework_chain: list[str] = Field(default_factory=lambda: ["google_adk", "crewai"])
    adk_demo_mode: bool = False
    crew_demo_mode: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
