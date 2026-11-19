# Run and verify AegisOps on another laptop

The only requirement is **Docker Desktop** (Windows/macOS) or Docker Engine + Compose (Linux), with about 4 GB of free RAM. Python and API keys are not needed.

## 1. Get the project

On the sharing machine, build the zip (it excludes `.env`, keys, virtualenvs and local state):

```bash
python scripts/package_share.py
```

Copy `dist/aegisops-share.zip` to the other laptop and unzip it, then open a terminal in the `aegisops` folder.

## 2. Start everything

```bash
docker compose up -d --build
```

The first build downloads base images and dependencies (about 5–10 minutes). This starts AegisOps, the ShopFront demo app, Prometheus, Alertmanager and Grafana.

## 3. Run the automatic self-check

```bash
docker compose run --rm selfcheck
```

It injects 8 real faults into the demo app one after another (database, memory, CPU, errors, slowness, outage, bad deploy, traffic), plus one healthy control. For each one it checks the full chain: Prometheus alert → Alertmanager → AegisOps detection → agent investigation → correct root cause → fix approved → recovery verified. It takes about 10 minutes and ends with:

```
RESULT: 9/9 scenarios passed - AegisOps is fully working.
```

The exit code is 0 when everything passes and 1 otherwise, with the failing scenarios named.

## 4. Look around

| What | Where |
|---|---|
| Operator console | http://localhost:8000 (user `analyst`, password `analyst`) |
| API docs (Swagger) | http://localhost:8000/docs |
| Grafana dashboard | http://localhost:3000 (admin / admin) → "AegisOps - Incident Operations" |
| Prometheus | http://localhost:9090 |
| Demo app | http://localhost:9000/api/products |

Try one scenario by hand and approve the fix yourself in the console:

```bash
docker compose exec aegisops python scripts/run_demo.py --docker --scenario db
```

## 5. Optional: LLM reasoning

Without keys, AegisOps uses deterministic evidence scoring (shown as `reasoning: deterministic` in the console). To use Gemini, create a `.env` file next to `docker-compose.yml` containing:

```
GEMINI_API_KEY=your-key
```

Then restart:

```bash
docker compose up -d
```

## Stop and clean up

```bash
docker compose down -v
```

`-v` also deletes stored incidents. Leave it off to keep them.

## Troubleshooting

* **Ports already in use** (8000, 9000, 9090, 9093, 3000): stop the other program, or change the left side of the `ports:` entries in `docker-compose.yml`.
* **Self-check fails on the first run right after startup**: wait one minute and run it again; Prometheus needs a few scrapes first.
* **Docker Desktop won't start on Windows**: make sure WSL 2 is installed (`wsl --install`) and virtualization is enabled.
