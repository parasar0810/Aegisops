"""AegisOps end-to-end demonstration (and benchmark harness).

Starts (unless --no-start) the synthetic demo-target app on :9000 and AegisOps on :8000
as separate processes, registers the app, injects a fault, lets the AegisOps
monitor DETECT it (no alert is sent by this script unless --webhook), waits for
the investigation, approves the top remediation (prompting unless --auto-approve),
verifies recovery and prints the postmortem.

  python scripts/run_demo.py --scenario db
  python scripts/run_demo.py --scenario all --auto-approve --report evals/reports/sre_benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from demo_app.registration import registration

# fault -> expected root-cause category (ground truth for the benchmark)
SCENARIOS = {
    "db": "database_failure", "memory": "memory_exhaustion", "cpu": "cpu_saturation",
    "errors": "http_errors", "slow": "high_latency", "down": "service_unavailable",
    "bad_deploy": "bad_deployment", "traffic": "abnormal_traffic", "none": None,
}


def wait_http(url: str, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {url}")


def start_processes(aegis_port: int, demo_port: int, state_dir: str) -> list[subprocess.Popen]:
    py = sys.executable
    demo = subprocess.Popen([py, "-m", "uvicorn", "demo_app.main:app", "--port", str(demo_port), "--log-level", "warning"],
                            cwd=ROOT, env={**os.environ, "PORT": str(demo_port)})
    aegis_env = {**os.environ, "AEGIS_MONITOR_INTERVAL_SECONDS": os.environ.get("AEGIS_MONITOR_INTERVAL_SECONDS", "5"),
                 "AEGIS_STATE_DIR": state_dir, "AEGIS_PUBLIC_URL": f"http://localhost:{aegis_port}",
                 "AEGIS_HOOK_TOKEN_DEMO_TARGET": os.environ.get("DEMO_ADMIN_TOKEN", "demo-admin-token"),
                 "AEGIS_VERIFY_SETTLE_SECONDS": os.environ.get("AEGIS_VERIFY_SETTLE_SECONDS", "6")}
    aegis = subprocess.Popen([py, "-m", "uvicorn", "app.main:app", "--port", str(aegis_port), "--log-level", "warning"],
                             cwd=ROOT, env=aegis_env)
    return [demo, aegis]


def token(api: str) -> dict[str, str]:
    from app.core.config import get_settings
    s = get_settings()
    r = httpx.post(f"{api}/api/auth/token", json={"username": s.demo_username, "password": s.demo_password,
                                                  "role": "sre"}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def run_scenario(api: str, demo: str, auth: dict, key: str, fault: str, auto_approve: bool,
                 use_webhook: bool, quiet: bool = False) -> dict:
    say = (lambda *a: None) if quiet else print
    httpx.post(f"{demo}/chaos/reset", timeout=5)
    time.sleep(10)  # let the healthy window settle and any recovery incident close
    known = {i["incident_id"] for i in httpx.get(f"{api}/api/v1/incidents", headers=auth, timeout=10).json()}
    t0 = time.time()
    if fault != "none":
        httpx.post(f"{demo}/chaos/{fault}", timeout=5)
    say(f"\n== Scenario '{fault}': fault injected at {time.strftime('%X')}")
    if use_webhook or fault == "none":
        time.sleep(15)  # let symptoms build in the metric window
        r = httpx.post(f"{api}/api/v1/incidents/webhook", headers={"X-AegisOps-Key": key}, timeout=10,
                       json={"application": "demo-target", "title": f"Synthetic alert ({fault})", "severity": "high"})
        say("   webhook ->", r.json())
    incident, detected_at = None, None
    deadline = time.time() + 120
    while time.time() < deadline:
        new = [i for i in httpx.get(f"{api}/api/v1/incidents", headers=auth, timeout=10).json()
               if i["incident_id"] not in known]
        if new:
            detected_at = detected_at or time.time()
            incident = httpx.get(f"{api}/api/v1/incidents/{new[-1]['incident_id']}", headers=auth, timeout=10).json()
            if incident["phase"] not in ("detected", "investigating"):
                break
        time.sleep(1)
    result: dict = {"scenario": fault, "expected": SCENARIOS[fault], "detected": incident is not None}
    if incident is None:
        say("   no incident detected within 120s")
        result["correct"] = SCENARIOS[fault] is None  # no incident on a healthy app is correct
        return result
    investigated_at = time.time()
    rca = incident.get("rca") or {}
    primary = rca.get("primary", {})
    result.update(
        incident_id=incident["incident_id"], source=incident["source"], detection_s=round(detected_at - t0, 2),
        investigation_agent_s=round(sum(incident.get("timings_ms", {}).values()) / 1000, 2),
        predicted=primary.get("category"), confidence=primary.get("confidence"),
        insufficient=rca.get("insufficient_evidence"), phase=incident["phase"], agent_ms=incident.get("timings_ms"),
        a2a_modes=sorted({a.get("mode") or "error" for a in incident.get("a2a_log", [])}))
    result["correct"] = (result["predicted"] == SCENARIOS[fault]) if SCENARIOS[fault] else bool(rca.get("insufficient_evidence"))
    say(f"   detected in {result['detection_s']}s by {incident['source']}; phase={incident['phase']}")
    say(f"   root cause: {primary.get('category')} (confidence {primary.get('confidence')})")
    say(f"   {rca.get('confidence_explanation', '')}")
    for o in incident.get("remediation_options", []):
        say(f"   option #{o['rank']}: {o['hook_name'] or '-'} [{o['risk']}] {o['description'][:80]}")
    actionable = [o for o in incident.get("remediation_options", []) if o["hook_name"]]
    if incident["phase"] == "awaiting_approval" and actionable:
        opt = actionable[0]
        approve = auto_approve or input(f"   Approve '{opt['hook_name']}'? [y/N] ").strip().lower() == "y"
        verb = "approve" if approve else "reject"
        r = httpx.post(f"{api}/api/v1/incidents/{incident['incident_id']}/remediation/{opt['option_id']}/{verb}",
                       headers=auth, json={"reason": "demo run"}, timeout=180)
        final = r.json()
        v = final.get("verification") or {}
        result.update(remediation=opt["hook_name"], approved=approve, final_phase=final.get("phase"),
                      recovered=v.get("recovered"), recovery_s=round(time.time() - t0, 2),
                      approval_to_verified_s=round(time.time() - investigated_at, 2))
        say(f"   {verb}d -> phase={final.get('phase')} recovered={v.get('recovered')}")
        say(f"   checks: {v.get('checks')}")
        if final.get("postmortem") and not quiet:
            pm = final["postmortem"]
            print("\n--- Postmortem (excerpt) ---")
            print(json.dumps({k: pm.get(k) for k in ("summary", "root_cause", "root_cause_confidence",
                                                     "supporting_evidence", "preventive_recommendations",
                                                     "generated_by")}, indent=2))
    httpx.post(f"{demo}/chaos/reset", timeout=5)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="db", choices=[*SCENARIOS, "all"])
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--demo", default="http://localhost:9000")
    ap.add_argument("--demo-internal", default=None, help="URL AegisOps uses to reach the demo app (docker)")
    ap.add_argument("--no-start", action="store_true", help="use already running services (docker compose)")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--webhook", action="store_true", help="send an alert instead of waiting for detection")
    ap.add_argument("--docker", action="store_true",
                    help="inside the aegisops container: demo at demo-app:9000, Prometheus mode, write Alertmanager key")
    ap.add_argument("--report")
    args = ap.parse_args()
    prometheus_url = None
    if args.docker:
        args.no_start, args.demo, prometheus_url = True, "http://demo-app:9000", "http://prometheus:9090"

    procs: list[subprocess.Popen] = []
    failed: list[str] = ["setup"]  # stays set if anything before the scenarios crashes
    if not args.no_start:
        state_dir = str(ROOT / "data" / f"demo_state_{int(time.time())}")
        procs = start_processes(int(args.api.rsplit(":", 1)[1]), int(args.demo.rsplit(":", 1)[1]), state_dir)
    try:
        wait_http(f"{args.demo}/health")
        wait_http(f"{args.api}/api/v1/system/health")
        auth = token(args.api)
        reg = registration(args.demo_internal or args.demo, prometheus_url=prometheus_url)
        r = httpx.post(f"{args.api}/api/v1/applications/register", json=reg, headers=auth, timeout=10)
        r.raise_for_status()
        key = r.json()["ingest_key"]
        if args.docker:  # Alertmanager reads this file on every webhook call
            (ROOT / "secrets" / "aegis_ingest_key").write_text(key)
        print("Registered application:", r.json()["app_id"])
        print("Reasoning mode:", httpx.get(f"{args.api}/api/v1/system/health", timeout=5).json()["llm_mode"])
        scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
        results = [run_scenario(args.api, args.demo, auth, key, s, args.auto_approve, args.webhook,
                                quiet=len(scenarios) > 1) for s in scenarios]
        if len(results) > 1:
            for res in results:
                print(json.dumps({k: res.get(k) for k in ("scenario", "expected", "predicted", "confidence",
                                                          "correct", "detection_s", "recovered", "recovery_s")}))
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                     "results": results}, indent=2))
            print("Report written to", args.report)
        # Pass = right root cause, and every approved fix verifiably recovered the app.
        failed = [r["scenario"] for r in results
                  if not r.get("correct") or (r.get("approved") and r.get("recovered") is not True)]
        print(f"\nRESULT: {len(results) - len(failed)}/{len(results)} scenarios passed"
              + (f"; failed: {failed}" if failed else " - AegisOps is fully working."))
    finally:
        for p in procs:
            p.terminate()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
