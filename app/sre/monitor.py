"""Incident Detection: active health/metric polling of registered applications.

Complements webhook ingestion (Prometheus Alertmanager / app events). Enabled
with AEGIS_MONITOR_INTERVAL_SECONDS > 0.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.core.telemetry import SRE_APP_HEALTH
from app.sre.analysis import _thresholds
from app.sre.mcp_server import MCPToolbox
from app.sre.models import IncidentState, Severity
from app.sre.pipeline import SREOrchestrator
from app.sre.store import StateStore

logger = logging.getLogger(__name__)

_SEVERITY_BY_SIGNAL = {"error_rate": Severity.high, "memory_percent": Severity.high,
                       "cpu_percent": Severity.high, "p95_latency_seconds": Severity.medium,
                       "request_rate": Severity.medium, "db_pool_in_use_ratio": Severity.high}


async def detect_once(store: StateStore, orch: SREOrchestrator) -> list[str]:
    opened: list[str] = []
    toolbox = MCPToolbox(store)
    async with toolbox.session():
        for app in store.list_applications():
            health = await toolbox.call("check_http_health", url=str(app.health_check_url))
            code = health.data.get("status_code") if health.ok else None
            up = code is not None and code < 400
            SRE_APP_HEALTH.labels(application=app.name).set(1 if up else 0)
            app.last_health = {"status_code": code, "latency_ms": health.data.get("latency_ms"), "up": up}
            store.save_application(app)
            breaches: dict[str, float] = {}
            if app.monitoring.metrics_url or app.monitoring.prometheus_url:
                m = await toolbox.call("query_metrics", application=app.name)
                th = _thresholds(app)
                breaches = {k: v for k, v in (m.data.get("values", {}) if m.ok else {}).items()
                            if k in _SEVERITY_BY_SIGNAL and v > th[k]}
            if up and not breaches:
                continue
            severity = Severity.critical if not up else max(
                (_SEVERITY_BY_SIGNAL[k] for k in breaches), key=list(Severity).index)
            title = f"{app.name}: " + ("health check failing" if not up else
                                       "anomalous " + ", ".join(sorted(breaches)))
            state = IncidentState(application=app.name, title=title, severity=severity, source="aegis_monitor",
                                  description=f"Detected by AegisOps monitor. health={code} breaches={breaches}",
                                  initial_alert={"metrics": breaches, "labels": {"detector": "aegis_monitor"}})
            if orch.open_incident(state):
                opened.append(state.incident_id)
    return opened


async def monitor_loop(store: StateStore, orch: SREOrchestrator, tasks: set) -> None:
    interval = float(os.environ.get("AEGIS_MONITOR_INTERVAL_SECONDS", "0"))
    if interval <= 0:
        return
    logger.info("AegisOps monitor polling every %.0fs", interval)
    while True:
        try:
            for incident_id in await detect_once(store, orch):
                t = asyncio.create_task(orch.investigate(incident_id))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        except Exception:
            logger.exception("monitor iteration failed")
        await asyncio.sleep(interval)
