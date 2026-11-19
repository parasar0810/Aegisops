"""ShopFront x AegisOps end-to-end demonstration (run inside the AegisOps Docker network).

  docker compose run --rm shopfront-check                       # all scenarios, auto-approve
  docker compose exec aegisops python scripts/shopfront_demo.py --scenario db   # interactive approval

For each scenario: wait for a clean baseline, inject the fault into ShopFront, confirm the
failure, wait for Prometheus -> Alertmanager -> AegisOps to open and investigate an incident,
approve the top-ranked allowlisted remediation, confirm recovery, and record what happened.
Nothing here fakes a result: every value comes from ShopFront, Prometheus or the AegisOps API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_demo import token, wait_http  # noqa: E402

API = os.environ.get("AEGIS_API", "http://aegisops:8000")
SHOP = "http://shopfront-api:8000"
SUP = "http://shopfront-api:8081"
PROM = "http://prometheus:9090"
FAULT = {"X-Fault-Token": os.environ.get("SHOPFRONT_FAULT_TOKEN", "")}
OPEN = ("detected", "investigating", "awaiting_approval", "remediating", "verifying")

# fault -> acceptable root causes (http500 is a bad feature-flag release: both labels are valid)
SCENARIOS = {
    "http500": {"http_errors", "bad_deployment"},
    "latency": {"high_latency"},
    "db": {"database_failure"},
    "cpu": {"cpu_saturation"},
    "memory": {"memory_exhaustion"},
    "downtime": {"service_unavailable"},
}


def registration() -> dict:
    """ShopFront's AegisOps manifest: internal Docker hostnames, Prometheus-backed signals."""
    rate = 'sum(rate(shopfront_http_requests_total{job="shopfront"}[30s]))'
    return {
        "name": "shopfront",
        "description": "ShopFront e-commerce store: React web tier, FastAPI API, PostgreSQL.",
        "base_url": SHOP,
        "environment": "demo",
        "tech_stack": ["react", "nginx", "python", "fastapi", "postgresql"],
        "health_check_url": f"{SHOP}/health",
        "services": [
            {"name": "shopfront-web", "kind": "nginx", "depends_on": ["shopfront-api"]},
            {"name": "shopfront-api", "kind": "http-api", "depends_on": ["shopfront-db"],
             "endpoints": ["/api/products", "/api/products/1"]},
            {"name": "shopfront-db", "kind": "postgresql", "depends_on": []},
        ],
        "deployment": {"platform": "docker-compose", "version": "2.4.0", "strategy": "recreate"},
        "infrastructure": {"host": "aegisops-ec2", "region": "ap-south-1"},
        "monitoring": {
            "prometheus_url": PROM,
            "logs_url": f"{SHOP}/internal/logs",
            "metric_queries": {
                "error_rate": f'sum(rate(shopfront_http_requests_total{{job="shopfront",status=~"5.."}}[30s])) / clamp_min({rate}, 0.001)',
                "p95_latency_seconds": 'histogram_quantile(0.95, sum by (le) (rate(shopfront_http_request_duration_seconds_bucket{job="shopfront"}[30s])))',
                "request_rate": rate,
                "cpu_percent": 'shopfront_cpu_percent{job="shopfront"}',
                "memory_percent": 'shopfront_memory_percent{job="shopfront"}',
                "db_pool_in_use_ratio": 'shopfront_db_pool_in_use_ratio{job="shopfront"}',
                "db_up": 'shopfront_db_up{job="shopfront"}',
                "up": 'up{job="shopfront"}',
                "seconds_since_deploy": 'time() - shopfront_deploy_timestamp_seconds{job="shopfront"}',
            },
            "thresholds": {"p95_latency_seconds": 1.5},
            # No database_host: the database is on ShopFront's private network only; AegisOps
            # learns its state from ShopFront's db_up metric and /health instead.
            "verify_settle_seconds": 45,
            "log_window_minutes": 2,
        },
        "remediation_hooks": [
            {"name": "rollback_feature_flag", "description": "Roll back the checkout_v2 feature flag to checkout_v1.",
             "url": f"{SHOP}/internal/ops/rollback_feature_flag", "risk": "low",
             "addresses": ["http_errors", "bad_deployment"], "rollback_hint": "Re-enable checkout_v2 once fixed."},
            {"name": "disable_recommendations", "description": "Bypass the slow recommendations dependency.",
             "url": f"{SHOP}/internal/ops/disable_recommendations", "risk": "low", "addresses": ["high_latency"],
             "rollback_hint": "Re-enable recommendations when the dependency is healthy."},
            {"name": "reset_db_pool", "description": "Recycle the database connection pool with the correct endpoint.",
             "url": f"{SHOP}/internal/ops/reset_db_pool", "risk": "low", "addresses": ["database_failure"],
             "rollback_hint": "The pool re-creates connections automatically."},
            {"name": "stop_background_jobs", "description": "Stop the runaway report job on the request workers.",
             "url": f"{SHOP}/internal/ops/stop_background_jobs", "risk": "low", "addresses": ["cpu_saturation"],
             "rollback_hint": "Re-run the report off-peak on a worker queue."},
            {"name": "release_memory", "description": "Clear the leaking product-image cache.",
             "url": f"{SHOP}/internal/ops/release_memory", "risk": "low", "addresses": ["memory_exhaustion"],
             "rollback_hint": "The cache warms up again on demand."},
            {"name": "restart_service", "description": "Restart the ShopFront API process (supervisor, allowlisted).",
             "url": f"{SUP}/ops/restart", "risk": "high",
             "addresses": ["service_unavailable", "memory_exhaustion", "cpu_saturation", "high_latency", "http_errors"],
             "rollback_hint": "Process restarts from the same image; no data is lost."},
        ],
    }


