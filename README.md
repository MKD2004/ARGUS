# Argus

Argus is an agentic incident-response copilot. It doesn't replace an on-call engineer — it acts like a junior incident investigator: gathering evidence, testing competing root-cause hypotheses, checking past incidents for a match, and proposing a validated fix as a reviewable diff. A human always makes the final call before anything reaches a real pull request.

The expensive part of an incident is rarely the fix — it's the investigation. Argus automates evidence-gathering and diagnosis so that by the time an engineer opens their laptop, they're reviewing a diagnosis instead of starting one from zero.

## How it works

1. An alert fires (or a fault is injected for a demo run).
2. Log, Metrics, and Deploy agents investigate in parallel and produce structured evidence — never bare prose.
3. A Hypothesis Generator/Validator loop proposes and checks root-cause candidates against the evidence, tracking what was ruled out and why, until one is accepted or the loop escalates to a human.
4. Similar past incidents are retrieved from an incident memory store to enrich the diagnosis.
5. A fix is planned, generated as a diff, and tested in a self-correcting loop.
6. Nothing reaches a pull request without an explicit human approval — this is a structural gate, not a suggestion.
7. A postmortem and Slack notification are generated regardless of outcome, and the resolved incident is stored for future retrieval.

## A real run

On 2026-09-15 the whole pipeline ran against a real failure, on a laptop, using a free local model:

