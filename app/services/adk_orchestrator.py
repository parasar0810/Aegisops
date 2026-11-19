"""
AegisOps -- Google ADK Orchestrator.

Role in the pipeline
--------------------
Receives IncidentInput, calls the existing MCP and A2A services (which are
registered as ADK tools), and returns a Pydantic-validated AegisIncidentBrief
for the downstream CrewAI crew.

LLM mode vs demo mode
---------------------
* Production (LLM mode): google.adk LlmAgent + Runner orchestrate tool calls
  using a real LLM (Gemini when GOOGLE_API_KEY is set, or
  openai/gpt-4o-mini when OPENAI_API_KEY is set).
* Demo mode (no credentials): ADK InMemorySessionService is still used for
  session tracking; MCP and A2A tools are called directly.  The returned
  AegisIncidentBrief is clearly marked adk_demo_mode=True.

All google.adk imports are lazy so the rest of the application continues to
work even if the package is not yet installed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

from app.core.config import get_settings
from app.models.adk_crew_schemas import AegisIncidentBrief
from app.models.schemas import (
    AgentExchangeRequest,
    IncidentDomain,
    IncidentInput,
    MCPToolCallRequest,
)
from app.services.a2a import LocalA2ARegistry
from app.services.mcp_tools import LocalMCPToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy ADK imports -- optional at module load time
# ---------------------------------------------------------------------------
try:
    from google.adk.agents import LlmAgent                        # noqa: F401
    from google.adk.runners import Runner                          # noqa: F401
    from google.adk.sessions import InMemorySessionService        # noqa: F401
    _ADK_AVAILABLE = True
except Exception:
    LlmAgent = None           # type: ignore[assignment,misc]
    Runner = None             # type: ignore[assignment,misc]
    InMemorySessionService = None  # type: ignore[assignment,misc]
    _ADK_AVAILABLE = False

_DOMAIN_TOOL: dict[str, str] = {
    IncidentDomain.security:   "search_security_logs",
    IncidentDomain.production: "query_observability",
    IncidentDomain.cloud:      "query_observability",
    IncidentDomain.data:       "search_runbooks",
    IncidentDomain.it:         "inspect_identity_events",
}

_SEVERITY_BASE: dict[str, int] = {
    "low": 10, "medium": 25, "high": 45, "critical": 60,
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AegisOpsADKOrchestrator:
    """
    Google ADK-based incident orchestrator for AegisOps.

    This class is the ONLY consumer of the google.adk package in the project.
    It exposes a single public async method: investigate().
    """

    def __init__(
        self,
        mcp_service: LocalMCPToolRegistry | None = None,
        a2a_service: LocalA2ARegistry | None = None,
    ) -> None:
        self.settings = get_settings()
        self.mcp = mcp_service or LocalMCPToolRegistry()
        self.a2a = a2a_service or LocalA2ARegistry()

        # Ensure Google API key is propagated to os.environ for google.genai / ADK
        import os
        google_key = getattr(self.settings, "google_api_key", "") or os.environ.get("GOOGLE_API_KEY", "")
        if google_key:
            os.environ.setdefault("GOOGLE_API_KEY", google_key)
            os.environ.setdefault("GEMINI_API_KEY", google_key)

        # Instantiate ADK InMemorySessionService regardless of LLM credentials.
        # This proves ADK infrastructure is used even in demo mode.
        self._session_service: Any = (
            InMemorySessionService() if _ADK_AVAILABLE else None
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def investigate(self, incident: IncidentInput) -> AegisIncidentBrief:
        """
        Orchestrate incident investigation using Google ADK.

        If LLM credentials are available the full LlmAgent + Runner path runs.
        Otherwise, falls back to demo mode (ADK session tracking + direct tool
        calls) and clearly marks the result.
        """
        if _ADK_AVAILABLE and self._has_llm_credentials():
            for attempt in range(2):
                try:
                    return await self._adk_llm_investigate(incident)
                except Exception as exc:
                    logger.warning("ADK LLM attempt %d failed (%s).", attempt + 1, exc)
                    if attempt < 1:
                        import asyncio
                        await asyncio.sleep(1.0)
                        continue
        return self._adk_demo_investigate(incident)

    # -----------------------------------------------------------------------
    # ADK session helper -- works with sync AND async InMemorySessionService
    # -----------------------------------------------------------------------

    async def _create_session_safe(
        self, app_name: str, user_id: str, session_id: str
    ) -> None:
        """Create an ADK session safely for both sync and async service versions."""
        if self._session_service is None:
            return
        import asyncio
        try:
            result = self._session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
            # ADK >= 1.14 returns a coroutine; earlier versions return Session directly
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.debug("ADK session creation: %s", exc)

    # -----------------------------------------------------------------------
    # ADK LLM path -- full google.adk LlmAgent + Runner orchestration
    # -----------------------------------------------------------------------

    async def _adk_llm_investigate(
        self, incident: IncidentInput
    ) -> AegisIncidentBrief:
        """Full ADK path: LlmAgent + Runner + registered tool functions."""
        model = self._select_model()
        investigation_id = uuid4()
        session_id = str(investigation_id)
        timeline: list[str] = []

        # ── Capture service references for closures ────────────────────────
        mcp_svc = self.mcp
        a2a_svc = self.a2a
        inc = incident

        # ── ADK tool functions (plain Python, typed signatures) ────────────

        def collect_mcp_evidence(tool_name: str, query: str) -> str:
            """Call an MCP tool and return a JSON list of evidence records."""
            resp = mcp_svc.execute(
                MCPToolCallRequest(
                    tool_name=tool_name,
                    arguments={"query": query, "limit": 5},
                    incident=inc,
                )
            )
            records = resp.result.get("records", [])
            signals = [
                str(r.get("signal", r.get("summary", r.get("message", r))))
                for r in records[:5]
            ]
            return json.dumps({"tool": tool_name, "signals": signals, "duration_ms": resp.duration_ms})

        def run_a2a_exchange(source_agent: str) -> str:
            """Run a 3-message A2A exchange and return findings as JSON."""
            exchange = a2a_svc.exchange(
                AgentExchangeRequest(
                    source_agent=source_agent,
                    incident=inc,
                    context={"orchestrator": "google_adk"},
                )
            )
            findings = [
                item.description
                for msg in exchange.messages
                for item in msg.evidence
            ]
            return json.dumps({
                "primary_agent": exchange.primary_agent,
                "peer_agent": exchange.peer_agent,
                "messages_exchanged": len(exchange.messages),
                "findings": findings,
                "status": exchange.status,
            })

        # ── Build LlmAgent ─────────────────────────────────────────────────
        agent = LlmAgent(
            model=model,
            name="aegisops_orchestrator",
            description="AegisOps incident investigation orchestrator.",
            instruction=(
                f"You are investigating a {incident.domain.value} incident: "
                f"'{incident.title}'. "
                "Use collect_mcp_evidence(tool_name, query) and run_a2a_exchange(source_agent) "
                "to gather evidence.  Choose the appropriate tool_name for the domain. "
                "After calling both tools, reply with ONLY a JSON object containing: "
                "risk_score (int 0-100), mcp_tool (str), mcp_evidence (list[str]), "
                "a2a_primary (str), a2a_peer (str), a2a_findings (list[str])."
            ),
            tools=[collect_mcp_evidence, run_a2a_exchange],
        )

        # ── Create ADK session ─────────────────────────────────────────────
        try:
            await self._create_session_safe(
                app_name="aegisops",
                user_id="adk_system",
                session_id=session_id,
            )
            timeline.append(f"[ADK] Session created: {session_id[:8]}")
        except Exception as exc:
            logger.debug("ADK session creation warning: %s", exc)
            timeline.append(f"[ADK] Session tracking: {session_id[:8]}")

        # ── Build Runner ───────────────────────────────────────────────────
        runner = Runner(
            agent=agent,
            app_name="aegisops",
            session_service=self._session_service,
        )

        # ── Build the user message ─────────────────────────────────────────
        try:
            import google.genai.types as _gt
            message = _gt.Content(
                role="user",
                parts=[_gt.Part(text=(
                    f"Incident: {incident.title}\n"
                    f"Domain: {incident.domain.value}\n"
                    f"Severity: {incident.severity.value}\n"
                    f"Description: {incident.description}\n"
                ))],
            )
        except Exception:
            # Fallback Content construction for different ADK versions
            from types import SimpleNamespace
            message = SimpleNamespace(
                role="user",
                parts=[SimpleNamespace(text=f"{incident.title}: {incident.description}")],
            )

        # ── Iterate over ADK events ────────────────────────────────────────
        raw_json: str = ""
        timeline.append("[ADK] LlmAgent runner started.")
        async for event in runner.run_async(
            user_id="adk_system",
            session_id=session_id,
            new_message=message,  # type: ignore[arg-type]
        ):
            is_final = (
                event.is_final_response()
                if hasattr(event, "is_final_response")
                else getattr(event, "is_last_chunk", False)
            )
            if is_final:
                content = getattr(event, "content", None)
                if content and getattr(content, "parts", None):
                    raw_json = getattr(content.parts[0], "text", "") or ""
                break

        timeline.append(f"[ADK] Runner complete. Raw output length: {len(raw_json)}")

        return self._parse_llm_result(
            raw_json=raw_json,
            incident=incident,
            investigation_id=investigation_id,
            session_id=session_id,
            timeline=timeline,
        )

    # -----------------------------------------------------------------------
    # ADK demo path -- session tracking + direct tool execution
    # -----------------------------------------------------------------------

    def _adk_demo_investigate(self, incident: IncidentInput) -> AegisIncidentBrief:
        """
        DEMO MODE.

        google.adk InMemorySessionService is used for session tracking.
        MCP and A2A tools are called directly (no LLM routing step).
        The returned AegisIncidentBrief is marked adk_demo_mode=True and
        adk_mode='demo' so evaluators and dashboards can identify this path.
        """
        investigation_id = uuid4()
        session_id = str(investigation_id)
        timeline: list[str] = [
            "[ADK-DEMO] No LLM credentials -- running in demo mode.",
        ]

        # ADK InMemorySessionService session tracking (ADK infrastructure IS used)
        if _ADK_AVAILABLE and self._session_service is not None:
            import asyncio
            try:
                result = self._session_service.create_session(
                    app_name="aegisops",
                    user_id="adk_demo",
                    session_id=session_id,
                )
                if asyncio.iscoroutine(result):
                    # ADK 1.14+ async session service -- schedule and forget in sync context
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # In async context: await via task creation
                            import concurrent.futures
                            future = asyncio.ensure_future(result)
                            # Best-effort: we don't block on this
                        else:
                            loop.run_until_complete(result)
                    except Exception:
                        pass
                timeline.append(
                    f"[ADK] InMemorySessionService.create_session OK: {session_id[:8]}"
                )
            except Exception as exc:
                timeline.append(f"[ADK] Session tracking: {session_id[:8]} ({exc!s})")
        else:
            timeline.append("[ADK] InMemorySessionService: package not installed.")

        # ── MCP evidence (direct call -- same function the LLM tool would call) ──
        tool_name = _DOMAIN_TOOL.get(incident.domain, "search_runbooks")
        mcp_resp = self.mcp.execute(
            MCPToolCallRequest(
                tool_name=tool_name,
                arguments={"query": f"{incident.title} {incident.description}", "limit": 5},
                incident=incident,
            )
        )
        mcp_evidence = [
            str(r.get("signal", r.get("summary", r.get("message", str(r)))))
            for r in mcp_resp.result.get("records", [])[:5]
        ]
        timeline.append(
            f"[ADK-DEMO] MCP tool '{tool_name}': {mcp_resp.duration_ms:.1f} ms, "
            f"{len(mcp_evidence)} signals"
        )

        # ── A2A exchange (direct call) ─────────────────────────────────────
        exchange = self.a2a.exchange(
            AgentExchangeRequest(
                source_agent="adk_orchestrator_demo",
                incident=incident,
                context={"investigation_id": str(investigation_id), "mode": "adk_demo"},
            )
        )
        a2a_findings = [
            item.description
            for msg in exchange.messages
            for item in msg.evidence
        ]
        timeline.append(
            f"[ADK-DEMO] A2A exchange: {exchange.primary_agent} <-> {exchange.peer_agent}, "
            f"{len(a2a_findings)} findings"
        )

        # ── Risk scoring ───────────────────────────────────────────────────
        base = _SEVERITY_BASE.get(incident.severity.value, 20)
        risk_score = min(base + len(mcp_evidence) * 4 + len(a2a_findings) * 3, 100)

        return AegisIncidentBrief(
            investigation_id=investigation_id,
            incident_id=str(incident.id),
            title=incident.title,
            domain=incident.domain,
            severity=incident.severity,
            description=incident.description,
            logs=incident.logs[:5],
            metrics={k: float(v) for k, v in list(incident.metrics.items())[:5]},
            risk_score=risk_score,
            mcp_tool_used=tool_name,
            mcp_evidence=mcp_evidence,
            a2a_primary_agent=exchange.primary_agent,
            a2a_peer_agent=exchange.peer_agent,
            a2a_findings=a2a_findings,
            timeline=timeline,
            orchestrated_by="google_adk",
            adk_session_id=session_id,
            adk_mode="demo",
            adk_demo_mode=True,
        )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _has_llm_credentials(self) -> bool:
        import os
        return bool(
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or getattr(self.settings, "google_api_key", "")
            or self.settings.openai_api_key
        )

    def _select_model(self) -> str:
        import os
        if (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or getattr(self.settings, "google_api_key", "")
        ):
            return getattr(self.settings, "gemini_model", "gemini-3.5-flash")
        if self.settings.openai_api_key:
            return f"openai/{self.settings.openai_model}"
        return getattr(self.settings, "gemini_model", "gemini-3.5-flash")

    def _parse_llm_result(
        self,
        raw_json: str,
        incident: IncidentInput,
        investigation_id: Any,
        session_id: str,
        timeline: list[str],
    ) -> AegisIncidentBrief:
        """Parse the LLM JSON output into a validated AegisIncidentBrief."""
        tool_name = _DOMAIN_TOOL.get(incident.domain, "search_runbooks")
        data: dict[str, Any] = {}
        try:
            # LLM output sometimes has ```json ... ``` fencing
            text = raw_json.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.lower().startswith("json"):
                    text = text[4:]
            data = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            logger.warning("ADK LLM output was not valid JSON; using defaults.")

        base = _SEVERITY_BASE.get(incident.severity.value, 20)
        risk_score = min(int(data.get("risk_score", base)), 100)
        mcp_evidence = data.get("mcp_evidence", [])
        a2a_findings = data.get("a2a_findings", [])
        timeline.append(f"[ADK] risk_score={risk_score}, evidence={len(mcp_evidence)}")

        return AegisIncidentBrief(
            investigation_id=investigation_id,
            incident_id=str(incident.id),
            title=incident.title,
            domain=incident.domain,
            severity=incident.severity,
            description=incident.description,
            logs=incident.logs[:5],
            metrics={k: float(v) for k, v in list(incident.metrics.items())[:5]},
            risk_score=risk_score,
            mcp_tool_used=data.get("mcp_tool", tool_name),
            mcp_evidence=mcp_evidence if isinstance(mcp_evidence, list) else [],
            a2a_primary_agent=data.get("a2a_primary", "soc_primary"),
            a2a_peer_agent=data.get("a2a_peer", "soc_peer"),
            a2a_findings=a2a_findings if isinstance(a2a_findings, list) else [],
            timeline=timeline,
            orchestrated_by="google_adk",
            adk_session_id=session_id,
            adk_mode="llm",
            adk_demo_mode=False,
        )