def inject(fault: str) -> None:
    if fault == "downtime":
        r = httpx.post(f"{SUP}/faults/downtime", headers=FAULT, json={"duration_s": 600}, timeout=20)
    else:
        body = {"duration_s": 600, "delay_ms": 2500, "target_percent": 90}
        r = httpx.post(f"{SHOP}/internal/faults/{fault}", headers=FAULT, json=body, timeout=60)
    r.raise_for_status()


def clear_all() -> None:
    for f in ("http500", "latency", "db", "cpu", "memory"):
        try:
            httpx.delete(f"{SHOP}/internal/faults/{f}", headers=FAULT, timeout=10)
        except httpx.HTTPError:
            pass
    try:
        if httpx.get(f"{SUP}/status", timeout=5).json().get("downtime_remaining_s"):
            httpx.delete(f"{SUP}/faults/downtime", headers=FAULT, timeout=20)
    except httpx.HTTPError:
        pass


def firing_shopfront_alerts() -> list[str]:
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": 'ALERTS{application="shopfront",alertstate="firing"}'}, timeout=10)
    return [x["metric"]["alertname"] for x in r.json()["data"]["result"]]


def incidents(auth: dict) -> list[dict]:
    return httpx.get(f"{API}/api/v1/incidents", params={"application": "shopfront"}, headers=auth, timeout=10).json()


def symptom(fault: str) -> str:
    """Observe the failure from ShopFront itself, independent of AegisOps."""
    try:
        if fault == "http500":
            r = httpx.post(f"{SHOP}/api/checkout", timeout=10,
                           json={"email": "probe@example.com", "items": [{"product_id": 1, "quantity": 1}]})
            return f"POST /api/checkout -> {r.status_code}"
        if fault == "latency":
            t = time.perf_counter()
            httpx.get(f"{SHOP}/api/products/2", timeout=15)
            return f"GET /api/products/2 took {time.perf_counter() - t:.2f}s"
        if fault in ("db", "downtime"):
            r = httpx.get(f"{SHOP}/health", timeout=5)
            return f"GET /health -> {r.status_code} {r.text[:60]}"
        m = httpx.get(f"{SHOP}/internal/metrics", timeout=10).text
        name = "shopfront_cpu_percent" if fault == "cpu" else "shopfront_memory_percent"
        val = next(line.split()[1] for line in m.splitlines() if line.startswith(name + " "))
        return f"{name} = {float(val):.0f}%"
    except httpx.HTTPError as exc:
        return f"request failed: {type(exc).__name__} (service unreachable)"