| Time | What happened |
|---|---|
| 0–15 s | A real traffic spike exhausted the payments service's Redis connection pool |
| 17 s | Log, Metrics, and Deploy agents returned 6 pieces of real evidence: 200 timeout errors from Loki (grouped into one claim), and Prometheus showing the pool at 10 of 10 with 530 timeouts |
| 89 s | The model accepted "Redis connection pool exhausted … capacity of 10 connections", citing 5 pieces of evidence, and ruled out two rival hypotheses |
| 160 s | **Patch attempt 1** raised the pool to 20 — the service's real capacity test **failed** (peak traffic needs 30) |
| 174 s | **Patch attempt 2** raised it to 30 — the test **passed** |
| 175 s | Paused for a human. Approved on the dashboard, which opened [PR #2](https://github.com/MKD2004/ARGUS/pull/2): one file, one line, with the postmortem as its comment |

The model is a setting (`LLM_PROVIDER`), so a stronger paid model drops into the same pipeline unchanged.

## Stack

- **Orchestration:** LangGraph (parallel branches, conditional routing, bounded retry loops, typed shared state) + FastAPI
- **LLM:** a local model through Ollama by default (`granite4.1:8b`), or Anthropic Claude, chosen by the `LLM_PROVIDER` setting and reached through LangChain
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

## Setup (once)

Needs [Docker Desktop](https://www.docker.com/products/docker-desktop/), [Ollama](https://ollama.com), Python 3.11+, Node 20+, and `git` on PATH. Commands are PowerShell, run from the repo root.

```powershell
# 1. The model (~5.3 GB). It needs about 7 GB of free memory while loaded,
#    so on a 16 GB machine close browsers and other heavy apps before a demo.
ollama pull granite4.1:8b

# 2. Backend environment
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # defaults to local Ollama; add GITHUB_TOKEN etc. if you want real PRs
cd ..

# 3. Dashboard dependencies
cd frontend
npm install
cd ..
```

`.env` settings worth knowing: `LLM_PROVIDER` (`ollama` or `anthropic`), `OLLAMA_MODEL`, `GITHUB_REPO` and `GITHUB_TOKEN` (needed for real PRs and issues), `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` (optional).

## Demo rehearsal (PowerShell)

The full run, start to finish. Use **four PowerShell windows**: three stay open (containers are detached, but the backend and dashboard run in the foreground). Total time is about 10 minutes, of which the run itself is around 3.

### 1. Start the demo stack — window 1

```powershell
cd 'C:\Users\mahit\OneDrive\Desktop\New folder\demo-env'
docker compose up -d --build payments prometheus loki promtail
docker compose ps        # all four plus redis should be running
```

Optional, so the "similar past incident" step has history to match against:

```powershell
cd '..\backend'
docker compose up -d postgres
.\.venv\Scripts\python.exe -m db.seed_incidents
```

### 2. Preload the model — window 1

Loading takes about a minute. Doing it now keeps that minute out of the demo.

```powershell
# Ollama must be running: launch it from the Start menu, or in its own window run `ollama serve`
ollama ps                # errors if Ollama isn't running

Invoke-RestMethod -Uri http://localhost:11434/api/generate -Method Post `
  -ContentType 'application/json' `
  -Body '{"model":"granite4.1:8b","keep_alive":"30m"}'
ollama ps                # now shows the model loaded, and for how long
```

### 3. Start the backend — window 2 (leave it running)

```powershell
cd 'C:\Users\mahit\OneDrive\Desktop\New folder\backend'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

### 4. Start the dashboard — window 3 (leave it running)

```powershell
cd 'C:\Users\mahit\OneDrive\Desktop\New folder\frontend'
npm run dev
```

### 5. Run the demo — browser

1. Open <http://localhost:5173>.
2. Choose **Redis connection exhaustion (real fault)** and click **Inject failure**.
3. Watch it work. Nothing happens for about 25 seconds while the fault reaches the logs and metrics, then the agents tick off one by one. Roughly: evidence at ~20 s, root cause at ~90 s, fix plan at ~145 s, patch attempts at ~160 s.
4. **The moment worth narrating:** the first patch usually fails the capacity test and the second passes. That is the retry loop correcting a wrong fix, checked by a real test.
5. At **Your decision**, type your name (it appears in the PR and the postmortem) and choose:
   - **Approve** → opens a real PR on `GITHUB_REPO` with the diff and the postmortem.
   - **Reject** → opens an issue with the diagnosis instead.

### 6. Reset afterwards

**If you approved, close the pull request without merging.** Open the PR link from the dashboard's Outcome panel and use **Close pull request**. Merging would fix the demo service's intentional misconfiguration, and the scenario would stop reproducing. A closed PR keeps its diff and postmortem comment, so it still works as something to show.

```powershell
# Stop the fault generator (it stops by itself after two minutes)
Invoke-RestMethod -Uri http://localhost:8003/fault/reset -Method Post

# Ctrl+C in the backend and dashboard windows, then:
cd 'C:\Users\mahit\OneDrive\Desktop\New folder\demo-env'
docker compose stop
cd '..\backend'
docker compose stop
ollama stop granite4.1:8b
```

### If something goes wrong

| Symptom | Cause and fix |
|---|---|
| **502 "Is demo-env running?"** on inject | The payments container isn't up. `docker compose ps` in `demo-env`. |
| The run gets killed, or everything crawls | Not enough memory. Close browsers, check `ollama ps` shows the model, and leave only the 5 demo containers running. |
| Every AI step is skipped, and the run ends with no diagnosis | Ollama isn't running, or the model isn't pulled. `ollama ps` should work and `ollama list` should show `granite4.1:8b`. Argus deliberately fails open here, so a missing model looks like "couldn't diagnose it". |
| **No evidence found** | Prometheus and Loki need a few seconds. Check <http://localhost:9090/targets> and that the payments container logs show timeout errors. |
| Approve is greyed out | There's no passing patch. The panel says why. Reject files an issue instead, which is also a fine thing to show. |
| Dashboard says "Incident not found" | The backend restarted; incidents are held in memory. Inject a new one. |

## Using the API directly

The dashboard buttons call these. `Invoke-RestMethod` avoids PowerShell's quoting traps with JSON bodies; note that plain `curl` in PowerShell is an alias for `Invoke-WebRequest`, so use `curl.exe` if you prefer curl.

```powershell
# Inject the real scenario (same as the dashboard button)
Invoke-RestMethod -Uri http://localhost:8000/scenarios/redis_exhaustion/inject -Method Post

# Start an investigation from an arbitrary alert
Invoke-RestMethod -Uri http://localhost:8000/incidents -Method Post -ContentType 'application/json' `
  -Body '{"service_name": "payments", "alert_type": "high_latency"}'

# Review, then decide ($id from the responses above)
Invoke-RestMethod -Uri "http://localhost:8000/incidents/$id"
Invoke-RestMethod -Uri "http://localhost:8000/incidents/$id/approval"
Invoke-RestMethod -Uri "http://localhost:8000/incidents/$id/decision" -Method Post -ContentType 'application/json' `
  -Body '{"decision": "rejected", "decided_by": "your-name"}'
```

Progress also streams over `ws://localhost:8000/ws/incidents/<incident_id>`: every event carries the full incident state, and a new connection replays everything so far. `approved` is refused (409, with the reason) unless there is an accepted root cause and the latest patch passed.

Backend health check: <http://localhost:8000/health>. Demo services are on `8001`–`8004`, Prometheus on `9090`, Grafana on `3000`, Loki on `3100`.

## Tests

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest              # tests needing Postgres skip themselves if it's down
.\.venv\Scripts\python.exe -m pytest -m "not integration"

cd ..\frontend
npm test                                          # dashboard view logic, Node's built-in test runner
```

202 backend tests and 21 frontend tests. The sandbox runner tests run real `git`, `ruff`, and `pytest` subprocesses, so they need `git` on PATH (they skip without it).

## Status

Built and verified end to end: alert ingress, parallel evidence gathering, the bounded hypothesis loop, incident-memory retrieval against pgvector, the fix loop (planning, patch generation as a reviewable diff, a bounded patch/test retry cycle), the human approval gate, the dashboard, and one real fault scenario.

A run pauses for a human decision. Approval is only possible when there is an accepted root cause and the latest patch passed its tests, and that rule is checked in four places before anything reaches GitHub. After the decision, Argus opens a PR (approved) or an issue (rejected), writes a postmortem from the recorded evidence and posts it there, sends a Slack summary, and stores the incident for future similarity matches. **GitHub PRs, issues, and postmortem comments have run against real GitHub;** Slack is tested only against a fake.

Generated patches are tested in a throwaway copy of the service with `git apply`, `ruff`, and `pytest`, so the real code is never touched. That is isolation, not a security sandbox: the patched code runs as the backend's own OS user, with secrets removed from its environment. A container-based runner is planned.

Known limits, kept deliberately:

- **One real scenario.** Redis connection exhaustion runs for real; the other six only raise their alert. `payments` is a real service with a real test; `api-gateway`, `auth`, and `inventory` are still health-check stubs with no tests, so no patch against them can pass.
- **Incidents live in memory.** A backend restart loses any incident waiting for approval, and its progress history. A Postgres-backed store is the planned fix.
- **The dashboard runs in development mode**, where Vite forwards `/api` and `/ws` to the backend. A deployed setup needs a static server or reverse proxy.
- **A local 8B model is weaker than a paid frontier model.** That's the point of `LLM_PROVIDER`: the pipeline doesn't change, only the setting.
