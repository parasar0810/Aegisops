"""
Tests for the Google ADK -> CrewAI investigation pipeline.

Test strategy
-------------
* Framework objects (Agent, Task, Crew for CrewAI; LlmAgent, Runner, Session
  for ADK) are instantiated through the real service code.
* LLM calls are mocked with unittest.mock.patch so tests pass without
  OPENAI_API_KEY or GOOGLE_API_KEY.
* Tests verify: schema validation, context transfer, demo-mode flags,
  error handling, and that each framework class appears in the code paths.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.adk_crew_schemas import (
    ADKCrewInvestigationResult,
    AegisIncidentBrief,
    CrewPostmortemReport,
)
from app.models.schemas import (
    ActionRiskLevel,
    AlertSeverity,
    IncidentDomain,
    IncidentInput,
)
from app.services.adk_orchestrator import AegisOpsADKOrchestrator, _ADK_AVAILABLE
from app.services.crew_investigation import AegisPostmortemCrew, _CREWAI_AVAILABLE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_incident(
    domain: IncidentDomain = IncidentDomain.production,
    severity: AlertSeverity = AlertSeverity.high,
) -> IncidentInput:
    return IncidentInput(
        id=str(uuid4()),
        title="Checkout API elevated 503 errors",
        description="The checkout service is returning 503 errors at 15% of requests.",
        domain=domain,
        severity=severity,
        logs=["ERROR 503 /checkout 1714200001", "WARN latency_spike 1714200005"],
        metrics={"error_rate": 0.15, "latency_p99_ms": 4200},
        tags=["sre", "checkout"],
    )


def _make_brief(incident: IncidentInput | None = None) -> AegisIncidentBrief:
    inc = incident or _make_incident()
    return AegisIncidentBrief(
        investigation_id=uuid4(),
        incident_id=str(inc.id),
        title=inc.title,
        domain=inc.domain,
        severity=inc.severity,
        description=inc.description,
        logs=inc.logs,
        metrics={"error_rate": 0.15},
        risk_score=62,
        mcp_tool_used="query_observability",
        mcp_evidence=["p99 latency spike 4.2s", "error_rate 15%"],
        a2a_primary_agent="sre_primary",
        a2a_peer_agent="sre_peer",
        a2a_findings=["replica lag detected", "dependency timeout"],
        timeline=["[ADK-DEMO] MCP tool: 12ms", "[ADK-DEMO] A2A exchange completed"],
        adk_mode="demo",
        adk_demo_mode=True,
    )


def _make_crew_output_raw() -> str:
    """Structured JSON the postmortem writer agent would produce."""
    return json.dumps({
        "root_cause_hypothesis": "Memory pressure in checkout service caused connection pool exhaustion.",
        "confidence": 0.82,
        "contributing_factors": ["replica lag", "dependency timeout", "p99 spike"],
        "timeline_summary": "Latency spike started at T+0, errors began at T+2min.",
        "risk_level": "high",
        "remediation_recommendation": "Increase connection pool size; restart affected replicas.",
        "postmortem_summary": "Checkout service 503 errors caused by connection pool exhaustion.",
        "lessons_learned": [
            "Add pool exhaustion alerts.",
            "Pre-warm connection pools before peak traffic.",
        ],
    })


# ---------------------------------------------------------------------------
# 1.  Schema validation tests
# ---------------------------------------------------------------------------


class TestADKCrewSchemas:
    def test_adk_incident_brief_validates_risk_range(self):
        brief = _make_brief()
        assert 0 <= brief.risk_score <= 100

    def test_adk_incident_brief_has_required_framework_fields(self):
        brief = _make_brief()
        assert brief.orchestrated_by == "google_adk"
        assert brief.adk_mode in {"llm", "demo"}

    def test_crew_postmortem_report_confidence_in_range(self):
        report = CrewPostmortemReport(
            investigation_id=uuid4(),
            root_cause_hypothesis="test",
            confidence=0.75,
            timeline_summary="T+0 -> T+5",
            risk_level=ActionRiskLevel.high,
            remediation_recommendation="restart",
            postmortem_summary="summary",
            crew_agents_used=["triage_analyst", "evidence_synthesizer", "postmortem_writer"],
        )
        assert 0.0 <= report.confidence <= 1.0
        assert report.generated_by == "crewai_postmortem_crew"

    def test_adk_crew_result_framework_chain(self):
        postmortem = CrewPostmortemReport(
            investigation_id=uuid4(),
            root_cause_hypothesis="test",
            confidence=0.8,
            timeline_summary="T+0",
            risk_level=ActionRiskLevel.medium,
            remediation_recommendation="fix it",
            postmortem_summary="summary",
        )
        result = ADKCrewInvestigationResult(
            investigation_id=uuid4(),
            incident_id="inc-001",
            title="Test",
            domain=IncidentDomain.production,
            status="degraded",
            risk_score=55,
            executive_summary="summary",
            postmortem=postmortem,
        )
        assert "google_adk" in result.framework_chain
        assert "crewai" in result.framework_chain

    def test_brief_to_result_investigation_id_propagation(self):
        """investigation_id flows from ADK brief -> CrewAI report -> final result."""
        brief = _make_brief()
        postmortem = CrewPostmortemReport(
            investigation_id=brief.investigation_id,
            root_cause_hypothesis="test",
            confidence=0.7,
            timeline_summary="t",
            risk_level=ActionRiskLevel.low,
            remediation_recommendation="monitor",
            postmortem_summary="ok",
        )
        result = ADKCrewInvestigationResult(
            investigation_id=brief.investigation_id,
            incident_id=brief.incident_id,
            title=brief.title,
            domain=brief.domain,
            status="investigating",
            risk_score=brief.risk_score,
            executive_summary=postmortem.postmortem_summary,
            postmortem=postmortem,
        )
        assert result.investigation_id == brief.investigation_id
        assert result.postmortem.investigation_id == brief.investigation_id


# ---------------------------------------------------------------------------
# 2.  ADK Orchestrator tests
# ---------------------------------------------------------------------------


class TestADKOrchestrator:
    def test_adk_available_flag(self):
        """ADK package must be importable (installed via pyproject.toml)."""
        assert _ADK_AVAILABLE, (
            "google-adk is not installed. Run: uv sync"
        )

    def test_orchestrator_initializes_session_service(self):
        """InMemorySessionService must be created even without LLM credentials."""
        orch = AegisOpsADKOrchestrator()
        # If ADK is available, the session service must be set
        if _ADK_AVAILABLE:
            assert orch._session_service is not None

    @pytest.mark.asyncio
    async def test_demo_mode_returns_valid_brief(self):
        """Demo mode must return a validated AegisIncidentBrief."""
        orch = AegisOpsADKOrchestrator()
        incident = _make_incident()
        brief = orch._adk_demo_investigate(incident)

        assert isinstance(brief, AegisIncidentBrief)
        assert brief.adk_demo_mode is True
        assert brief.adk_mode == "demo"
        assert brief.orchestrated_by == "google_adk"
        assert 0 <= brief.risk_score <= 100
        assert brief.mcp_tool_used in {
            "search_security_logs", "query_observability",
            "search_runbooks", "inspect_identity_events",
        }
        assert brief.a2a_primary_agent != ""
        assert brief.a2a_peer_agent != ""

    @pytest.mark.asyncio
    async def test_investigate_returns_brief_no_credentials(self):
        """
        investigate() must return a brief regardless of LLM credential presence.
        When no credentials are configured, it falls through to demo mode.
        """
        orch = AegisOpsADKOrchestrator()
        incident = _make_incident()
        # Patch _has_llm_credentials to force demo path
        with patch.object(orch, "_has_llm_credentials", return_value=False):
            brief = await orch.investigate(incident)
        assert isinstance(brief, AegisIncidentBrief)

    @pytest.mark.asyncio
    async def test_llm_path_mocked(self):
        """
        Verify the LLM path code executes correctly when Runner.run_async
        is mocked to return a structured JSON event.
        """
        if not _ADK_AVAILABLE:
            pytest.skip("google-adk not installed")

        expected_json = json.dumps({
            "risk_score": 70,
            "mcp_tool": "query_observability",
            "mcp_evidence": ["latency spike", "error rate 15%"],
            "a2a_primary": "sre_primary",
            "a2a_peer": "sre_peer",
            "a2a_findings": ["replica lag"],
        })

        # Mock event that reports is_final_response() = True
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content.parts = [MagicMock(text=expected_json)]

        async def _fake_run_async(**kwargs):
            yield mock_event

        orch = AegisOpsADKOrchestrator()
        incident = _make_incident()

        with (
            patch.object(orch, "_has_llm_credentials", return_value=True),
            patch("app.services.adk_orchestrator.Runner") as MockRunner,
        ):
            mock_runner_instance = MagicMock()
            mock_runner_instance.run_async = _fake_run_async
            MockRunner.return_value = mock_runner_instance

            brief = await orch.investigate(incident)

        assert isinstance(brief, AegisIncidentBrief)
        assert brief.risk_score == 70
        assert brief.adk_demo_mode is False
        assert brief.adk_mode == "llm"
        assert "latency spike" in brief.mcp_evidence

    @pytest.mark.asyncio
    async def test_llm_path_fallback_on_runner_error(self):
        """If Runner.run_async raises, investigate() must fall back to demo mode."""
        if not _ADK_AVAILABLE:
            pytest.skip("google-adk not installed")

        orch = AegisOpsADKOrchestrator()
        incident = _make_incident()

        with (
            patch.object(orch, "_has_llm_credentials", return_value=True),
            patch.object(orch, "_adk_llm_investigate", side_effect=RuntimeError("no creds")),
        ):
            brief = await orch.investigate(incident)

        assert isinstance(brief, AegisIncidentBrief)
        assert brief.adk_demo_mode is True

    @pytest.mark.asyncio
    async def test_brief_timeline_populated(self):
        """Timeline must contain at least one ADK-labeled entry."""
        orch = AegisOpsADKOrchestrator()
        incident = _make_incident()
        with patch.object(orch, "_has_llm_credentials", return_value=False):
            brief = await orch.investigate(incident)
        assert any("[ADK" in entry for entry in brief.timeline)

    @pytest.mark.asyncio
    async def test_security_domain_uses_search_security_logs(self):
        """Domain routing: security domain must use search_security_logs MCP tool."""
        orch = AegisOpsADKOrchestrator()
        incident = _make_incident(domain=IncidentDomain.security)
        with patch.object(orch, "_has_llm_credentials", return_value=False):
            brief = await orch.investigate(incident)
        assert brief.mcp_tool_used == "search_security_logs"

    @pytest.mark.asyncio
    async def test_production_domain_uses_query_observability(self):
        """Domain routing: production domain must use query_observability MCP tool."""
        orch = AegisOpsADKOrchestrator()
        incident = _make_incident(domain=IncidentDomain.production)
        with patch.object(orch, "_has_llm_credentials", return_value=False):
            brief = await orch.investigate(incident)
        assert brief.mcp_tool_used == "query_observability"


# ---------------------------------------------------------------------------
# 3.  CrewAI crew tests
# ---------------------------------------------------------------------------


class TestCrewPostmortem:
    def test_crewai_available_flag(self):
        """crewai package must be importable."""
        assert _CREWAI_AVAILABLE, "crewai is not installed. Run: uv sync"

    def test_crew_builds_without_llm_call(self):
        """_build_crew() must construct Agent/Task/Crew objects without LLM."""
        crew_svc = AegisPostmortemCrew()
        brief = _make_brief()
        crew = crew_svc._build_crew(brief)
        # Verify CrewAI objects were created
        assert crew is not None
        assert len(crew.agents) == 3
        assert len(crew.tasks) == 3

    def test_run_mocked_kickoff_returns_valid_report(self):
        """
        crew.kickoff() mocked to return structured JSON.
        Verifies _parse_output() produces a valid CrewPostmortemReport.
        """
        crew_svc = AegisPostmortemCrew()
        brief = _make_brief()

        mock_output = MagicMock()
        mock_output.raw = _make_crew_output_raw()

        with patch("crewai.Crew.kickoff", return_value=mock_output):
            report = crew_svc.run(brief)

        assert isinstance(report, CrewPostmortemReport)
        assert report.investigation_id == brief.investigation_id
        assert report.crew_demo_mode is False
        assert "connection pool" in report.root_cause_hypothesis
        assert report.confidence == pytest.approx(0.82)
        assert report.risk_level == ActionRiskLevel.high
        assert len(report.contributing_factors) == 3
        assert len(report.lessons_learned) == 2
        assert report.crew_agents_used == [
            "triage_analyst", "evidence_synthesizer", "postmortem_writer"
        ]

    def test_run_no_credentials_returns_demo_report(self):
        """
        When kickoff() raises (no LLM credentials), run() must return a
        clearly labeled demo report, not raise an exception.
        """
        crew_svc = AegisPostmortemCrew()
        brief = _make_brief()

        with patch("crewai.Crew.kickoff", side_effect=Exception("no API key")):
            report = crew_svc.run(brief)

        assert isinstance(report, CrewPostmortemReport)
        assert report.crew_demo_mode is True
        assert "DEMO MODE" in report.root_cause_hypothesis
        assert report.investigation_id == brief.investigation_id

    def test_parse_output_handles_json_fences(self):
        """_extract_json must handle code-fenced LLM output."""
        crew_svc = AegisPostmortemCrew()
        fenced = f"```json\n{_make_crew_output_raw()}\n```"
        mock_out = MagicMock()
        mock_out.raw = fenced
        brief = _make_brief()
        with patch("crewai.Crew.kickoff", return_value=mock_out):
            report = crew_svc.run(brief)
        assert report.crew_demo_mode is False
        assert report.confidence == pytest.approx(0.82)

    def test_parse_output_handles_invalid_json(self):
        """_extract_json must return empty dict gracefully on bad output."""
        crew_svc = AegisPostmortemCrew()
        mock_out = MagicMock()
        mock_out.raw = "Sorry, I cannot provide a postmortem."
        brief = _make_brief()
        with patch("crewai.Crew.kickoff", return_value=mock_out):
            report = crew_svc.run(brief)
        # Falls back to defaults, not an exception
        assert isinstance(report, CrewPostmortemReport)

    def test_demo_report_preserves_investigation_id(self):
        """Demo report must carry the same investigation_id as the brief."""
        crew_svc = AegisPostmortemCrew()
        brief = _make_brief()
        report = crew_svc._demo_report(brief)
        assert report.investigation_id == brief.investigation_id
        assert report.crew_demo_mode is True

    def test_agent_names_in_crew(self):
        """All three agent roles must be present in the built crew."""
        crew_svc = AegisPostmortemCrew()
        brief = _make_brief()
        crew = crew_svc._build_crew(brief)
        roles = {agent.role for agent in crew.agents}
        assert "Incident Triage Analyst" in roles
        assert "Evidence Synthesizer" in roles
        assert "Postmortem Writer" in roles


# ---------------------------------------------------------------------------
# 4.  Integration test: full ADK -> CrewAI pipeline
# ---------------------------------------------------------------------------


class TestADKCrewIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline_demo_mode(self):
        """
        End-to-end demo pipeline: ADK (demo) -> CrewAI (mocked kickoff).
        Verifies context flows correctly from ADK brief to final result.
        """
        orch = AegisOpsADKOrchestrator()
        crew_svc = AegisPostmortemCrew()
        incident = _make_incident()

        # Step 1: ADK produces brief (demo mode -- no LLM)
        with patch.object(orch, "_has_llm_credentials", return_value=False):
            brief = await orch.investigate(incident)

        assert isinstance(brief, AegisIncidentBrief)

        # Step 2: CrewAI processes brief (mocked LLM)
        mock_output = MagicMock()
        mock_output.raw = _make_crew_output_raw()

        with patch("crewai.Crew.kickoff", return_value=mock_output):
            postmortem = crew_svc.run(brief)

        assert isinstance(postmortem, CrewPostmortemReport)

        # Verify investigation_id propagation
        assert postmortem.investigation_id == brief.investigation_id

        # Verify context was transferred
        assert postmortem.confidence > 0.0
        assert postmortem.risk_level in ActionRiskLevel.__members__.values()

        # Step 3: Compose result
        result = ADKCrewInvestigationResult(
            investigation_id=brief.investigation_id,
            incident_id=brief.incident_id,
            title=brief.title,
            domain=brief.domain,
            status="degraded",
            risk_score=brief.risk_score,
            executive_summary=postmortem.postmortem_summary,
            timeline=brief.timeline,
            mcp_tools_used=[brief.mcp_tool_used],
            a2a_agents_consulted=[brief.a2a_primary_agent, brief.a2a_peer_agent],
            postmortem=postmortem,
        )

        assert result.framework_chain == ["google_adk", "crewai"]
        assert result.investigation_id == brief.investigation_id
        assert result.postmortem.investigation_id == brief.investigation_id

    @pytest.mark.asyncio
    async def test_full_pipeline_schema_all_fields_present(self):
        """ADKCrewInvestigationResult must contain all mandatory fields."""
        orch = AegisOpsADKOrchestrator()
        crew_svc = AegisPostmortemCrew()
        incident = _make_incident(domain=IncidentDomain.security, severity=AlertSeverity.critical)

        with patch.object(orch, "_has_llm_credentials", return_value=False):
            brief = await orch.investigate(incident)

        mock_output = MagicMock()
        mock_output.raw = _make_crew_output_raw()
        with patch("crewai.Crew.kickoff", return_value=mock_output):
            postmortem = crew_svc.run(brief)

        result = ADKCrewInvestigationResult(
            investigation_id=brief.investigation_id,
            incident_id=brief.incident_id,
            title=brief.title,
            domain=brief.domain,
            status="major_incident",
            risk_score=brief.risk_score,
            executive_summary=postmortem.postmortem_summary,
            timeline=brief.timeline,
            mcp_tools_used=[brief.mcp_tool_used],
            a2a_agents_consulted=[brief.a2a_primary_agent, brief.a2a_peer_agent],
            postmortem=postmortem,
            adk_demo_mode=brief.adk_demo_mode,
            crew_demo_mode=postmortem.crew_demo_mode,
        )

        # Validate required fields
        assert result.incident_id
        assert result.title
        assert result.domain == IncidentDomain.security
        assert result.risk_score >= 0
        assert result.postmortem.root_cause_hypothesis
        assert len(result.mcp_tools_used) == 1
        assert len(result.a2a_agents_consulted) == 2
        assert result.adk_demo_mode is True  # no credentials
        # Crew ran through kickoff (mocked) so crew_demo_mode is False
        assert result.crew_demo_mode is False