def run(fault: str, auth: dict, auto: bool) -> dict:
    res: dict = {"scenario": fault, "expected": sorted(SCENARIOS[fault])}
    clear_all()
    deadline = time.time() + 240  # clean baseline: app healthy, no firing ShopFront alerts, no open incident
    while time.time() < deadline:
        try:
            healthy = httpx.get(f"{SHOP}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            healthy = False
        if healthy and not firing_shopfront_alerts() and not [i for i in incidents(auth) if i["phase"] in OPEN]:
            break
        time.sleep(5)
    time.sleep(60)  # quiet gap so the previous scenario's errors age out of rate windows and log evidence
    known = {i["incident_id"] for i in incidents(auth)}
    t0 = time.time()
    inject(fault)
    time.sleep(4)
    res["symptom"] = symptom(fault)
    print(f"\n== {fault}: injected; ShopFront shows: {res['symptom']}", flush=True)
    inc, detected = None, None
    while time.time() < t0 + 300:
        new = [i for i in incidents(auth) if i["incident_id"] not in known]
        if new:
            detected = detected or time.time()
            inc = httpx.get(f"{API}/api/v1/incidents/{new[-1]['incident_id']}", headers=auth, timeout=10).json()
            if inc["phase"] not in ("detected", "investigating"):
                break
        time.sleep(3)
    res["aegisops_healthy_during_fault"] = httpx.get(f"{API}/api/v1/system/health", timeout=5).status_code == 200
    if inc is None:
        res.update(detected=False, correct=False)
        print("   no incident within 300s", flush=True)
        clear_all()
        return res
    rca = inc.get("rca") or {}
    primary = rca.get("primary", {})
    res.update(
        detected=True, incident_id=inc["incident_id"], source=inc["source"], alert=inc["title"],
        detection_s=round(detected - t0, 1), adk_steps=inc["completed_steps"],
        a2a=[{"agent": x.get("to"), "state": x.get("task_state"), "mode": x.get("mode"), "findings": x.get("findings")}
             for x in inc["a2a_log"]],
        mcp_evidence=[f"{e['source']}: {e['summary'][:90]}" for e in inc["evidence"]],
        predicted=primary.get("category"), confidence=primary.get("confidence"),
        explanation=rca.get("confidence_explanation"),
        options=[f"{o['hook_name'] or 'manual'} [{o['risk']}]" for o in inc["remediation_options"]])
    res["correct"] = res["predicted"] in SCENARIOS[fault]
    print(f"   detected in {res['detection_s']}s via {inc['source']} ({inc['title']})", flush=True)
    print(f"   root cause: {res['predicted']} (confidence {res['confidence']}); options: {res['options']}", flush=True)
    hooks = [o for o in inc["remediation_options"] if o["hook_name"]]
    if inc["phase"] == "awaiting_approval" and hooks:
        opt = hooks[0]
        ok = auto or input(f"   Approve '{opt['hook_name']}'? [y/N] ").strip().lower() == "y"
        r = httpx.post(f"{API}/api/v1/incidents/{inc['incident_id']}/remediation/{opt['option_id']}/{'approve' if ok else 'reject'}",
                       headers=auth, json={"reason": "ShopFront demo: evidence reviewed"}, timeout=240)
        final = r.json()
        v = final.get("verification") or {}
        ex = (final.get("executions") or [{}])[-1]
        res.update(approved=ok, remediation=opt["hook_name"], execution=f"{ex.get('status')} (HTTP {ex.get('http_status')})",
                   recovered=v.get("recovered"), checks=v.get("checks"), phase=final.get("phase"),
                   postmortem=bool(final.get("postmortem")), recovery_s=round(time.time() - t0, 1))
        res["symptom_after"] = symptom(fault)
        print(f"   approved {opt['hook_name']} -> {res['execution']}; recovered={res['recovered']}; "
              f"after fix: {res['symptom_after']}", flush=True)
    clear_all()
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all", choices=[*SCENARIOS, "all"])
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--report", default="/app/data/shopfront_report.json")
    a = ap.parse_args()
    wait_http(f"{API}/api/v1/system/health")
    wait_http(f"{SHOP}/health")
    auth = token(API)
    r = httpx.post(f"{API}/api/v1/applications/register", json=registration(), headers=auth, timeout=15)
    r.raise_for_status()
    (ROOT / "secrets" / "shopfront_ingest_key").write_text(r.json()["ingest_key"])  # read by Alertmanager
    print("Registered ShopFront:", r.json()["app_id"], flush=True)
    print("Reasoning mode:", httpx.get(f"{API}/api/v1/system/health", timeout=5).json()["llm_mode"], flush=True)
    results = [run(s, auth, a.auto_approve) for s in (SCENARIOS if a.scenario == "all" else [a.scenario])]
    Path(a.report).write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                          "results": results}, indent=2))
    failed = [x["scenario"] for x in results
              if not (x.get("correct") and x.get("recovered") is True and x.get("aegisops_healthy_during_fault"))]
    for x in results:
        print(json.dumps({k: x.get(k) for k in ("scenario", "predicted", "confidence", "correct", "source",
                                                "detection_s", "remediation", "recovered", "recovery_s")}))
    print(f"\nRESULT: {len(results) - len(failed)}/{len(results)} ShopFront scenarios passed"
          + (f"; failed: {failed}" if failed else " - detection, investigation, approval, fix and recovery verified."))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
