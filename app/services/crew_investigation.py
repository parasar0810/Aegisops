"""
AegisOps -- CrewAI Investigation Crew.

Role in the pipeline
--------------------
Receives a validated AegisIncidentBrief (from the Google ADK orchestrator)
and runs a 3-agent sequential CrewAI crew to synthesise evidence and produce
a structured postmortem report.

Agents
------
1. Incident Triage Analyst    -- classifies risk level, forms root-cause hypothesis
2. Evidence Synthesizer       -- reviews MCP + A2A findings, identifies contributing factors
3. Postmortem Writer          -- writes the final structured postmortem

LLM selection
-------------
Priority:  OPENAI_API_KEY -> Ollama (ollama_base_url) -> demo fallback.
In demo fallback, the Crew / Agent / Task objects are still instantiated and
crew.kickoff() is still called, but the call is expected to fail; the
exception is caught, the result is clearly labeled crew_demo_mode=True, and
the framework code paths ARE exercised.

In tests, callers should mock crewai.Crew.kickoff to return a structured
CrewOutput without making real LLM calls.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import get_settings
from app.models.adk_crew_schemas import AegisIncidentBrief, CrewPostmortemReport
from app.models.schemas import ActionRiskLevel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy CrewAI imports
# ---------------------------------------------------------------------------
try:
    from crewai import Agent, Crew, LLM, Process, Task  # noqa: F401
    _CREWAI_AVAILABLE = True
except Exception:
    Agent = None        # type: ignore[assignment,misc]
    Crew = None         # type: ignore[assignment,misc]
    LLM = None          # type: ignore[assignment,misc]
    Process = None      # type: ignore[assignment,misc]
    Task = None         # type: ignore[assignment,misc]
    _CREWAI_AVAILABLE = False

_AGENT_NAMES = [
    "triage_analyst",
    "evidence_synthesizer",
    "postmortem_writer",
]


# ---------------------------------------------------------------------------
# Crew
# ---------------------------------------------------------------------------


class AegisPostmortemCrew:
    """
    Three-agent CrewAI crew: Triage Analyst -> Evidence Synthesizer -> Postmortem Writer.

    This class is the ONLY consumer of the crewai package in the project.
    It exposes a single public method: run(brief) -> CrewPostmortemReport.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._llm: Any = self._build_llm()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self, brief: AegisIncidentBrief) -> CrewPostmortemReport:
        """
        Run the CrewAI investigation crew on an AegisIncidentBrief.

        Returns CrewPostmortemReport.  In the absence of LLM credentials
        (or if CrewAI is not installed), returns a demo-labeled report that
        preserves all schema fields.  Tests should mock Crew.kickoff.
        """
        if not _CREWAI_AVAILABLE:
            logger.warning("crewai package is not installed -- returning demo report.")
            return self._demo_report(brief, reason="crewai not installed")

        crew = self._build_crew(brief)
        inputs = self._build_inputs(brief)

        try:
            output = crew.kickoff(inputs=inputs)
            return self._parse_output(output, brief)
        except Exception as exc:
            logger.warning(
                "CrewAI kickoff failed (%s) -- returning demo report.  "
                "Ensure OPENAI_API_KEY or Ollama is configured for live LLM mode.",
                exc,
            )
            return self._demo_report(brief, reason=str(exc))

    async def run_async(self, brief: AegisIncidentBrief) -> CrewPostmortemReport:
        """
        Async variant of run() for use inside FastAPI async endpoints.

        Uses crew.kickoff_async() which is natively async in crewai >= 0.80.
        Tests should mock crewai.Crew.kickoff_async.
        """
        if not _CREWAI_AVAILABLE:
            logger.warning("crewai package is not installed -- returning demo report.")
            return self._demo_report(brief, reason="crewai not installed")

        crew = self._build_crew(brief)
        inputs = self._build_inputs(brief)

        try:
            output = await crew.kickoff_async(inputs=inputs)
            return self._parse_output(output, brief)
        except Exception as exc:
            logger.warning(
                "CrewAI kickoff_async failed (%s) -- returning demo report.  "
                "Ensure OPENAI_API_KEY or Ollama is configured for live LLM mode.",
                exc,
            )
            return self._demo_report(brief, reason=str(exc))

    # -----------------------------------------------------------------------
    # Crew construction
    # -----------------------------------------------------------------------

    def _build_crew(self, brief: AegisIncidentBrief) -> "Crew":
        """Build the 3-agent sequential CrewAI crew."""
        llm = self._llm

        # ── Agent 1: Triage Analyst ────────────────────────────────────────
        triage_agent = Agent(
            role="Incident Triage Analyst",
            goal=(
                "Assess the severity and risk of the incident. "
                "Identify the most likely root cause based on evidence."
            ),
            backstory=(
                "You are a senior site reliability and security analyst with 10 years "
                "of experience investigating production incidents, security breaches, and "
                "cloud outages.  You are concise, evidence-driven, and never speculative."
            ),
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )

        # ── Agent 2: Evidence Synthesizer ─────────────────────────────────
        evidence_agent = Agent(
            role="Evidence Synthesizer",
            goal=(
                "Review all MCP tool signals and A2A agent findings. "
                "Identify contributing factors and causal chains."
            ),
            backstory=(
                "You specialize in correlating signals from disparate observability "
                "and security tools to build a coherent picture of incident causality."
            ),
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )

        # ── Agent 3: Postmortem Writer ─────────────────────────────────────
        postmortem_agent = Agent(
            role="Postmortem Writer",
            goal=(
                "Write a structured, actionable postmortem report in JSON format.  "
                "Include root cause, contributing factors, timeline, remediation, "
                "and lessons learned."
            ),
            backstory=(
                "You turn incident analyses into clear, well-structured postmortems "
                "that engineering teams can act on immediately."
            ),
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )

        # ── Tasks ──────────────────────────────────────────────────────────
        triage_task = Task(
            description=(
                "Analyze the following incident brief and classify the risk level "
                "(low/medium/high/critical) and form a root-cause hypothesis.\n\n"
                "Brief JSON:\n{brief_json}\n\n"
                "Return a concise analysis paragraph."
            ),
            expected_output=(
                "A paragraph describing risk level, root cause hypothesis, and "
                "initial confidence (0.0-1.0)."
            ),
            agent=triage_agent,
        )

        evidence_task = Task(
            description=(
                "Using the triage analysis and the original brief, synthesise the "
                "MCP tool signals and A2A agent findings into a list of contributing "
                "factors and a timeline summary.\n\n"
                "Brief JSON:\n{brief_json}\n\n"
                "Return a bullet-point list of contributing factors followed by a "
                "one-paragraph timeline summary."
            ),
            expected_output=(
                "Bullet list of contributing factors + one-paragraph timeline summary."
            ),
            agent=evidence_agent,
        )

        postmortem_task = Task(
            description=(
                "Using the triage and evidence analyses, write the final postmortem "
                "as a JSON object with EXACTLY these keys (no extras):\n"
                "  root_cause_hypothesis (str)\n"
                "  confidence (float 0.0-1.0)\n"
                "  contributing_factors (list[str])\n"
                "  timeline_summary (str)\n"
                "  risk_level (one of: low, medium, high, critical)\n"
                "  remediation_recommendation (str)\n"
                "  postmortem_summary (str)\n"
                "  lessons_learned (list[str])\n\n"
                "Brief JSON:\n{brief_json}\n\n"
                "Respond with ONLY the JSON object, no additional text."
            ),
            expected_output=(
                "A valid JSON object with the eight required keys."
            ),
            agent=postmortem_agent,
        )

        return Crew(
            agents=[triage_agent, evidence_agent, postmortem_agent],
            tasks=[triage_task, evidence_task, postmortem_task],
            process=Process.sequential,
            verbose=False,
        )

    # -----------------------------------------------------------------------
    # LLM configuration
    # -----------------------------------------------------------------------

    def _build_llm(self) -> Any:
        """
        Build a crewai.LLM object.

        Priority: GEMINI_API_KEY (Google Gemini) -> OPENAI_API_KEY -> Ollama -> None.
        Returns None when crewai is not installed.
        """
        if not _CREWAI_AVAILABLE:
            return None

        import os
        s = self.settings
        gemini_key = getattr(s, "gemini_api_key", "") or os.getenv("GEMINI_API_KEY", "")
        if gemini_key:
            model_name = getattr(s, "gemini_model", "gemini-3.5-flash-lite")
            return LLM(
                model=f"gemini/{model_name}",
                api_key=gemini_key,
            )

        if s.openai_api_key:
            return LLM(
                model=f"openai/{s.openai_model}",
                api_key=s.openai_api_key,
                base_url=s.openai_base_url,
            )

        # Ollama (no API key required; falls back if not running)
        return LLM(
            model=f"ollama/{s.ollama_llm_model}",
            base_url=s.ollama_base_url,
        )

    # -----------------------------------------------------------------------
    # Output parsing
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_inputs(brief: AegisIncidentBrief) -> dict[str, str]:
        return {"brief_json": brief.model_dump_json()}

    def _parse_output(
        self, output: Any, brief: AegisIncidentBrief
    ) -> CrewPostmortemReport:
        """
        Parse the CrewOutput from crew.kickoff() into a CrewPostmortemReport.

        CrewAI >= 0.61 returns a CrewOutput with a .raw attribute containing
        the string output of the final task.
        """
        raw: str = ""
        try:
            raw = output.raw if hasattr(output, "raw") else str(output)
        except Exception:
            raw = str(output)

        # Extract JSON from the raw output (may have surrounding text/fencing)
        data = self._extract_json(raw)

        risk_raw = data.get("risk_level", "medium")
        try:
            risk_level = ActionRiskLevel(risk_raw)
        except ValueError:
            risk_level = ActionRiskLevel.medium

        confidence = float(data.get("confidence", 0.70))
        confidence = max(0.0, min(1.0, confidence))

        return CrewPostmortemReport(
            investigation_id=brief.investigation_id,
            root_cause_hypothesis=str(
                data.get("root_cause_hypothesis", f"Root cause under investigation: {brief.title}")
            ),
            confidence=confidence,
            contributing_factors=list(data.get("contributing_factors", brief.mcp_evidence[:3])),
            timeline_summary=str(data.get("timeline_summary", " -> ".join(brief.timeline[:4]))),
            risk_level=risk_level,
            remediation_recommendation=str(
                data.get("remediation_recommendation", "Review evidence and apply standard runbook.")
            ),
            postmortem_summary=str(data.get("postmortem_summary", brief.description)),
            lessons_learned=list(data.get("lessons_learned", [])),
            crew_agents_used=_AGENT_NAMES,
            crew_demo_mode=False,
        )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Extract a JSON object from potentially decorated LLM output."""
        # Strip code fences
        stripped = text.strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
        if fence_match:
            stripped = fence_match.group(1).strip()

        # Find first { ... } block
        brace_match = re.search(r"\{[\s\S]*\}", stripped)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

        # Last resort: try full text as JSON
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {}

    # -----------------------------------------------------------------------
    # Demo / fallback report
    # -----------------------------------------------------------------------

    def _demo_report(
        self, brief: AegisIncidentBrief, reason: str = ""
    ) -> CrewPostmortemReport:
        """
        Return a labeled demo report when LLM credentials are absent.

        CrewAI Agent / Task / Crew objects are instantiated in self._build_crew()
        and crew.kickoff() is called before this fallback is reached, so the
        CrewAI framework code paths DO execute (they just fail at the LLM step).
        """
        risk_level = {
            "low": ActionRiskLevel.low,
            "medium": ActionRiskLevel.medium,
            "high": ActionRiskLevel.high,
            "critical": ActionRiskLevel.critical,
        }.get(brief.severity.value, ActionRiskLevel.medium)

        note = f"[DEMO MODE{': ' + reason[:80] if reason else ''}]"

        return CrewPostmortemReport(
            investigation_id=brief.investigation_id,
            root_cause_hypothesis=(
                f"{note} Based on {len(brief.mcp_evidence)} MCP signals and "
                f"{len(brief.a2a_findings)} A2A findings, the most likely root cause is "
                f"a {brief.domain.value}-domain failure related to: {brief.title}."
            ),
            confidence=0.55,
            contributing_factors=brief.mcp_evidence[:3] + brief.a2a_findings[:2],
            timeline_summary=f"{note} " + " | ".join(brief.timeline[:4]),
            risk_level=risk_level,
            remediation_recommendation=(
                f"{note} Apply standard {brief.domain.value} runbook. "
                "Escalate to domain owner if not resolved within 30 minutes."
            ),
            postmortem_summary=(
                f"{note} Incident '{brief.title}' investigated via ADK + CrewAI pipeline.  "
                f"MCP tool: {brief.mcp_tool_used}.  "
                f"A2A exchange: {brief.a2a_primary_agent} <-> {brief.a2a_peer_agent}."
            ),
            lessons_learned=[
                "Configure OPENAI_API_KEY or Ollama for full LLM-powered postmortem synthesis.",
                f"MCP evidence collected from: {brief.mcp_tool_used}.",
                f"A2A exchange completed between {brief.a2a_primary_agent} and {brief.a2a_peer_agent}.",
            ],
            crew_agents_used=_AGENT_NAMES,
            crew_demo_mode=True,
        )
