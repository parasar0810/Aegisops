# Team Contributions

> This document records only information that could be extracted from project files.
> No individual contributions have been invented or assumed.

---

## What the Project Files Say

No `CONTRIBUTORS.md`, `AUTHORS`, `git log` output, or team attribution section was found in any project documentation file examined (README.md, ARCHITECTURE.md, EVALUATION_REPORT.md, SHARING.md, DEPLOYMENT.md, APPLICATION_INTEGRATION.md, EVALUATION_RUNBOOK.md).

The `.env.example` file shows the demo username as `operator` and no personal names appear in any configuration file.

---

## Project Identity

The project is identified in `pyproject.toml` as:

```toml
[project]
name = "agentic-ai-incident-investigation-platform"
version = "0.1.0"
description = "Agentic AI incident investigation platform for multi-domain triage, evidence collection, RAG-grounded reasoning, and remediation guidance."
```

No `authors` field is populated in `pyproject.toml`.

---

## What Can Be Inferred About Scope

The project is a substantial system with the following independently verifiable deliverables:

| Deliverable | Approximate Code |
|---|---|
| Core SRE pipeline (ADK, pipeline, store, routes) | ~65 KB Python |
| A2A specialists and analysis engine | ~28 KB Python |
| MCP server and guardrails | ~16 KB Python |
| Legacy SOC services (ADK orchestrator, CrewAI crew, A2A registry, MCP registry) | ~90 KB Python |
| Pydantic schemas and models | ~27 KB Python |
| Test suites (5 test files) | ~63 KB Python |
| ShopFront e-commerce app (API + React frontend) | ~35 KB |
| Docker + Prometheus + Grafana + Nginx configuration | ~18 KB config |
| AWS Terraform infrastructure | ~10 KB HCL |
| Documentation (5 MD files + README) | ~55 KB Markdown |

---

## Recommendation for Presentation

If your university submission requires a team contribution table, populate it from your own records (commit history, task tracking, meeting notes). The project files do not contain the information needed to do this accurately.

