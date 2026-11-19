"""Fault injection CLI (run inside the shopfront-api container).

  python -m shopfront.faultctl status
  python -m shopfront.faultctl inject http500|latency|db|cpu|memory|downtime [--duration 600] [--delay-ms 2500]
  python -m shopfront.faultctl clear  http500|latency|db|cpu|memory|downtime
"""

import argparse
import json
import os
import sys

import httpx

API, SUP = "http://127.0.0.1:8000", "http://127.0.0.1:8081"
HEAD = {"X-Fault-Token": os.environ.get("FAULT_TOKEN", "")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "inject", "clear"])
    ap.add_argument("fault", nargs="?", choices=["http500", "latency", "db", "cpu", "memory", "downtime"])
    ap.add_argument("--duration", type=float, default=600)
    ap.add_argument("--delay-ms", type=int, default=2500)
    a = ap.parse_args()
    if a.action != "status" and not a.fault:
        ap.error("fault name required")
    if a.action == "status":
        r = [httpx.get(f"{SUP}/status", timeout=5), httpx.get(f"{API}/internal/faults", headers=HEAD, timeout=5)]
    elif a.fault == "downtime":
        method = "post" if a.action == "inject" else "delete"
        r = [getattr(httpx, method)(f"{SUP}/faults/downtime", headers=HEAD, timeout=15,
                                    **({"json": {"duration_s": a.duration}} if method == "post" else {}))]
    elif a.action == "inject":
        r = [httpx.post(f"{API}/internal/faults/{a.fault}", headers=HEAD, timeout=60,
                        json={"duration_s": a.duration, "delay_ms": a.delay_ms})]
    else:
        r = [httpx.delete(f"{API}/internal/faults/{a.fault}", headers=HEAD, timeout=30)]
    for resp in r:
        print(resp.status_code, json.dumps(resp.json()))
    return 0 if all(x.status_code < 300 for x in r) else 1


if __name__ == "__main__":
    sys.exit(main())
