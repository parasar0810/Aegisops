"""Synthetic demo-target application for the AegisOps self-check (not ShopFront).

An independent FastAPI service (no AegisOps imports) with controllable faults:
  POST /chaos/{fault}   fault in: errors, cpu, memory, slow, db, down, traffic, bad_deploy
  POST /chaos/reset
Remediation hooks AegisOps may call (Bearer DEMO_ADMIN_TOKEN):
  POST /admin/restart, /admin/rollback, /admin/clear_cache, /admin/reset_db_pool, /admin/enable_rate_limit
Telemetry: GET /health, GET /metrics (Prometheus), GET /logs/recent (JSON)
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

ADMIN_TOKEN = os.environ.get("DEMO_ADMIN_TOKEN", "demo-admin-token")
MEMORY_BUDGET_MB = float(os.environ.get("DEMO_MEMORY_BUDGET_MB", "256"))
FAULTS = {"errors", "cpu", "memory", "slow", "db", "down", "traffic", "bad_deploy"}

reg = CollectorRegistry()
REQS = Counter("shop_http_requests_total", "Requests", ["status"], registry=reg)
G = {name: Gauge(name, doc, registry=reg) for name, doc in {
    "shop_cpu_percent": "Process CPU utilisation, percent of one core (measured)",
    "shop_memory_percent": "Allocated memory as percent of the configured budget (measured)",
    "shop_error_rate": "5xx ratio of /api requests over the last 60s",
    "shop_p95_latency_seconds": "p95 /api request latency over the last 60s",
    "shop_request_rate": "Requests per second over the last 60s",
    "shop_db_pool_in_use_ratio": "Database connection pool utilisation",
    "shop_seconds_since_deploy": "Seconds since the last deployment",
}.items()}

_lock = threading.Lock()
state: dict = {"faults": set(), "deployed_at": time.time() - 86400, "version": "v1.4.2", "ballast": []}
_window: deque[tuple[float, int, float]] = deque()  # (ts, status, latency)
_logs: deque[dict] = deque(maxlen=2000)
_cpu = {"wall": time.time(), "proc": time.process_time(), "pct": 0.0}
_burner: list[threading.Event] = []


def log(level: str, message: str, service: str = "demo-api") -> None:
    _logs.append({"timestamp": datetime.now(UTC).isoformat(), "level": level, "service": service, "message": message})


def _burn(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(i * i for i in range(20000))


def apply_fault(fault: str) -> None:
    with _lock:
        state["faults"].add(fault)
    if fault == "cpu" and not _burner:
        ev = threading.Event()
        threading.Thread(target=_burn, args=(ev,), daemon=True).start()
        _burner.append(ev)
        log("warning", "worker pool saturated: request queue depth 140")
    if fault == "memory":
        state["ballast"] = [bytearray(1024 * 1024) for _ in range(int(MEMORY_BUDGET_MB * 0.93))]
        log("error", "MemoryError risk: heap usage 93% of budget; GC thrashing in product-image cache")
    if fault == "bad_deploy":
        state["deployed_at"], state["version"] = time.time(), "v1.5.0"
        log("info", "Deployment completed: release v1.5.0 rolled out to demo-api")
    if fault == "db":
        log("error", "sqlalchemy.exc.TimeoutError: QueuePool limit of size 20 overflow 0 reached, "
                     "connection pool exhausted", "demo-db-client")
    if fault == "traffic":
        log("warning", "rate limit warnings: 429 Too Many Requests from 312 client IPs (bot pattern)")


def clear_faults(which: set[str] | None = None) -> None:
    with _lock:
        targets = set(state["faults"]) if which is None else which & state["faults"]
        state["faults"] -= targets
    if "cpu" in targets:
        for ev in _burner:
            ev.set()
        _burner.clear()
    if "memory" in targets:
        state["ballast"] = []


def _record(status: int, latency: float) -> None:
    now = time.time()
    _window.append((now, status, latency))
    while _window and now - _window[0][0] > 60:
        _window.popleft()
    REQS.labels(status=str(status)).inc()


def refresh_gauges() -> None:
    now_w, now_p = time.time(), time.process_time()
    dw = now_w - _cpu["wall"]
    if dw >= 0.5:
        _cpu["pct"] = min(100.0, 100 * (now_p - _cpu["proc"]) / dw)
        _cpu.update(wall=now_w, proc=now_p)
    f = state["faults"]
    window = list(_window)
    lat = sorted(w[2] for w in window) or [0.0]
    G["shop_cpu_percent"].set(_cpu["pct"])
    G["shop_memory_percent"].set(100 * len(state["ballast"]) / MEMORY_BUDGET_MB)
    G["shop_error_rate"].set(sum(1 for w in window if w[1] >= 500) / len(window) if window else 0.0)
    G["shop_p95_latency_seconds"].set(lat[int(0.95 * (len(lat) - 1))])
    # shortcut: the traffic fault scales the measured rate instead of generating real flood load.
    G["shop_request_rate"].set((len(window) / 60.0) * (40 if "traffic" in f else 1))
    G["shop_db_pool_in_use_ratio"].set(1.0 if "db" in f else 0.25)
    G["shop_seconds_since_deploy"].set(now_w - state["deployed_at"])


def _background_traffic() -> None:
    """Steady synthetic user traffic so the error/latency windows are populated."""
    import httpx
    port = int(os.environ.get("PORT", "9000"))
    while True:
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/products", timeout=10)
        except Exception:
            pass
        time.sleep(0.25)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.environ.get("DEMO_SYNTHETIC_TRAFFIC", "true") == "true":
        for _ in range(3):
            threading.Thread(target=_background_traffic, daemon=True).start()
    log("info", "demo-api started")
    yield


app = FastAPI(title="AegisOps demo-target app", lifespan=lifespan)


@app.middleware("http")
async def telemetry(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        _record(response.status_code, time.perf_counter() - started)
    return response


@app.get("/api/products")
async def products():
    f = state["faults"]
    if "down" in f:
        raise HTTPException(503, "service unavailable")
    if "slow" in f:
        await asyncio.sleep(random.uniform(2.5, 4.0))
    elif "memory" in f or "cpu" in f:
        await asyncio.sleep(random.uniform(0.5, 2.5))
    if "db" in f and random.random() < 0.8:
        log("error", "OperationalError: could not connect to database: connection pool exhausted (20/20 in use)",
            "demo-db-client")
        raise HTTPException(500, "database error")
    if ("errors" in f or "bad_deploy" in f) and random.random() < 0.5:
        log("error", f"Traceback (most recent call last): KeyError 'price_cents' in render_product ({state['version']})")
        raise HTTPException(500, "internal server error")
    return {"version": state["version"], "products": [{"id": i, "name": f"Item {i}"} for i in range(5)]}


@app.get("/health")
def health():
    f = state["faults"]
    if "down" in f:
        return Response('{"status":"down"}', status_code=503, media_type="application/json")
    if "db" in f:
        return Response('{"status":"degraded","database":"unreachable"}', status_code=503,
                        media_type="application/json")
    return {"status": "ok", "version": state["version"], "deployed_at": state["deployed_at"]}


@app.get("/metrics")
def metrics():
    refresh_gauges()
    return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)


@app.get("/logs/recent")
def recent_logs(limit: int = 200):
    return list(_logs)[-min(max(limit, 1), 2000):]


@app.post("/chaos/{fault}")
def chaos(fault: str):
    if fault == "reset":  # back to a clean baseline: no faults, old release, fresh logs
        clear_faults()
        state["deployed_at"], state["version"] = time.time() - 86400, "v1.4.2"
        _window.clear()
        _logs.clear()
        return {"faults": []}
    if fault not in FAULTS:
        raise HTTPException(404, f"unknown fault; choose from {sorted(FAULTS)}")
    apply_fault(fault)
    return {"faults": sorted(state["faults"])}


@app.post("/admin/{action}")
def admin(action: str, authorization: str | None = Header(default=None)):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "admin token required")
    fixes = {"restart": {"cpu", "memory", "down", "errors", "slow"}, "rollback": {"bad_deploy", "errors"},
             "clear_cache": {"memory"}, "reset_db_pool": {"db"}, "enable_rate_limit": {"traffic"}}
    if action not in fixes:
        raise HTTPException(404, "unknown action")
    if action == "rollback":
        state["deployed_at"], state["version"] = time.time() - 86400, "v1.4.2"
    clear_faults(fixes[action])
    _window.clear()  # fresh measurement window after the fix
    log("info", f"admin action {action} executed")
    return {"action": action, "remaining_faults": sorted(state["faults"])}
