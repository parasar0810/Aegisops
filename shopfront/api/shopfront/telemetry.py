"""ShopFront telemetry: Prometheus metrics, container-aware CPU/memory, structured JSON logs."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("shopfront_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("shopfront_http_request_duration_seconds", "HTTP request latency", ["route"],
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 5, 10))
DB_UP = Gauge("shopfront_db_up", "1 if the last database probe succeeded")
DB_ERRORS = Counter("shopfront_db_errors_total", "Database errors seen by the application")
DB_POOL = Gauge("shopfront_db_pool_in_use_ratio", "Checked-out connections / pool capacity")
CPU = Gauge("shopfront_cpu_percent", "Container CPU use, percent of its CPU limit")
MEMORY = Gauge("shopfront_memory_percent", "Container memory use, percent of its memory limit")
DEPLOYED = Gauge("shopfront_deploy_timestamp_seconds", "Unix time the running release was built")
ORDERS = Counter("shopfront_orders_total", "Checkout attempts", ["result"])

DEPLOYED.set(float(os.environ.get("BUILD_TIME", "0") or 0) or time.time())

_CG = Path("/sys/fs/cgroup")
_cpu_last: tuple[float, float] | None = None
_last = {"cpu": 0.0, "mem": 0.0}


def _cpu_limit_cores() -> float:
    try:
        quota, period = (_CG / "cpu.max").read_text().split()
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    return float(os.cpu_count() or 1)


def _cpu_usage_seconds() -> float:
    try:
        for line in (_CG / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    except (OSError, ValueError):
        pass
    return time.process_time()


def memory_limit_bytes() -> int | None:
    try:
        raw = (_CG / "memory.max").read_text().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def memory_percent() -> float:
    limit = memory_limit_bytes()
    try:
        current = int((_CG / "memory.current").read_text())
    except (OSError, ValueError):
        return 0.0  # not in a cgroup v2 container (e.g. local tests)
    return 100.0 * current / limit if limit else 0.0


def refresh_resource_gauges() -> dict[str, float]:
    """Update CPU% (of the container's CPU limit) and memory% (of its memory limit)."""
    global _cpu_last
    now, used = time.monotonic(), _cpu_usage_seconds()
    if _cpu_last and now - _cpu_last[0] > 0.2:
        _last["cpu"] = min(100.0, 100 * (used - _cpu_last[1]) / (now - _cpu_last[0]) / _cpu_limit_cores())
    _cpu_last = (now, used)
    _last["mem"] = memory_percent()
    CPU.set(_last["cpu"])
    MEMORY.set(_last["mem"])
    return dict(_last)


# ── Structured logs: JSON to stdout + a ring buffer served at /internal/logs ──
RECENT_LOGS: deque[dict] = deque(maxlen=2000)


class _JsonHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        entry = {"timestamp": datetime.now(UTC).isoformat(), "level": record.levelname.lower(),
                 "service": getattr(record, "service", "shopfront-api"), "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        RECENT_LOGS.append(entry)
        sys.stdout.write(json.dumps(entry) + "\n")
        sys.stdout.flush()


log = logging.getLogger("shopfront")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    log.addHandler(_JsonHandler())


def event(level: str, message: str, service: str = "shopfront-api", **fields) -> None:
    log.log(getattr(logging, level.upper()), message, extra={"service": service, "fields": fields})
