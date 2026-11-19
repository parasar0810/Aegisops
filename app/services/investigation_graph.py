from __future__ import annotations

import time

from app.agents.enrichment import ThreatEnrichmentAgent
from app.agents.evidence import EvidenceCollectorAgent
from app.agents.remediation import RemediationRecommenderAgent
from app.agents.triage import AlertTriageAgent
from app.core.config import get_settings
from app.core.telemetry import (
    AUTOMATED_STEPS_TOTAL,
    DOMAIN_ROUTING_TOTAL,
    EVIDENCE_ITEMS_TOTAL,
    INVESTIGATION_DURATION,
    INVESTIGATIONS_TOTAL,
)
from app.models.schemas import (
    AgentExchangeRequest,
    AgentFinding,
    EvidenceItem,
    IncidentDomain,
    IncidentInput,
    InvestigationReport,
    MCPToolCallRequest,
    RemediationStep,
    SecurityAlert,
    Verdict,
)
from app.models.state import InvestigationState
from app.rag.retriever import SecurityKnowledgeRetriever
from app.services.a2a import LocalA2ARegistry
from app.services.llm import OllamaService
from app.services.mcp_tools import LocalMCPToolRegistry
from app.services.tracing import LangfuseTracer


class InvestigationGraph:
    """Sequential pipeline for the RAG-grounded security investigation workflow."""

    def __init__(self, retriever: SecurityKnowledgeRetriever | None = None):
        self.retriever = retriever or SecurityKnowledgeRetriever()
        self.llm = OllamaService()
        self.tracer = LangfuseTracer()
        self.a2a = LocalA2ARegistry()
        self.mcp_tools = LocalMCPToolRegistry()
        self.triage_agent = AlertTriageAgent()
        self.enrichment_agent = ThreatEnrichmentAgent()
        self.evidence_agent = EvidenceCollectorAgent()
        self.remediation_agent = RemediationRecommenderAgent()
        self.steps = [self._retrieve_context, self._soc_a2a_handoff, self.triage_agent.run,
                      self.enrichment_agent.run, self.evidence_agent.run, self._llm_reasoning,
                      self.remediation_agent.run]

    def investigate(self, alert: SecurityAlert) -> InvestigationReport:
        started = time.perf_counter()
        state = InvestigationState(alert=alert)
        for step in self.steps:
            state = step(state)

        state.risk_score = max(0, min(state.risk_score, 100))
        verdict = self._verdict_for_score(state.risk_score)
        report = InvestigationReport(
            alert=alert,
            verdict=verdict,
            risk_score=state.risk_score,
            executive_summary=self._summary(alert, verdict, state.risk_score),
            timeline=state.timeline,
            findings=state.findings,
            evidence=state.evidence,
            recommended_actions=self._build_actions(state),
            references=state.references,
        )

        self.tracer.trace_investigation(report)
        INVESTIGATIONS_TOTAL.labels(severity=alert.severity.value, verdict=verdict.value).inc()
        INVESTIGATION_DURATION.observe(time.perf_counter() - started)
        return report

    async def investigate_async(
        self,
        alert: SecurityAlert,
        *,
        session_id: str | None = None,
    ) -> InvestigationReport:
        return self.investigate(alert)

    def _report_from_state(self, state: InvestigationState) -> InvestigationReport:
        state.risk_score = max(0, min(state.risk_score, 100))
        verdict = self._verdict_for_score(state.risk_score)
        report = InvestigationReport(
            alert=state.alert,
            verdict=verdict,
            risk_score=state.risk_score,
            executive_summary=self._summary(state.alert, verdict, state.risk_score),
            timeline=state.timeline,
            findings=state.findings,
            evidence=state.evidence,
            recommended_actions=self._build_actions(state),
            references=state.references,
        )
        self.tracer.trace_investigation(report)
        INVESTIGATIONS_TOTAL.labels(severity=state.alert.severity.value, verdict=verdict.value).inc()
        return report

    def _retrieve_context(self, state: InvestigationState) -> InvestigationState:
        state.references = self.retriever.retrieve_for_alert(state.alert)
        state.timeline.append(f"Retrieved {len(state.references)} relevant knowledge base documents.")
        tool_result = self.mcp_tools.execute(
            MCPToolCallRequest(
                tool_name="search_security_logs",
                arguments={"query": f"{state.alert.title} {state.alert.description}", "limit": 5},
                alert=state.alert,
            )
        )
        state.enrichment["mcp_evidence"] = tool_result.result
        for record in tool_result.result.get("records", [])[:3]:
            state.evidence.append(
                EvidenceItem(
                    kind="mcp_tool",
                    description=str(record.get("signal", "mcp evidence")),
                    value=str(record.get("summary", record)),
                    weight=5,
                )
            )
            EVIDENCE_ITEMS_TOTAL.labels(domain=IncidentDomain.security.value, kind="mcp_tool").inc()
        state.timeline.append(
            f"MCP tool search_security_logs collected local security evidence in {tool_result.duration_ms} ms."
        )
        AUTOMATED_STEPS_TOTAL.labels(domain=IncidentDomain.security.value, step="mcp_evidence_collection").inc()
        AUTOMATED_STEPS_TOTAL.labels(domain=IncidentDomain.security.value, step="rag_context_retrieval").inc()
        return state

    def _soc_a2a_handoff(self, state: InvestigationState) -> InvestigationState:
        incident = IncidentInput(
            id=state.alert.id,
            title=state.alert.title,
            domain=IncidentDomain.security,
            severity=state.alert.severity,
            source=state.alert.source,
            detected_at=state.alert.detected_at,
            service=state.alert.host,
            owner_team="SOC",
            description=state.alert.description,
            events=state.alert.raw_events,
            tags=state.alert.tags,
        )
        exchange = self.a2a.exchange(
            AgentExchangeRequest(
                source_agent="security_router",
                incident=incident,
                context={"references": len(state.references)},
            )
        )
        state.enrichment["a2a_exchange"] = exchange.model_dump(mode="json")
        state.timeline.append(
            f"A2A exchange completed across {exchange.primary_agent} and {exchange.peer_agent} "
            f"with {len(exchange.messages)} task/result messages."
        )
        for message in exchange.messages:
            state.timeline.append(
                f"A2A message {message.source_agent} -> {message.target_agent}: {message.task_type}."
            )
        DOMAIN_ROUTING_TOTAL.labels(domain=IncidentDomain.security.value, target_agent="soc_agent").inc()
        AUTOMATED_STEPS_TOTAL.labels(domain=IncidentDomain.security.value, step="a2a_agent_exchange").inc()
        for message in exchange.messages:
            for item in message.evidence:
                state.evidence.append(item)
                EVIDENCE_ITEMS_TOTAL.labels(domain=IncidentDomain.security.value, kind=item.kind).inc()
        return state

    def _llm_reasoning(self, state: InvestigationState) -> InvestigationState:
        analysis = self.llm.analyze_alert(state.alert, state.references)
        state.timeline.append("LLM reasoning generated a grounded investigation assessment.")
        AUTOMATED_STEPS_TOTAL.labels(domain=IncidentDomain.security.value, step="llm_reasoning").inc()
        state.findings.append(
            AgentFinding(
                agent="llm_reasoning",
                summary=analysis.summary,
                risk_delta=0,
                confidence=analysis.confidence,
                evidence=[*analysis.likely_causes, *analysis.recommended_next_steps],
                references=state.references[:3],
            )
        )
        return state

    @staticmethod
    def _verdict_for_score(score: int) -> Verdict:
        settings = get_settings()
        if score >= settings.risk_threshold_high + 15:
            return Verdict.confirmed_incident
        if score >= settings.risk_threshold_high:
            return Verdict.likely_compromise
        if score >= settings.risk_threshold_medium:
            return Verdict.suspicious
        return Verdict.benign

    @staticmethod
    def _summary(alert: SecurityAlert, verdict: Verdict, score: int) -> str:
        target = alert.user or alert.host or "the affected asset"
        return (
            f"{alert.title} for {target} is assessed as {verdict.value.replace('_', ' ')} "
            f"with risk score {score}/100. The strongest indicators are identity anomaly, "
            "source infrastructure context, and post-authentication activity."
        )

    @staticmethod
    def _build_actions(state: InvestigationState) -> list[RemediationStep]:
        action_text = state.enrichment.get("recommended_action_text", [])
        if not isinstance(action_text, list):
            action_text = []

        actions = []
        for index, action in enumerate(action_text, start=1):
            owner = "Identity Engineering" if "MFA" in action or "password" in action else "SOC"
            actions.append(
                RemediationStep(
                    priority=min(index, 5),
                    action=action,
                    owner=owner,
                    rationale="Aligned to retrieved identity incident response playbooks and observed evidence.",
                )
            )
        return actions
