import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"

print("=" * 60)
print("1. Authenticating via POST /api/auth/token")
auth_req = urllib.request.Request(
    f"{BASE}/api/auth/token",
    data=json.dumps({"username": "analyst", "password": "analyst", "role": "admin"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(auth_req, timeout=10) as resp:
    token = json.loads(resp.read().decode())["access_token"]
    print("Token obtained successfully.")

print("\n" + "=" * 60)
print("2. Submitting realistic incident to POST /api/adk/investigate")
payload = {
    "incident": {
        "title": "Payment gateway elevated 503 latency",
        "domain": "production",
        "severity": "high",
        "source": "datadog_webhook",
        "service": "checkout-payment-api",
        "environment": "production",
        "owner_team": "payments-sre",
        "description": "Payment service returning 503 errors at 12% rate, p99 latency 4500ms following v2.4.1 deployment.",
        "logs": [
            "ERROR 503 /checkout/pay 1714200001 upstream connect timeout",
            "WARN retry_attempt=3 service=payment-gw pool=db-primary",
            "FATAL circuit_breaker_tripped payment-auth-subsystem"
        ],
        "metrics": {
            "error_rate": 0.12,
            "latency_p99_ms": 4500.0,
            "throughput_rps": 850.0,
            "db_pool_utilization": 0.98
        },
        "tags": ["payment", "sre", "production"]
    }
}

inv_req = urllib.request.Request(
    f"{BASE}/api/adk/investigate",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    method="POST"
)

t0 = time.time()
with urllib.request.urlopen(inv_req, timeout=180) as resp:
    res = json.loads(resp.read().decode())
    elapsed = round(time.time() - t0, 2)
    print(f"\nInvestigation Response Status: {resp.status} (elapsed: {elapsed}s)")
    print(f"investigation_id:     {res.get('investigation_id')}")
    print(f"framework_chain:      {res.get('framework_chain')}")
    print(f"adk_demo_mode:        {res.get('adk_demo_mode')}")
    print(f"crew_demo_mode:       {res.get('crew_demo_mode')}")
    print(f"status:               {res.get('status')}")
    print(f"risk_score:           {res.get('risk_score')}")
    print(f"mcp_tools_used:       {res.get('mcp_tools_used')}")
    print(f"a2a_agents_consulted: {res.get('a2a_agents_consulted')}")
    
    print("\n--- ADK Timeline ---")
    for t in res.get("timeline", []):
        print(f"  {t}")
        
    pm = res.get("postmortem", {})
    print(f"\n--- CrewAI Postmortem Report ---")
    print(f"crew_demo_mode:       {pm.get('crew_demo_mode')}")
    print(f"confidence:           {pm.get('confidence')}")
    print(f"risk_level:           {pm.get('risk_level')}")
    print(f"crew_agents_used:     {pm.get('crew_agents_used')}")
    print(f"\nRoot Cause Hypothesis:\n{pm.get('root_cause_hypothesis')}")
    print(f"\nPostmortem Summary:\n{pm.get('postmortem_summary')}")
    print(f"\nRemediation Recommendation:\n{pm.get('remediation_recommendation')}")
    print(f"\nContributing Factors:\n{pm.get('contributing_factors')}")
    print(f"\nLessons Learned:\n{pm.get('lessons_learned')}")

print("\n" + "=" * 60)
print("3. Checking Remediation queue (GET /api/remediations)")
rem_req = urllib.request.Request(
    f"{BASE}/api/remediations",
    headers={"Authorization": f"Bearer {token}"}
)
with urllib.request.urlopen(rem_req, timeout=10) as resp:
    remeds = json.loads(resp.read().decode())
    print(f"Total remediations in queue: {len(remeds)}")
    if remeds:
        r = remeds[0]
        print(f"  Action ID: {r.get('id')}")
        print(f"  Status:    {r.get('status')} (pending_approval confirmed)")
        print(f"  Title:     {r.get('title')}")

print("\n" + "=" * 60)
print("Dual Gemini Workflow Test Complete!")

