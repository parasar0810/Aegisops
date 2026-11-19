# AegisOps Evaluation Report

All numbers below come from real runs on 2026-11-12 (Windows 11, Python 3.11, Docker Desktop 29.9). Estimates are labelled as estimates.

## 1. Automated tests

```bash
uv run pytest
```

**63 passed, 0 failed.**

| Suite | Tests | Covers |
|---|---|---|
| `test_sre_pipeline.py` | 13 | registration validation, webhook auth, RBAC, MCP tool list + SSRF guard, A2A agent cards, DB incident end to end (detect → RCA → approve → hook → verified recovery → postmortem → audit), bad-deploy attribution, insufficient evidence on a healthy app, rejection, Alertmanager payload + alert correlation, prompt injection in logs, resume after restart |
| `test_sdk.py` | 1 | SDK registration, middleware exception reporting with correlation id, bad key rejected without raising |
| `test_adk_crew_workflow.py` | ~29 | original ADK orchestrator + CrewAI postmortem crew (mocked LLM) |
| `test_investigation.py`, `test_remediation_workflow.py` | ~20 | legacy SOC/RAG workflows (now without LangGraph) and legacy remediation approvals |

The SRE tests run AegisOps and the demo app in-process, but every MCP call, A2A task and remediation hook still goes through real HTTP request handling (ASGI). Other checks: the legacy `scripts/verify_platform.py` passes 12/12, `evals/run_eval.py` passes, and `ruff` reports no defects in the new code (remaining findings are FastAPI's `Depends` pattern and deliberate fallback `except` blocks).

## 2. Live end-to-end benchmark (separate processes, real ports)

```bash
uv run python scripts/run_demo.py --scenario all --auto-approve --report evals/reports/sre_benchmark.json
```

**Method.** The ShopFront demo app and AegisOps run as separate OS processes. For each scenario the harness resets the demo app, waits 10 s, injects one fault, and waits for the **AegisOps monitor to detect it on its own** (5 s polling; no alert is sent, except for the healthy control which receives a synthetic alert). Once the investigation finishes, the harness approves the top-ranked remediation and waits for AegisOps' recovery verification. Ground truth is the injected fault. Reasoning mode: deterministic (no LLM).

| Scenario | Expected | Predicted | Confidence | Correct | Detection (s) | Agent time (s) | Recovered | Fault → verified (s) |
|---|---|---|---|---|---|---|---|---|
| db | database_failure | database_failure | 0.74 | yes | 7.2 | 0.74 | yes | 18.1 |
| memory | memory_exhaustion | memory_exhaustion | 0.60 | yes | 7.3 | 1.73 | yes | 18.3 |
| cpu | cpu_saturation | cpu_saturation | 0.61 | yes | 13.3 | 2.77 | yes | 24.5 |
| errors | http_errors | http_errors | 0.68 | yes | 7.2 | 0.92 | yes | 18.3 |
| slow | high_latency | high_latency | 0.55 | yes | 16.4 | 4.65 | yes | 32.5 |
| down | service_unavailable | service_unavailable | 0.56 | yes | 7.2 | 0.78 | yes | 18.2 |
| bad_deploy | bad_deployment | bad_deployment | 0.61 | yes | 7.2 | 0.69 | yes | 18.1 |
| traffic | abnormal_traffic | abnormal_traffic | 0.59 | yes | 7.2 | 0.69 | yes | 18.2 |
| none (healthy) | no root cause | unknown, insufficient evidence | 0.10 | yes | 19.2* | 0.86 | no action | – |

\* time until the synthetic alert was processed.

| Metric | Result |
|---|---|
| Root-cause accuracy | **9/9** (8/8 faults + healthy control correctly left without a root cause) |
| Recovery success rate | **8/8** approved fixes verified |
| False positives | 0 in this run (the healthy control produced no remediation proposal) |
| Detection time | median 7.2 s, max 16.4 s (latency and CPU need sustained signal) |
| Agent investigation time | median 0.85 s, max 4.65 s |
| Fault → verified recovery | median 18.2 s (includes the deliberate 6 s post-fix settle) |

**Honest caveats**

* **Tuned on this target.** The first run scored **5/9**. The misses were log bleed between scenarios (30-minute log window), a single evidence source for latency and CPU, an under-weighted deploy signal, and a Windows file-replace race. After fixing those (10-minute log window, live endpoint probes, CPU log signals, deploy/error-onset correlation, retrying atomic writes, demo reset clears its logs) the re-run scored 9/9. Because the fixes were made against this same demo app, this is **not** a measure of accuracy on unseen applications.
* One run per scenario; confidence values are calibrated heuristics, not probabilities.

## 3. LLM mode (CrewAI with Gemini)

One live `db` scenario with `GEMINI_MODEL=gemini-3.5-flash-lite`:

* All 5 CrewAI specialists ran over A2A with real LLM calls (`reasoning_mode: crewai_llm`), taking 2.2–4.4 s each.
* Root cause `database_failure`, confidence **0.87** (vs 0.74 deterministic), supported by alert, health, log and metric evidence.
* The existing 3-agent CrewAI postmortem crew produced a real narrative (`crew_demo_mode: false`).
* Approved fix executed and recovery verified.
* Observed weakness: specialists sometimes report the dominant cause outside their focus (e.g. the security agent also reported the DB failure).

Token counts were not captured: CrewAI reports approximate counts for this Gemini model. The configured OpenAI key returned `429 insufficient_quota`, so the OpenAI path is untested.

## 4. Docker Compose (real monitoring path)

Inside the Compose stack, detection goes through Prometheus alert rules → Alertmanager → AegisOps webhook, and metrics are read through Prometheus.

| Scenario | Detected by | Detection (s) | Root cause | Recovered |
|---|---|---|---|---|
| db | prometheus_alertmanager | 20.2 | database_failure (0.74) | yes |
| memory | prometheus_alertmanager | 21.6 | memory_exhaustion (0.54) | yes |

Detection is slower than the built-in monitor because of the alert `for:` durations (10–20 s) and the Alertmanager `group_wait` (5 s). Also verified: Prometheus targets up and 7 rules loaded, Grafana dashboard provisioned and querying live data, Swagger/console/agent cards reachable, zero errors in the AegisOps logs, state persisted across a rebuild.

**Full Docker self-check** (`docker compose run --rm selfcheck`, all 9 scenarios via Prometheus → Alertmanager): **9/9 passed, exit 0**, both in the dev folder and from a clean unzip of the shareable bundle (`dist/aegisops-share.zip`).

## 4b. AWS deployment (EC2 t4g.small, ap-south-1)

Same self-check, run on the instance through SSM on 2026-11-12:

| Scenario | Predicted | Confidence | Detection (s) | Recovered | Fault → verified (s) |
|---|---|---|---|---|---|
| db | database_failure | 0.73 | 24.4 | yes | 34.5 |
| memory | memory_exhaustion | 0.60 | 24.6 | yes | 36.8 |
| cpu | cpu_saturation | 0.66 | 37.7 | yes | 50.1 |
| errors (HTTP 500) | http_errors | 0.71 | 27.5 | yes | 37.6 |
| slow | high_latency | 0.55 | 39.7 | yes | 53.1 |
| down | service_unavailable | 0.57 | 26.6 | yes | 36.7 |
| bad_deploy | bad_deployment | 0.63 | 40.0 | yes | 50.1 |
| traffic | abnormal_traffic | 0.59 | 24.7 | yes | 34.9 |
| none (healthy) | unknown (insufficient evidence) | 0.10 | 15.1 | – | – |

**9/9 passed.** Also verified on AWS: the instance rebooted and all 5 containers came back automatically (API healthy 42 s after the reboot request) with all 9 incidents still stored. Over the internet, the API rejects the default password and missing tokens (401) and accepts the generated operator password. Ports 3000/9090/9093/9000 are not reachable from outside. Memory in use after reboot: ~850 MiB of 1.8 GiB.

Two Linux-only bugs found during the AWS run and fixed (Docker Desktop on Windows hid both): the non-root container could not write the Alertmanager key into a root-owned bind mount, and a volume could not be mounted inside a read-only bind mount. The key now travels through the `am-key` named volume.

## 5. Cost estimation (estimate, not measured)

Deterministic mode makes **no LLM calls**, so the only cost is compute.

LLM mode per investigation: 5 specialist calls + 3 postmortem crew calls = **8 LLM calls**. Specialist prompts carry the evidence summary (roughly 2–4k input tokens, ~300 output tokens each); postmortem calls carry the brief (roughly 2–3k input, ~500 output each). That is roughly **25–35k input + 3k output tokens per incident**. Multiply by your model's current per-token price to get the cost per incident. With a "flash-lite" class model this is typically a fraction of a cent to a few cents per incident; check current pricing before relying on it.

Infrastructure: one AegisOps container (1 vCPU / 2 GB is enough for the demo load) plus your existing Prometheus. AWS costs will be estimated with the Terraform once the AWS details are provided.

## Reproduce

```bash
uv run pytest
```

```bash
uv run python scripts/run_demo.py --scenario all --auto-approve --report evals/reports/sre_benchmark.json
```

```bash
docker compose up -d --build
```

```bash
docker compose run --rm selfcheck
```

## 6. Phase 2: ShopFront (separate application) on AWS

ShopFront (`shopfront/`: React + FastAPI + PostgreSQL) runs as its own Compose project on the same EC2 instance, registered through `/api/v1/applications/register`, scraped by Prometheus, with six alert rules routed by Alertmanager to the AegisOps webhook using ShopFront's own ingest key. Faults are injected through ShopFront's token-protected internal API; detection is entirely Prometheus → Alertmanager → webhook (no alert is sent by the harness).

```bash
docker compose run --rm shopfront-check
```

**Live run on AWS, 2026-11-12, deterministic reasoning:**

| Fault | ShopFront symptom (observed directly) | Detected by | Detection (s) | Root cause (confidence) | Approved fix | Recovered | Fault → verified (s) |
|---|---|---|---|---|---|---|---|
| HTTP 500 | `POST /api/checkout -> 500` | prometheus_alertmanager | 97.7 | bad_deployment (0.56)* | rollback_feature_flag | yes, checkout → 201 | 142.8 |
| Latency | `GET /api/products/2` took 2.52 s | prometheus_alertmanager | 42.8 | high_latency (0.55) | disable_recommendations | yes, 0.02 s | 91.0 |
| Database | `/health -> 503`, database unreachable | prometheus_alertmanager | 22.2 | database_failure (0.75) | reset_db_pool | yes, `/health` 200 | 67.4 |
| CPU | `shopfront_cpu_percent = 100%` | prometheus_alertmanager | 43.8 | cpu_saturation (0.57) | stop_background_jobs | yes, 2% | 89.0 |
| Memory | `shopfront_memory_percent = 91%` | prometheus_alertmanager | 25.5 | memory_exhaustion (0.53) | release_memory | yes, 21% | 70.7 |
| Downtime | API unreachable (connection refused) | prometheus_alertmanager | 40.6 | service_unavailable (0.63) | restart_service | yes, `/health` 200 | 85.8 |

**6/6 passed.** "Fault → verified" includes the 45 s settle ShopFront needs for its 30 s PromQL `rate()` windows to drain. AegisOps' own health stayed 200 during every ShopFront fault, and ShopFront recovered each time without restarting AegisOps.

\* The image had been built minutes earlier, so "errors began right after a release" is a valid reading of the evidence; the harness accepts `bad_deployment` or `http_errors` for this fault, and the chosen fix (`rollback_feature_flag`) addresses both. Locally (release older than 15 min) the same fault was labelled `http_errors` (0.59).

**Also verified on AWS:** ShopFront stack fully stopped and restarted → orders persisted (87 before, 93 after including new synthetic purchases), AegisOps incidents unchanged (16), AegisOps health 200 throughout. `https://shop.13-127-211-52.sslip.io/` serves the store with a valid certificate; `/internal/*` paths are not reachable through it. Memory in use with both stacks: ~1.0 GiB of 1.8 GiB.

**How we got to 6/6 (honest history):**

| Run | Result | Cause of failures | Fix |
|---|---|---|---|
| Local #1 | 3/6 | The registration gave AegisOps ShopFront's database host for a TCP probe; the database is (correctly) private, so the probe always failed and added a false "database unreachable" finding that pulled HTTP 500, latency and downtime towards `database_failure` | removed the direct DB probe; ShopFront's own `db_up` metric and `/health` report DB state |
| Local #2 | 5/6 | Downtime had only one evidence source (failed health check) → confidence 0.38 → AegisOps correctly refused to act | added Prometheus `up{job="shopfront"}` as a signal (a second, independent observation) |
| Local #3 + AWS | 6/6 | – | – |

Also added during Phase 2: per-app `log_window_minutes` (back-to-back scenarios were reading the previous incident's errors) and `verify_settle_seconds`, plus a 60 s quiet gap between scripted scenarios.

**Tests:** AegisOps 65 passed (63 previous + 2 new analysis tests), ShopFront API 11 passed.

**Screenshots:** `docs/screenshots/shopfront-home-aws.png`, `docs/screenshots/shopfront-product-aws.png` (live AWS store), `docs/screenshots/aegisops-console-shopfront-db-incident.png` (console, resolved ShopFront database incident; captured on the local stack because the AWS console login is the operator's).

## 7. Gemini on AWS (Nov 2026)

Key in SSM `/aegisops/GEMINI_API_KEY` (SecureString), model `gemini-3.5-flash-lite`, free API tier.

| Run | Where | Result | Duration |
|---|---|---|---|
| Live ShopFront `db` fault (Prometheus → Alertmanager → webhook) | live stack | 5/5 specialists `crewai_llm` over A2A (all citing real evidence ids), RCA `database_failure` 0.86, `reset_db_pool` approved, recovered, CrewAI postmortem crew, audit `incident_opened` / `remediation_approved` / `remediation_executed` | detected 28.3 s, recovered 90.4 s after the fault |
| Isolated fallback (invalid key, Google API returns 400) | one-off container, `timeout 120` | 5/5 A2A tasks `deterministic`, RCA `database_failure` 0.74, fix + recovery verified, postmortem `crewai_error: llm_unavailable` | 12.4 s pipeline, 31 s wall |
| Isolated Gemini (real key) | one-off container, `timeout 120` | 5/5 `crewai_llm`, RCA 0.87, recovered, postmortem `deterministic+crewai` | 21.4 s pipeline, 39 s wall |
| Unit test: every CrewAI call hangs | pytest | specialists and postmortem give up at the cap, deterministic result | < 10 s (cap 0.5 s) |

**Problems found and fixed**

* The first live run after adding the key silently fell back to deterministic: compose loaded `REDIS_URL=redis://localhost:6379/0` from `.env.example`, and CrewAI's lock store tried that (absent) Redis on every call. `REDIS_URL` is now empty for `aegisops`.
* CrewAI's native Gemini client ignores `LLM(timeout=60)`, so a stalled Gemini call was unbounded. Each CrewAI run is now wrapped in `asyncio.wait_for(AEGIS_LLM_TIMEOUT_SECONDS=45)` with `max_iter=3, max_retry_limit=1`.
* When the postmortem crew swallowed an LLM failure it returned a demo report that was labelled `deterministic+crewai`; it is now reported as `crewai_error: llm_unavailable`.
* A first "fallback" test was invalid: `docker compose run shopfront-check` recreated `aegisops` through `depends_on` with the real key. The fallback is now tested in an isolated one-off container (`--no-deps`).
* The SSM wait loops (`redeploy.sh`, operator scripts) hid errors and had no deadline; one ran 27 min after the remote command had finished in 3 min 24 s. They now have per-call timeouts, visible errors and a deadline.

**Usage:** about 8 Gemini requests per incident (5 specialists + the 3-agent postmortem crew), a few thousand input tokens each (CrewAI reports approximate counts only). Cost: $0 while the key's Google project stays on the free tier with billing off (this cannot be checked from AWS; confirm in Google AI Studio).

Tests: 66 AegisOps (adds the LLM-hang test) and 11 ShopFront pass.
