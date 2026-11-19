# Test Results

> All results below are sourced directly from project files. No results have been invented.
> Source files: `EVALUATION_REPORT.md` (2026-11-12), `evals/reports/latest.json` (2026-11-11), `evals/reports/sre_benchmark.json` (2026-11-11).

---

## 1. Automated Test Suite

**Command:**
```bash
uv run pytest
```

**Result: 63 passed, 0 failed.**

(Source: `EVALUATION_REPORT.md` Section 1)

| Test File | Tests | Coverage |
|---|---|---|
| `app/tests/test_sre_pipeline.py` | 13 | Registration validation, webhook auth, RBAC, MCP tool list + SSRF guard, A2A agent cards, DB incident end-to-end (detect → RCA → approve → hook → verified recovery → postmortem → audit), bad-deploy attribution, insufficient evidence on healthy app, rejection, Alertmanager payload + alert correlation, prompt injection in logs, resume after restart |
| `app/tests/test_sdk.py` | 1 | SDK registration, middleware exception reporting with correlation ID, bad key rejected without raising |
| `app/tests/test_adk_crew_workflow.py` | ~29 | ADK orchestrator + CrewAI postmortem crew (mocked LLM): schema validation, context transfer, demo-mode flags, error handling |
| `app/tests/test_investigation.py` | ~10 | Legacy SOC/RAG workflows |
| `app/tests/test_remediation_workflow.py` | ~10 | Legacy remediation approvals |

**Test design:** The SRE tests run AegisOps and the demo app in-process. Every MCP call, A2A task and remediation hook goes through real HTTP request handling (ASGI transport). LLM calls are mocked to allow tests to pass without API keys.

---

## 2. Evaluation Harness — 12/12 Passed

**File:** `evals/reports/latest.json` — generated 2026-11-11

```
total: 12 | passed: 12 | evaluation_mode: local
```

| Scenario | Passed | Risk Score | Status |
|---|---|---|---|
| cloud autoscaling failure | ✅ | 100 | major_incident |
| cloud load balancer unhealthy targets | ✅ | 56 | investigating |
| data model drift investigation | ✅ | 78 | degraded |
| data schema compatibility failure | ✅ | 92 | major_incident |
| it email delivery delay | ✅ | 70 | degraded |
| it vpn access outage | ✅ | 90 | major_incident |
| login anomaly account takeover | ✅ | 100 | confirmed_incident |
| checkout api production 503 incident | ✅ | 100 | major_incident |
| production latency spike after deploy | ✅ | 100 | major_incident |
| production queue backlog | ✅ | 79 | degraded |
| security suspicious oauth persistence | ✅ | 100 | confirmed_incident |
| security password spray against many users | ✅ | 58 | suspicious |

**Note on OpenAI judge:** All 12 cases show `openai_judge.error: "429 Too Many Requests"`. The OpenAI-based quality judge failed due to rate limiting; pass/fail is based on the local scoring logic, not the LLM judge.

---

## 3. Live SRE Benchmark — 9/9 Scenarios

**Command:**
```bash
uv run python scripts/run_demo.py --scenario all --auto-approve --report evals/reports/sre_benchmark.json
```

**File:** `evals/reports/sre_benchmark.json` — generated 2026-11-11, `evals/reports/platform_verification.json`

**Method:** ShopFront demo app and AegisOps run as separate OS processes. Faults injected; AegisOps monitor detects them autonomously (5 s poll). No synthetic alerts sent (except the healthy control). Remediation auto-approved; recovery verified by AegisOps.

**Reasoning mode: deterministic (no LLM).**

| Scenario | Expected | Predicted | Confidence | Correct | Detection (s) | Agent time (s) | Recovered | Fault→verified (s) |
|---|---|---|---|---|---|---|---|---|
| db | database_failure | database_failure | 0.74 | ✅ | 7.17 | 0.74 | ✅ | 18.12 |
| memory | memory_exhaustion | memory_exhaustion | 0.60 | ✅ | 7.25 | 1.73 | ✅ | 18.26 |
| cpu | cpu_saturation | cpu_saturation | 0.61 | ✅ | 13.34 | 2.77 | ✅ | 24.49 |
| errors | http_errors | http_errors | 0.68 | ✅ | 7.24 | 0.92 | ✅ | 18.26 |
| slow | high_latency | high_latency | 0.55 | ✅ | 16.36 | 4.65 | ✅ | 32.46 |
| down | service_unavailable | service_unavailable | 0.56 | ✅ | 7.19 | 0.78 | ✅ | 18.19 |
| bad_deploy | bad_deployment | bad_deployment | 0.61 | ✅ | 7.20 | 0.69 | ✅ | 18.11 |
| traffic | abnormal_traffic | abnormal_traffic | 0.59 | ✅ | 7.22 | 0.69 | ✅ | 18.16 |
| none (healthy) | (no root cause) | unknown (insufficient evidence) | 0.10 | ✅ | 19.23 | 0.86 | no action | — |

