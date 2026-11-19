"""Demo-only fault injection. Disabled unless FAULT_INJECTION_ENABLED=true.

Every fault is bounded (container CPU/memory limits) and expires automatically,
so a forgotten fault can never keep the demo broken. Each fault models a
realistic cause, and each has a remediation that AegisOps can propose.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from shopfront.db import db
from shopfront.telemetry import event, memory_limit_bytes, memory_percent

ENABLED = os.environ.get("FAULT_INJECTION_ENABLED", "false").lower() == "true"
MAX_DURATION = 900


@dataclass
class FaultState:
    checkout_v2: bool = False           # A: buggy release flag -> HTTP 500 on checkout
    recommendations_delay_ms: int = 0   # B: slow dependency -> latency on product pages
    cpu_stop: threading.Event | None = None   # D: runaway report job
    ballast: list[bytearray] = field(default_factory=list)  # E: leaking image cache
    expires: dict[str, float] = field(default_factory=dict)


state = FaultState()
_lock = threading.Lock()


def active() -> dict[str, dict]:
    now = time.time()
    return {k: {"expires_in_s": round(v - now)} for k, v in state.expires.items()}


def _expire(name: str, duration: float) -> None:
    state.expires[name] = time.time() + min(max(duration, 10), MAX_DURATION)


# ── Faults and their fixes ──────────────────────────────────────────────────

def enable_http500(duration: float = 600) -> None:
    state.checkout_v2 = True
    _expire("http500", duration)
    event("info", "Feature flag checkout_v2 enabled (release 2.4.0 rollout)", flag="checkout_v2")


def rollback_feature_flag() -> str:
    state.checkout_v2 = False
    state.expires.pop("http500", None)
    event("info", "Feature flag checkout_v2 rolled back to checkout_v1")
    return "checkout_v2 disabled; checkout_v1 restored"


def enable_latency(duration: float = 600, delay_ms: int = 2500) -> None:
    state.recommendations_delay_ms = max(0, min(int(delay_ms), 8000))
    _expire("latency", duration)
    event("warning", f"recommendations-service responding slowly ({state.recommendations_delay_ms} ms)",
          service="recommendations-client")


def disable_recommendations() -> str:
    state.recommendations_delay_ms = 0
    state.expires.pop("latency", None)
    event("info", "Recommendations dependency disabled; product pages served without it")
    return "recommendations call bypassed"


def enable_db_failure(duration: float = 600) -> None:
    db.set_broken(True)
    _expire("db", duration)
    event("error", "Database connection pool reconfigured; new connections failing", service="shopfront-db-client")


def reset_db_pool() -> str:
    db.set_broken(False)
    state.expires.pop("db", None)
    ok = db.ping()
    event("info", f"Database connection pool recycled (healthy={ok})", service="shopfront-db-client")
    return f"pool recycled, database reachable={ok}"


def enable_cpu(duration: float = 300) -> None:
    with _lock:
        if state.cpu_stop:
            return
        stop = threading.Event()
        state.cpu_stop = stop

    def burn() -> None:
        while not stop.is_set():
            sum(i * i for i in range(20000))
    threading.Thread(target=burn, daemon=True, name="report-job").start()
    _expire("cpu", duration)
    event("warning", "Nightly sales report job started on the request workers; worker pool saturated")


def stop_background_jobs() -> str:
    with _lock:
        if state.cpu_stop:
            state.cpu_stop.set()
        state.cpu_stop = None
    state.expires.pop("cpu", None)
    event("info", "Background report job stopped")
    return "report job stopped"


def enable_memory(duration: float = 300, target_percent: float = 90, max_mb: int | None = None) -> None:
    target = min(max(target_percent, 10), 92)  # stay under the container limit (no OOM kill)
    limit = memory_limit_bytes()
    cap_mb = max_mb or (int(limit * target / 100 / 2**20) if limit else 64)
    allocated = sum(len(b) for b in state.ballast) // 2**20
    while allocated < cap_mb and (limit is None or memory_percent() < target):
        state.ballast.append(bytearray(b"\x01") * (8 * 2**20))  # filled, so pages are really committed
        allocated += 8
    _expire("memory", duration)
    event("error", f"Product image cache grew to {allocated} MB; heap pressure, GC thrashing (MemoryError risk)")


def release_memory() -> str:
    freed = sum(len(b) for b in state.ballast) // 2**20
    state.ballast.clear()
    state.expires.pop("memory", None)
    event("info", f"Product image cache cleared ({freed} MB released)")
    return f"{freed} MB released"


ENABLE = {"http500": enable_http500, "latency": enable_latency, "db": enable_db_failure,
          "cpu": enable_cpu, "memory": enable_memory}
DISABLE = {"http500": rollback_feature_flag, "latency": disable_recommendations, "db": reset_db_pool,
           "cpu": stop_background_jobs, "memory": release_memory}
# Allowlisted remediation operations (AegisOps hooks). Nothing else can be invoked.
OPS = {"rollback_feature_flag": rollback_feature_flag, "disable_recommendations": disable_recommendations,
       "reset_db_pool": reset_db_pool, "stop_background_jobs": stop_background_jobs,
       "release_memory": release_memory}


def watchdog() -> None:
    """Revert any fault whose time is up."""
    while True:
        time.sleep(5)
        now = time.time()
        for name, until in list(state.expires.items()):
            if now >= until:
                DISABLE[name]()
                event("info", f"Fault '{name}' expired and was reverted automatically")
