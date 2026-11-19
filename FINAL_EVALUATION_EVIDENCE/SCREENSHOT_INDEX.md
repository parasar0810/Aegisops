# Screenshot Index

## Existing Screenshots

Three screenshots exist in `docs/screenshots/`:

| Filename | File Size | Description |
|---|---|---|
| `docs/screenshots/aegisops-console-shopfront-db-incident.png` | 439 KB | AegisOps web console showing a ShopFront **database incident** in progress. Shows the investigation timeline, evidence panel, and remediation options. |
| `docs/screenshots/shopfront-home-aws.png` | 214 KB | ShopFront e-commerce store **home page** as deployed on AWS (`https://shop.13-127-211-52.sslip.io/`). Shows the live storefront UI. |
| `docs/screenshots/shopfront-product-aws.png` | 150 KB | ShopFront **product detail page** as deployed on AWS. |

One additional image exists at:

| Filename | Description |
|---|---|
| `docs/architecture.png` | System architecture diagram (file size not read — large binary). |

---

## Presentation Recommendations

### Must-Include Screenshots (Slide Priority)

| Priority | Screenshot | Recommended Slide |
|---|---|---|
| ⭐⭐⭐ | `aegisops-console-shopfront-db-incident.png` | **Core demo slide.** Shows the platform working end-to-end: detected incident, evidence gathered by ADK pipeline, MCP tool results, A2A specialist findings, and human approval UI. |
| ⭐⭐⭐ | `docs/architecture.png` | **Architecture slide.** Use as the main system overview diagram. |
| ⭐⭐ | `shopfront-home-aws.png` | **Live deployment slide.** Shows the monitored application running on AWS. Use to establish that this is a real system, not a demo mock. |
| ⭐ | `shopfront-product-aws.png` | **Supporting deployment slide.** Secondary AWS screenshot to reinforce live deployment claim. |

### Recommended Additional Screenshots (Take During Presentation Prep)

Since only 3 console screenshots exist, the following are recommended for a comprehensive presentation:

| Recommended Screenshot | What to Show |
|---|---|
| Grafana dashboard | `http://localhost:3000` — `AegisOps - Incident Operations` dashboard with live metrics |
| Prometheus metrics | `http://localhost:8000/api/metrics` or `http://localhost:9090` — scrape targets up |
| Swagger UI | `http://localhost:8000/docs` — full API surface |
| Agent Card JSON | `http://localhost:8000/api/v1/agents/sre_agent/agent-card.json` — A2A protocol evidence |
| A2A incident list | `/api/v1/incidents` with 9 scenario results showing phase=resolved |
| Terminal: `uv run pytest` | Show 63 passed, 0 failed |
| Terminal: benchmark | `uv run python scripts/run_demo.py --scenario all` output showing 9/9 |

---

## Screenshot Quality Notes

- `aegisops-console-shopfront-db-incident.png` is the most evidence-dense screenshot; ensure it is legible in the presentation (zoom in if needed to show the incident timeline and approval button).
- The two ShopFront AWS screenshots demonstrate live deployment but require verbal explanation to connect them to the AegisOps monitoring story.
- The architecture diagram (`docs/architecture.png`) should appear early to frame all subsequent screenshots.

