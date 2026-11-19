# AegisOps live demonstration: manual runbook

You run every step yourself. Nothing here is executed for you. All commands come from this repo
(`infra/aws/terraform/outputs.tf`, `infra/aws/README.md`, `shopfront/README.md`, `faultctl.py`),
except the two lines marked **(my one-liner)**, which were used and verified on this instance.

| Fixed value | |
|---|---|
| Region | `ap-south-1` |
| EC2 instance | `i-0ba7456de468a6268` |
| AegisOps console | https://13-127-211-52.sslip.io/ (user `analyst`) |
| ShopFront store | https://shop.13-127-211-52.sslip.io/ |
| ShopFront API container | `shopfront-shopfront-api-1` |

Passwords are never printed below: each command copies the secret to your clipboard (`| Set-Clipboard`). Paste it into the login box.

---

## 0. Before the evaluation (do this once, ~10 min)

**0.1 Install the AWS Session Manager plugin (REQUIRED, it is not installed on this laptop).**
Both the Grafana tunnel and the instance shell need it.
Download and run https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe, then open a NEW PowerShell window and check:

```powershell
session-manager-plugin
```
Expected: `The Session Manager plugin was installed successfully.`
(Fallback for the shell steps only: AWS Console > Systems Manager > Session Manager > Start session > pick the instance. That opens a shell in the browser with no plugin. Port forwarding has no such fallback.)

**0.2 AWS CLI is logged in:**
```powershell
aws sts get-caller-identity --query Arn --output text
```
Expected: `arn:aws:iam::352285845006:user/parasar`. If it errors, run `aws login`.

**0.3 Confirm the instance id and that it is running:**
```powershell
aws ec2 describe-instances --region ap-south-1 --filters "Name=tag:Name,Values=aegisops*" "Name=instance-state-name,Values=running" --query "Reservations[0].Instances[0].InstanceId" --output text
```
Expected: `i-0ba7456de468a6268`.

**0.4 Your network must be allow-listed.** The console and store only accept the 4 IPs in `allowed_cidrs`
(`infra/aws/terraform/terraform.tfvars`, line 2). Check the IP you will present from:
```powershell
Invoke-RestMethod https://api.ipify.org
```
If it is not one of the four, the two https URLs time out. (The SSM tunnels and shell work from any network.)
Adding an IP is an AWS change (`terraform apply`): do it BEFORE the evaluation, not during.

**0.5 Window plan:** PowerShell #1 = Grafana tunnel (stays open), #2 = Prometheus tunnel (stays open),
#3 = instance shell. Browser tabs: Grafana, Prometheus, AegisOps console, ShopFront store.

---

## 1. Port forwarding (copy of `terraform output grafana_tunnel`, with the argument quoted for PowerShell)

PowerShell #1, Grafana:
```powershell
aws ssm start-session --region ap-south-1 --target i-0ba7456de468a6268 --document-name AWS-StartPortForwardingSession --parameters "portNumber=3000,localPortNumber=3000"
```
PowerShell #2, Prometheus (same tunnel, port 9090, per `infra/aws/README.md`):
```powershell
aws ssm start-session --region ap-south-1 --target i-0ba7456de468a6268 --document-name AWS-StartPortForwardingSession --parameters "portNumber=9090,localPortNumber=9090"
```
Each prints `Waiting for connections...`. Leave them open. Stop with Ctrl+C when finished.
(Optional third tunnel, Alertmanager UI: same command with `9093` for both ports.)

## 2. Grafana: URL and login

* URL: **http://localhost:3000** (plain http: the traffic is inside the SSM tunnel)
* User: **admin**
* Password (copied to clipboard, not printed), then paste it:
```powershell
aws ssm get-parameter --region ap-south-1 --with-decryption --name /aegisops/GRAFANA_ADMIN_PASSWORD --query Parameter.Value --output text | Set-Clipboard
```
Sign-up is disabled. Dashboard direct link: http://localhost:3000/d/aegisops-ops

## 3. What Grafana actually has (checked in `docker/grafana/provisioning/`)

Exactly **one** provisioned dashboard: folder **AegisOps** > **AegisOps - Incident Operations** (refresh 10 s).
Set the time range (top right) to **Last 15 minutes**.

| Panel | Shows | Use in the demo |
|---|---|---|
| Application up (health) | `aegis_application_up`: AegisOps's own health check of ShopFront | **ShopFront availability**: drops to 0 during the fault |
| Incidents opened, Incidents by severity | AegisOps counters | goes +1 / critical when the incident opens |
| Investigation duration, Agent execution time p95 | ADK steps | investigation speed |
| MCP tool calls by status | the MCP tools used | evidence collection |
| A2A tasks by specialist | the 5 CrewAI specialists | shows all five answered |
| Human approval decisions | approvals | +1 `approved` after your click |
| Recovery success rate | recovered / total | |
| **Monitored app: HTTP 5xx / p95 latency / CPU-memory** | `shop_*` series | **NOT ShopFront.** These are the synthetic `demo-target` app's metrics and stay flat. Do not present them as ShopFront. |

