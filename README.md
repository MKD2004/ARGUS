# Argus

Argus is an agentic incident-response copilot. It doesn't replace an on-call engineer — it acts like a junior incident investigator: gathering evidence, testing competing root-cause hypotheses, checking past incidents for a match, and proposing a validated fix as a reviewable diff. A human always makes the final call before anything reaches a real pull request.

The expensive part of an incident is rarely the fix — it's the investigation. Argus automates evidence-gathering and diagnosis so that by the time an engineer opens their laptop, they're reviewing a diagnosis instead of starting one from zero.

## How it works

1. An alert fires (or a fault is manually injected for a demo run).
2. Log, Metrics, and Deploy agents investigate in parallel and produce structured evidence — never bare prose.
3. A Hypothesis Generator/Validator loop proposes and checks root-cause candidates against the evidence, tracking what was ruled out and why, until one is accepted or the loop escalates to a human.
4. Similar past incidents are retrieved from an incident memory store to enrich the diagnosis.
5. A fix is planned, generated as a diff, and tested in a self-correcting loop.
6. Nothing reaches a pull request without an explicit human approval — this is a structural gate, not a suggestion.
7. A postmortem and Slack notification are generated regardless of outcome, and the resolved incident is stored for future retrieval.

## Stack

- **Orchestration:** LangGraph (parallel branches, conditional routing, bounded retry loops, typed shared state) + FastAPI
- **LLM:** Anthropic Claude via `langchain-anthropic`
- **Data:** PostgreSQL + `pgvector` for incident similarity search
- **Observability (demo environment):** Prometheus, Loki, Grafana
- **Integrations:** GitHub API (deploy history, PRs, issues), Slack API (notifications)
- **Frontend:** React + WebSocket-streamed live agent progress
- **Demo environment:** an isolated Docker Compose microservices stack with on-demand, reversible fault injection

## Project layout

```
backend/     FastAPI + LangGraph service, typed state models, its own Docker Compose (app + Postgres/pgvector)
frontend/    React dashboard (Vite + TypeScript)
demo-env/    Isolated demo microservices stack + Prometheus/Loki/Grafana, separate Docker Compose
```

## Running locally

Requires Docker Desktop.

```bash
# Argus's own services (API + Postgres/pgvector)
cd backend
cp .env.example .env   # fill in ANTHROPIC_API_KEY etc.
docker compose up -d --build

# Demo microservices stack (isolated from the above)
cd demo-env
docker compose up -d --build

# Frontend
cd frontend
npm install
npm run dev
```

Seed the incident memory store, so similarity retrieval has history to match against:

```bash
cd backend
python -m db.seed_incidents   # idempotent
```

Backend health check: `http://localhost:8000/health`. Demo stack services are on `8001`–`8004`, Prometheus on `9090`, Grafana on `3000`, Loki on `3100`.

Start an investigation:

```bash
curl -X POST http://localhost:8000/incidents   -H 'Content-Type: application/json'   -d '{"service_name": "payments", "alert_type": "high_latency"}'
```

The response is the final incident state — evidence gathered, hypotheses considered and rejected with reasons, the accepted hypothesis, and any similar past incidents.

## Tests

```bash
cd backend
pytest                      # tests needing Postgres skip themselves if it's down
pytest -m "not integration" # skip them explicitly
```

## Status

In progress. The investigation half of the pipeline is built and tested end to end: alert ingress, parallel Log/Metrics/Deploy evidence gathering, the bounded hypothesis generate/validate loop, and incident-memory retrieval against pgvector.

Not built yet: fix planning, patch generation, the test/retry loop, the human approval gate, GitHub/Slack output, the dashboard, and the demo environment's fault-injection scenarios (the demo services are currently health-check stubs).
