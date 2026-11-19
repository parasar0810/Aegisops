"""AegisOps MCP server: real, read-only diagnostic tools for monitored apps.

Tools reach live systems (HTTP health endpoints, Prometheus, metrics scrape,
application log endpoints, Docker, TCP database probes). Every tool:
  * validates its arguments (typed signatures -> MCP JSON schema),
  * only touches hosts belonging to a *registered* application (no SSRF),
  * enforces a timeout,
  * returns a ``ToolResult`` that the client re-validates before agents use it.

In-process agents connect through an MCP ClientSession over memory streams;
external MCP clients can run ``python -m app.sre.mcp_server`` (stdio).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from prometheus_client.parser import text_string_to_metric_families
from pydantic import BaseModel, Field

from app.core.telemetry import MCP_TOOL_CALLS_TOTAL, MCP_TOOL_DURATION
from app.sre.guardrails import sanitize_untrusted
from app.sre.store import StateStore

DEFAULT_TIMEOUT = 5.0
_PROMQL_SAFE = re.compile(r"^[\w\s\.\-\+\*/\(\)\[\]\{\}=~!\"',:<>|^%]{1,500}$")

# Tests inject an httpx transport (e.g. ASGITransport) here.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


class ToolResult(BaseModel):
    ok: bool
    tool: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=HTTP_TRANSPORT, follow_redirects=False)


def build_server(store: StateStore) -> FastMCP:
    mcp = FastMCP("aegisops-diagnostics")

    def app_or_raise(application: str):
        app = store.find_application(application)
        if app is None:
            raise ValueError(f"application '{application}' is not registered")
        return app

    def allowed_host(url: str) -> bool:
        host = urlparse(url).hostname
        hosts = set()
        for a in store.list_applications():
            for u in (a.base_url, a.health_check_url, a.monitoring.metrics_url,
                      a.monitoring.prometheus_url, a.monitoring.logs_url):
                if u:
                    hosts.add(urlparse(str(u)).hostname)
        return host in hosts

    async def timed(tool: str, coro) -> ToolResult:
        started = time.perf_counter()
        try:
            data = await asyncio.wait_for(coro, timeout=DEFAULT_TIMEOUT * 3)
            result = ToolResult(ok=True, tool=tool, data=data)
        except Exception as exc:  # tool errors become structured results, never crashes
            result = ToolResult(ok=False, tool=tool, error=f"{type(exc).__name__}: {exc}"[:500])
        elapsed = time.perf_counter() - started
        result.duration_ms = round(elapsed * 1000, 2)
        MCP_TOOL_CALLS_TOTAL.labels(tool=tool, mode="mcp", status="ok" if result.ok else "error").inc()
        MCP_TOOL_DURATION.labels(tool=tool, mode="mcp").observe(elapsed)
        return result

    @mcp.tool()
    async def check_http_health(url: str, timeout_seconds: float = DEFAULT_TIMEOUT) -> ToolResult:
        """GET a registered application's URL and report status code and latency."""
        async def run() -> dict[str, Any]:
            if not allowed_host(url):
                raise PermissionError("URL host is not a registered application host")
            t = min(max(timeout_seconds, 0.5), 15.0)
            started = time.perf_counter()
            try:
                async with _client(t) as c:
                    r = await c.get(url)
                body, flags = sanitize_untrusted(r.text, 500)
                return {"url": url, "status_code": r.status_code, "reachable": True,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                        "body_excerpt": body, "injection_flags": flags}
            except httpx.TransportError as exc:
                return {"url": url, "status_code": None, "reachable": False,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                        "error": type(exc).__name__}
        return await timed("check_http_health", run())

    @mcp.tool()
    async def query_metrics(application: str) -> ToolResult:
        """Read the application's configured metric signals (Prometheus or /metrics scrape)."""
        async def run() -> dict[str, Any]:
            app = app_or_raise(application)
            mon = app.monitoring
            queries = mon.metric_queries
            if not queries:
                raise ValueError("no metric_queries configured for application")
            values: dict[str, float] = {}
            async with _client(DEFAULT_TIMEOUT) as c:
                if mon.prometheus_url:
                    for signal, promql in queries.items():
                        r = await c.get(f"{str(mon.prometheus_url).rstrip('/')}/api/v1/query",
                                        params={"query": promql})
                        r.raise_for_status()
                        res = r.json().get("data", {}).get("result", [])
                        if res:
                            values[signal] = float(res[0]["value"][1])
                    return {"mode": "prometheus", "values": values, "source": str(mon.prometheus_url)}
                if not mon.metrics_url:
                    raise ValueError("neither prometheus_url nor metrics_url configured")
                r = await c.get(str(mon.metrics_url))
                r.raise_for_status()
            samples: dict[str, float] = {}
            for fam in text_string_to_metric_families(r.text):
                for s in fam.samples:
                    samples.setdefault(s.name, float(s.value))
            for signal, metric in queries.items():
                if metric in samples:
                    values[signal] = samples[metric]
            return {"mode": "scrape", "values": values, "source": str(mon.metrics_url)}
        return await timed("query_metrics", run())

    @mcp.tool()
    async def prometheus_query(application: str, promql: str) -> ToolResult:
        """Run an instant PromQL query against the application's Prometheus."""
        async def run() -> dict[str, Any]:
            app = app_or_raise(application)
            if not app.monitoring.prometheus_url:
                raise ValueError("application has no prometheus_url")
            if not _PROMQL_SAFE.match(promql):
                raise ValueError("promql contains disallowed characters")
            async with _client(DEFAULT_TIMEOUT) as c:
                r = await c.get(f"{str(app.monitoring.prometheus_url).rstrip('/')}/api/v1/query",
                                params={"query": promql})
                r.raise_for_status()
            return {"result": r.json().get("data", {}).get("result", [])[:20]}
        return await timed("prometheus_query", run())

    @mcp.tool()
    async def search_logs(application: str, query: str = "", level: str = "",
                          service: str = "", since_minutes: int = 30, limit: int = 50) -> ToolResult:
        """Search recent application logs (app log endpoint + ingested events)."""
        async def run() -> dict[str, Any]:
            app = app_or_raise(application)
            lim = min(max(limit, 1), 200)
            cutoff = datetime.now(UTC) - timedelta(minutes=min(max(since_minutes, 1), 1440))
            lines: list[dict[str, Any]] = list(store.read_logs(app.name))
            if app.monitoring.logs_url:
                async with _client(DEFAULT_TIMEOUT) as c:
                    r = await c.get(str(app.monitoring.logs_url), params={"limit": 500})
                    r.raise_for_status()
                    payload = r.json()
                    lines += payload if isinstance(payload, list) else payload.get("logs", [])
            out, flagged = [], 0
            for ln in lines:
                if not isinstance(ln, dict):
                    continue
                msg = str(ln.get("message", ""))
                ts = ln.get("timestamp")
                try:
                    if ts and datetime.fromisoformat(str(ts).replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
                if level and str(ln.get("level", "")).lower() != level.lower():
                    continue
                if service and ln.get("service") not in (service, None):
                    continue
                if query and query.lower() not in msg.lower():
                    continue
                safe, flags = sanitize_untrusted(msg, 400)
                flagged += bool(flags)
                out.append({"timestamp": ts, "level": ln.get("level"), "service": ln.get("service"),
                            "message": safe})
            return {"count": len(out), "logs": out[-lim:], "injection_flagged": flagged}
        return await timed("search_logs", run())

    @mcp.tool()
    async def inspect_container(application: str) -> ToolResult:
        """Inspect the application's Docker container state and resource usage (read-only)."""
        async def run() -> dict[str, Any]:
            app = app_or_raise(application)
            name = app.monitoring.container_name
            if not name:
                raise ValueError("no container_name configured")
            if not shutil.which("docker"):
                return {"available": False, "reason": "docker CLI not present on AegisOps host"}

            async def docker(*args: str) -> str:
                # Fixed argv, validated container name, no shell.
                proc = await asyncio.create_subprocess_exec(
                    "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode:
                    raise RuntimeError(err.decode()[:200])
                return out.decode()

            state = json.loads(await docker("inspect", "--format", "{{json .State}}", name))
            stats = (await docker("stats", "--no-stream", "--format", "{{json .}}", name)).strip()
            return {"available": True, "status": state.get("Status"), "restarting": state.get("Restarting"),
                    "oom_killed": state.get("OOMKilled"), "exit_code": state.get("ExitCode"),
                    "stats": json.loads(stats) if stats else {}}
        return await timed("inspect_container", run())

    @mcp.tool()
    async def check_database(application: str) -> ToolResult:
        """TCP reachability probe of the application's registered database endpoint."""
        async def run() -> dict[str, Any]:
            app = app_or_raise(application)
            host, port = app.monitoring.database_host, app.monitoring.database_port
            if not host or not port:
                raise ValueError("no database_host/database_port configured")
            started = time.perf_counter()
            try:
                await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
                    None, lambda: socket.create_connection((host, port), timeout=3).close()), 4)
                ok = True
            except Exception:
                ok = False
            return {"host": host, "port": port, "reachable": ok,
                    "connect_ms": round((time.perf_counter() - started) * 1000, 1)}
        return await timed("check_database", run())

    return mcp


class MCPToolbox:
    """MCP client used by agents. Re-validates every tool result."""

    def __init__(self, store: StateStore) -> None:
        self.server = build_server(store)
        self.calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def session(self) -> AsyncIterator[MCPToolbox]:
        async with create_connected_server_and_client_session(self.server) as s:
            self._session = s
            yield self

    async def list_tools(self) -> list[str]:
        return [t.name for t in (await self._session.list_tools()).tools]

    async def call(self, name: str, **arguments: Any) -> ToolResult:
        res = await self._session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=20))
        if res.isError or not res.structuredContent:
            text = " ".join(getattr(c, "text", "") for c in res.content)[:300]
            result = ToolResult(ok=False, tool=name, error=text or "tool error")
        else:
            payload = res.structuredContent.get("result", res.structuredContent)
            result = ToolResult.model_validate(payload)
        self.calls.append({"tool": name, "args": arguments, "ok": result.ok, "duration_ms": result.duration_ms})
        return result


if __name__ == "__main__":  # stdio transport for external MCP clients
    build_server(StateStore()).run()