**There is no ShopFront CPU / memory / request-rate / error / latency panel.** Use Grafana **Explore**
(http://localhost:3000/explore, data source Prometheus > Code mode). The metrics are real and exist today
(`shopfront/api/shopfront/telemetry.py`); the first four expressions come from `docker/prometheus/alerts.yml`:

| Signal | PromQL |
|---|---|
| Database availability | `shopfront_db_up` |
| HTTP errors (5xx per s) | `sum(rate(shopfront_http_requests_total{status=~"5.."}[1m]))` |
| Error ratio (alert threshold 0.05) | `sum(rate(shopfront_http_requests_total{status=~"5.."}[30s])) / clamp_min(sum(rate(shopfront_http_requests_total[30s])), 0.001)` |
| Latency p95 (alert threshold 1.5 s) | `histogram_quantile(0.95, sum by (le) (rate(shopfront_http_request_duration_seconds_bucket[1m])))` |
| CPU % of its limit | `shopfront_cpu_percent` |
| Memory % of its limit | `shopfront_memory_percent` |
| Request rate | `sum(rate(shopfront_http_requests_total[1m]))` |
| Scrape up | `up{job="shopfront"}` |

The same expressions work in the Prometheus UI (http://localhost:9090/graph). For the database demo the
signals that move are `shopfront_db_up` (1 to 0), 5xx rate and error ratio up. CPU, memory and latency stay normal.
Adding a ready-made ShopFront dashboard is a separate change (it must be copied to the instance); ask if you want it.

## 4. Pre-demo health checks

From PowerShell (no tunnel needed):
```powershell
Invoke-RestMethod https://13-127-211-52.sslip.io/api/v1/system/health
```
Expected: `status ok`, `applications 1`, `active_investigations 0`, **`llm_mode llm`**.
```powershell
(Invoke-WebRequest -UseBasicParsing https://shop.13-127-211-52.sslip.io/api/products).StatusCode
```
Expected: `200` (the store reads the database to answer this).

Deep check on the instance. PowerShell #3, open the shell (no `-document-name`: this is `terraform output ssm_shell`):
```powershell
aws ssm start-session --region ap-south-1 --target i-0ba7456de468a6268
```
Everything below inside this shell is Linux (not PowerShell). No faults active and faults enabled:
```bash
sudo docker exec shopfront-shopfront-api-1 python -m shopfront.faultctl status
```
Expected two lines: `200 {"api_running": true, ...}` and `200 {"enabled": true, "active": {}}`.
Record ShopFront's internal health (my one-liner):
```bash
sudo docker exec shopfront-shopfront-api-1 python -c "import httpx;r=httpx.get('http://127.0.0.1:8000/health',timeout=5);print(r.status_code,r.text)"
```
Expected: `200 {"status":"ok","database":"up","version":"2.4.0"}`.

**Also check in the browser (important):** open the AegisOps console > **Active** tab: it must be EMPTY.
While an incident for ShopFront is still open, new alerts are merged into it and no new incident appears.
In Prometheus (http://localhost:9090/alerts) all six `ShopFront*` rules should be **Inactive (green)**.

**Do not start if** any value above differs (non-empty Active tab, a non-empty `active`, `llm_mode` not `llm`, a non-200).

## 5. Trigger ONLY the database fault (in the instance shell)

```bash
sudo docker exec shopfront-shopfront-api-1 python -m shopfront.faultctl inject db --duration 600
```
Expected: `200 {"injected": "db", "active": {"db": {"expires_in_s": 600}}}`.
Note the time. Reload the store: it now shows errors (503). Flag usage is `faultctl inject db [--duration N]`
(`shopfront/api/shopfront/faultctl.py`).

**Bounded and self-reverting:** `faults.py` clamps every fault to `MAX_DURATION = 900` s (`min(max(duration, 10), 900)`),
and a watchdog thread reverts it automatically. 600 s is the CLI default; even `--duration 5000` would be capped at 900.
Only the in-memory connection setting is changed (the DSN points at a dead port); the data in PostgreSQL is not touched.
It cannot reach AegisOps, EC2 or Docker.
Check the countdown any time: `sudo docker exec shopfront-shopfront-api-1 python -m shopfront.faultctl status`.

## 6. Watch it happen (timings from the last real run on this instance)

| ~Seconds after inject | Where | What you see |
|---|---|---|
| +0 to +3 | store, Grafana "Application up" | store 503; `shopfront_db_up` = 0 |
| +2 | Prometheus > **Alerts** (http://localhost:9090/alerts) | `ShopFrontDatabaseDown` **Pending** (rule: `shopfront_db_up == 0`, `for: 15s`) |
| +15 to +20 | Prometheus | **Firing** (red). Click it to see labels `application=shopfront`, `severity=critical` |
| +15 to +20 | Alertmanager (http://localhost:9093, needs the 3rd tunnel) | alert **active** |
| +20 to +25 | AegisOps console | incident appears on the **Active** tab (P1, source prometheus_alertmanager) |
| +25 to +35 | AegisOps console | status **awaiting approval** |

Optional live proof that the webhook arrived (instance shell; read-only; Ctrl+C to stop):
```bash
cd /opt/aegisops/app && sudo docker compose -f docker-compose.yml -f docker-compose.aws.yml logs -f --tail 0 aegisops | grep --line-buffered -i webhook
```
Expected: `POST /api/v1/incidents/webhook HTTP/1.1" 200 OK`.

**AegisOps console:** https://13-127-211-52.sslip.io/ > sign in as **analyst**. Password to clipboard:
```powershell
aws ssm get-parameter --region ap-south-1 --with-decryption --name /aegisops/DEMO_PASSWORD --query Parameter.Value --output text | Set-Clipboard
```
Click the new incident (title about ShopFront and its database) on the **Active** tab. The detail page shows,
top to bottom: the investigation timeline (the ADK agents with durations), the MCP evidence (health, metrics, logs, endpoint probes),
the five specialists' findings (each marked `crewai_llm` = Gemini) with their A2A handoffs,
the root cause (`database_failure`, about 0.87 confidence, supporting evidence ids) and **Remediation options**.

## 7. Approve `reset_db_pool` (nothing runs until you click)

1. In the incident detail, find the **Remediation options** table.
2. Row **#1 `reset_db_pool`** (risk low, "Recycle the database connection pool...") > click **Approve** (right-hand column).
   Row #2 (escalate to on-call) has no button: it changes nothing. Do not click **Reject**.
3. A browser box asks **Reason to approve?** (pre-filled `Evidence reviewed`) > click **OK**.
4. Within a second the **Timeline** gets `human_approval` and `remediation_execution_agent: Executed hook reset_db_pool: succeeded (HTTP 200)`.
   AegisOps then waits about 45 s for fresh metrics, then adds `Recovery verified` and `Postmortem generated`. The incident moves to the **Resolved** tab (about 50 s in total last time).

## 8. Verify recovery and checkout after approval

Instance shell, fault gone and database up:
```bash
sudo docker exec shopfront-shopfront-api-1 python -m shopfront.faultctl status
```
Expected: `"active": {}`.
```bash
sudo docker exec shopfront-shopfront-api-1 python -c "import httpx;r=httpx.get('http://127.0.0.1:8000/health',timeout=5);print(r.status_code,r.text)"
```
Expected: `200 {"status":"ok","database":"up",...}` (my one-liner). Also: Prometheus `shopfront_db_up` = 1, the `ShopFrontDatabaseDown` rule back to Inactive, and the console's **Recovery verification** shows `db_up: 0 -> 1` and `error_rate: ... -> 0`.

PowerShell, store and checkout (places ONE real test order):
```powershell
$B = "https://shop.13-127-211-52.sslip.io"
(Invoke-WebRequest -UseBasicParsing "$B/api/products").StatusCode
$p = Invoke-RestMethod "$B/api/products" | Where-Object { $_.stock -ge 3 } | Select-Object -First 1
$body = @{ email = "evaluation@example.com"; items = @(@{ product_id = $p.id; quantity = 1 }) } | ConvertTo-Json -Depth 4
$order = Invoke-RestMethod -Method Post -Uri "$B/api/checkout" -ContentType "application/json" -Body $body
$order
Invoke-RestMethod "$B/api/orders/$($order.order_id)"
```
Expected: `200`, then `order_id ... status placed`, then the order read back from the database. (You can also just click through the store in the browser: add to cart > checkout.)

Optional audit evidence (instance shell, last 5 audit records, contains no secrets):
```bash
cd /opt/aegisops/app && sudo docker compose -f docker-compose.yml -f docker-compose.aws.yml exec -T aegisops tail -n 5 /app/data/sre_state/audit.log.jsonl
```
Expect `incident_opened`, `remediation_approved` (by `analyst`) and `remediation_executed`.
Final evidence in the console: **Resolved** tab > the incident (timeline, root cause, findings, evidence, approval, execution, verification, postmortem).

## 9. If something goes wrong

* **Abort / roll back the fault by hand** (documented rollback, instance shell):
  ```bash
  sudo docker exec shopfront-shopfront-api-1 python -m shopfront.faultctl clear db
  ```
  The incident stays "awaiting approval": reject it or leave it. Do not approve a fix for a fault you already cleared.
* **No incident appeared after about 60 s:** check the Active tab for an older open incident (alerts get merged into it); check Prometheus Alerts shows **Firing**.
* **Do not restart anything.** The fault expires on its own within 900 s.
* Tunnel will not start: `session-manager-plugin` missing (section 0.1) or `aws login` expired (0.2).
* Store or console time out from the browser: your IP is not in `allowed_cidrs` (0.4).

## 10. Not covered / known limits

* Grafana has no ShopFront-specific panels (section 3); Explore queries are the workaround.
* The Docker status of `shopfront-traffic` shows "unhealthy" (cosmetic: its health check targets the API image's port). Traffic is flowing.
* The test order stays in ShopFront's database. The AegisOps incident history keeps earlier demo incidents (Resolved tab).
