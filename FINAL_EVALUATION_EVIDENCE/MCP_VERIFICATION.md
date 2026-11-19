# MCP Verification

## Overview

The project implements **two distinct MCP layers**:

1. **SRE MCP Server** (`app/sre/mcp_server.py`) — a **real, standards-compliant MCP server** using the `mcp` SDK's `FastMCP`. Tools touch live systems (HTTP, Prometheus, Docker, TCP database probes).
2. **Local MCP Tool Registry** (`app/services/mcp_tools.py`) — a **local simulation layer** for the legacy SOC pipeline. It mirrors MCP's discoverable-manifest + structured-call interface but executes locally without network calls (dry-run / synthetic results).

---

## 1. SRE MCP Server — Real Implementation

**File:** `app/sre/mcp_server.py`

### Transport and Protocol

```python
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

mcp = FastMCP("aegisops-diagnostics")
```

- **In-process transport:** `create_connected_server_and_client_session` (memory streams) — used by `MCPToolbox` inside the pipeline.
- **Stdio transport:** External MCP clients can connect by running `python -m app.sre.mcp_server`.
- The `MCPToolbox` class wraps a `ClientSession` to call tools via the MCP protocol; it does **not** call tool functions directly.

### Real MCP Tools

| Tool Name | External Service Touched | How |
|---|---|---|
| `check_http_health` | Registered application's HTTP endpoint | `httpx.AsyncClient.get(url)` with SSRF guard |
| `query_metrics` | Application's `/metrics` Prometheus scrape endpoint | `httpx.AsyncClient.get(metrics_url)` → `prometheus_client.parser` |
| `run_promql` | Prometheus server (`prometheus_url`) | `httpx.AsyncClient.get(.../api/v1/query_range)` |
| `search_logs` | Application's log endpoint (`logs_url`) | `httpx.AsyncClient.get(logs_url)` |
| `inspect_container` | Docker socket via `docker inspect` subprocess | `shutil.which("docker")` + `asyncio` subprocess |
| `check_database` | Database TCP port (`database_host`:`database_port`) | `socket.create_connection()` TCP probe |

**All tools are genuinely connected to external services when those services exist.** No tool is simulated in the SRE server.

### SSRF Protection

```python
def allowed_host(url: str) -> bool:
    host = urlparse(url).hostname
    hosts = set()
    for a in store.list_applications():
        for u in (a.base_url, a.health_check_url, a.monitoring.metrics_url, ...):
            if u:
                hosts.add(urlparse(str(u)).hostname)
    return host in hosts
```

Only hosts belonging to a **registered** application are reachable. Any URL with a non-registered host raises `PermissionError`.

### PromQL Input Validation

```python
_PROMQL_SAFE = re.compile(r"^[\w\s\.\-\+\*/\(\)\[\]\{\}=~!\"',:<>|^%]{1,500}$")
```

PromQL expressions are validated against a whitelist regex before being sent to Prometheus.

### Tool Result Schema

```python
class ToolResult(BaseModel):
    ok: bool
    tool: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
```

Every tool returns a `ToolResult`. Errors become structured results, never unhandled exceptions.

### Prometheus Instrumentation (Real-time)

Each tool call updates:
- `incident_mcp_tool_calls_total{tool, mode="mcp", status}` — Counter
- `incident_mcp_tool_duration_seconds{tool, mode="mcp"}` — Histogram

---

## 2. Local MCP Tool Registry — Simulated

**File:** `app/services/mcp_tools.py`

### Nature of Simulation

```python
class LocalMCPToolRegistry:
    """Local MCP-style tool registry with no-cost dry-run execution.

    The API shape mirrors the important MCP ideas for this project: tools are
    discoverable through manifests and executable through a single structured
    call interface. Real vendors can be enabled later by swapping handlers when
    credentials are present.
    """
```

This is an **in-process simulation**. Tools return synthetic but schema-valid responses. No network calls are made. It is used exclusively by the **legacy SOC pipeline** (`adk_orchestrator.py`, `a2a.py`).

### Tools Registered (All Simulated)

| Tool Name | Domains | External Integration Stub |
|---|---|---|
| `search_runbooks` | All | None (local RAG-style lookup) |
| `search_security_logs` | security | Splunk, Microsoft Sentinel (stubbed) |
| `query_observability` | production, cloud | Datadog, Grafana Loki (stubbed) |
| `inspect_identity_events` | it | Okta (stubbed) |
| `query_cloud_metrics` | cloud | CloudWatch (stubbed) |
| `threat_intel_lookup` | security | CrowdStrike (stubbed) |
| `check_data_pipeline` | data | None |

### Instrumentation

The local registry also updates the same Prometheus metrics:
```python
MCP_TOOL_CALLS_TOTAL.labels(tool=manifest.name, mode=manifest.mode, status="ok").inc()
MCP_TOOL_DURATION.labels(tool=manifest.name, mode=manifest.mode).observe(duration)
```

---

## 3. MCP Client Usage (SRE Pipeline)

**File:** `app/sre/mcp_server.py` — `MCPToolbox`

```python
class MCPToolbox:
    """In-process MCP client for the SRE pipeline."""

    async def __aenter__(self):
        read, write, read_c, write_c = create_connected_server_and_client_session(self._server)
        self._session = ClientSession(read_c, write_c)
        await self._session.initialize()
        return self

    async def call(self, tool: str, **kwargs) -> ToolResult:
        result = await self._session.call_tool(tool, kwargs)
        # ... parse and validate ToolResult ...
```

The `MCPToolbox` communicates with the `FastMCP` server over **in-memory streams** (bidirectional async pipes), which is a valid MCP transport per the specification. This allows full MCP protocol compliance (tool discovery, schema validation, session management) without network overhead.

---

## 4. Summary

| Aspect | SRE MCP Server | Local MCP Registry |
|---|---|---|
| Package | `mcp` (FastMCP) | Custom implementation |
| Transport | Memory streams (in-process) + stdio (external) | None — direct Python calls |
| Tools | 6 real tools touching live HTTP/Prometheus/Docker/TCP | 7 simulated tools with synthetic results |
| External connectivity | Yes — requires running services | No |
| Used by | SRE pipeline (`app/sre/pipeline.py`) | Legacy SOC pipeline (`adk_orchestrator.py`) |
| Prometheus metrics | Yes | Yes |

