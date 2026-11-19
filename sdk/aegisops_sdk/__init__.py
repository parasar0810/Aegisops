"""AegisOps Python SDK (optional; plain HTTP works for any language).

    from aegisops_sdk import AegisOpsClient, AegisOpsMiddleware
    client = AegisOpsClient("https://aegisops.example.com", app_name="shopfront", ingest_key=os.environ["AEGISOPS_KEY"])
    app.add_middleware(AegisOpsMiddleware, client=client, service="shopfront-api")

Only depends on httpx. Reporting never raises into your request path.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from contextvars import ContextVar
from typing import Any

import httpx

__all__ = ["AegisOpsClient", "AegisOpsMiddleware", "correlation_id"]
__version__ = "1.0.0"

log = logging.getLogger("aegisops_sdk")
correlation_id: ContextVar[str | None] = ContextVar("aegisops_correlation_id", default=None)


class AegisOpsClient:
    def __init__(self, base_url: str, app_name: str, ingest_key: str | None = None,
                 admin_token: str | None = None, timeout: float = 3.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_name = app_name
        self.ingest_key = ingest_key
        self.admin_token = admin_token
        self._http = httpx.Client(timeout=timeout, transport=transport)

    # Registration needs an admin/sre JWT; returns the ingest key (store it as a secret).
    def register(self, manifest: dict[str, Any]) -> str:
        r = self._http.post(f"{self.base_url}/api/v1/applications/register", json=manifest,
                            headers={"Authorization": f"Bearer {self.admin_token}"})
        r.raise_for_status()
        self.ingest_key = r.json()["ingest_key"]
        return self.ingest_key

    def send_event(self, title: str, event_type: str = "alert", severity: str = "medium",
                   description: str = "", service: str | None = None, logs: list[str] | None = None,
                   metrics: dict[str, float] | None = None, labels: dict[str, str] | None = None) -> dict | None:
        payload = {"application": self.app_name, "event_type": event_type, "title": title[:300],
                   "severity": severity, "description": description[:4000], "service": service,
                   "logs": (logs or [])[:200], "metrics": metrics or {}, "labels": labels or {},
                   "correlation_id": correlation_id.get()}
        try:
            r = self._http.post(f"{self.base_url}/api/v1/incidents/webhook", json=payload,
                                headers={"X-AegisOps-Key": self.ingest_key or ""})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            log.warning("AegisOps event not delivered: %s", exc)
            return None

    def report_exception(self, exc: BaseException, service: str | None = None, severity: str = "medium") -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]
        self.send_event(f"{type(exc).__name__}: {exc}"[:300], event_type="exception", severity=severity,
                        service=service, logs=tb.splitlines()[-40:])

    def report_health(self, status: str, details: dict[str, Any] | None = None, service: str | None = None) -> None:
        self.send_event(f"health: {status}", event_type="health", severity="low", service=service,
                        description=str(details or {}))

    def report_deployment(self, version: str, service: str | None = None) -> None:
        self.send_event(f"Deployment completed: release {version}", event_type="deployment",
                        severity="low", service=service)


class AegisOpsMiddleware:
    """ASGI middleware: propagates X-Correlation-ID and reports unhandled exceptions."""

    def __init__(self, app, client: AegisOpsClient, service: str | None = None) -> None:
        self.app, self.client, self.service = app, client, service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        cid = headers.get(b"x-correlation-id", b"").decode() or uuid.uuid4().hex
        token = correlation_id.set(cid)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-correlation-id", cid.encode()))
            await send(message)
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # shortcut: sync HTTP call inside the event loop; fine for rare exceptions,
            # move to a background queue if exception volume is high.
            self.client.report_exception(exc, service=self.service, severity="high")
            raise
        finally:
            correlation_id.reset(token)