**Summary metrics:**
- Root-cause accuracy: **9/9** (8/8 faults + healthy control correctly suppressed)
- Recovery success rate: **8/8** approved fixes verified
- False positives: **0** in this run
- Median detection time: **7.2 s** (max 16.4 s — CPU/latency need sustained signal)
- Median agent investigation time: **0.85 s**
- Median fault→verified recovery: **18.2 s** (includes 6 s post-fix settle)

---

## 4. ShopFront Fault Scenarios — Recovery Times

> **Source:** `EVALUATION_REPORT.md` Section 2 and `evals/reports/sre_benchmark.json`.
> These are measured from actual benchmark runs (run_demo.py), not invented.

The demo app (`demo_app/main.py`) simulates ShopFront faults. The 6 primary fault types mapped to benchmark scenarios:

| Fault Type | Scenario Key | Fault→Verified (s) | Approval→Verified (s) |
|---|---|---|---|
| Database pool exhaustion | `db` | 18.12 | 8.88 |
| Memory exhaustion | `memory` | 18.26 | 8.94 |
| CPU saturation | `cpu` | 24.49 | ~12 |
| HTTP 500 error injection | `errors` | 18.26 | ~9 |
| High latency (slow endpoint) | `slow` | 32.46 | ~16 |
| Service downtime | `down` | 18.19 | ~9 |

> **Note on "ShopFront" terminology:** The `run_demo.py` benchmark uses the demo target (`demo_app/`) which simulates ShopFront faults. The actual ShopFront e-commerce app (`shopfront/`) has its own 6-scenario demo (`scripts/shopfront_demo.py`). The `evals/reports/sre_benchmark.json` contains results for the demo_app scenarios; a separate `shopfront_report.json` is generated by `shopfront_demo.py` but was not found in the submitted files.

---

## 5. Docker Compose Verification

**Source:** `EVALUATION_REPORT.md` Section 4

```
docker compose run --rm selfcheck  →  9/9 passed, exit 0
```

Also verified: Prometheus targets up with 7 rules loaded, Grafana dashboard provisioned, Swagger/console/agent cards reachable, zero errors in AegisOps logs.

| Scenario | Detected by | Detection (s) | Root cause | Recovered |
|---|---|---|---|---|
| db | prometheus_alertmanager | 20.2 | database_failure (0.74) | yes |
| memory | prometheus_alertmanager | 21.6 | memory_exhaustion (0.54) | yes |

(Detection is slower due to Prometheus alert `for:` durations and Alertmanager `group_wait`.)

---

## 6. AWS Deployment Verification

**Source:** `EVALUATION_REPORT.md` Section 4b — EC2 t4g.small, ap-south-1, 2026-11-12.

**Result: 9/9 passed.**

| Scenario | Predicted | Confidence | Detection (s) | Recovered | Fault→verified (s) |
|---|---|---|---|---|---|
| db | database_failure | 0.73 | 24.4 | ✅ | 34.5 |
| memory | memory_exhaustion | 0.60 | 24.6 | ✅ | 36.8 |
| cpu | cpu_saturation | 0.66 | 37.7 | ✅ | 50.1 |
| errors (HTTP 500) | http_errors | 0.71 | 27.5 | ✅ | 37.6 |
| slow | high_latency | 0.55 | 39.7 | ✅ | 53.1 |
| down | service_unavailable | 0.57 | 26.6 | ✅ | 36.7 |
| bad_deploy | bad_deployment | 0.63 | 40.0 | ✅ | 50.1 |
| traffic | abnormal_traffic | 0.59 | 24.7 | ✅ | 34.9 |
| none (healthy) | unknown (insufficient) | 0.10 | 15.1 | — | — |

---

## 7. Honest Caveats (from EVALUATION_REPORT.md)

> "The first run scored 5/9. The misses were log bleed between scenarios, a single evidence source for latency and CPU, an under-weighted deploy signal, and a Windows file-replace race. After fixing those, the re-run scored 9/9. Because the fixes were made against this same demo app, this is **not** a measure of accuracy on unseen applications."

> "One run per scenario; confidence values are calibrated heuristics, not probabilities."

