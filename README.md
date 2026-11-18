# Operation AI Squad

Operation AI Squad is a local-first mission-planning and AI council application. It has a FastAPI backend, a responsive browser dashboard, SQLite persistence, and an Ollama-backed analysis/discussion flow.

**Door Kickers 2 is not connected.** There is no supported game-state reader or in-game command interface in this project. Mission information is entered by the operator and the dashboard is clearly marked simulation-only. See [GAME_INTEGRATION_RESEARCH.md](./GAME_INTEGRATION_RESEARCH.md).

## Features

- Create and update missions, operator rosters, equipment, confirmed threats, unknowns, and observations.
- Preserve immutable mission snapshots for each council meeting and append-only mission/vote history in SQLite.
- Ask three distinct council roles for independent structured analysis and peer-aware discussion through the configured AI provider.
- Record initial votes and explained vote revisions manually. The AI does not cast votes or issue orders.
- Calculate a majority result, show missing voters without fabricating votes, and record a commander decision.
- Resume in-progress meetings after restarting the service.
- View provider availability and receive live mission/council updates over WebSocket.
- Browse the interactive API reference at `/docs`.

## Requirements

- Python 3.10 or later (the current project was tested with Python 3.14).
- Ollama running locally with the configured model for live AI analysis. The default is `qwen2.5:3b`.
- Door Kickers 2 is not required to run this application.

## Quick start (Windows PowerShell)

From the project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

For development and tests, install the additional test dependency:

```powershell
python -m pip install -r requirements-dev.txt
```

Install and start Ollama separately, then download the default model:

```powershell
ollama pull qwen2.5:3b
```

If Ollama listens somewhere other than `http://127.0.0.1:11434`, configure its HTTP endpoint for the current PowerShell session:

```powershell
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
```

Start the application bound to the local computer:

```powershell
python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the dashboard or [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for the API reference.

The database is created as `ai_squad.sqlite3` in the current directory. Set `AI_SQUAD_DB_PATH` to use another file:

```powershell
$env:AI_SQUAD_DB_PATH = "C:\Data\ai-squad.sqlite3"
```

The application stores no provider API keys. Mission and council data is saved locally in SQLite; protect or back up that file according to your needs.

## Using the dashboard

1. Create a mission and enter the information available to you. Keep observations, confirmed facts, assumptions, and unknowns distinct.
2. Create a council meeting with one exact motion. Each agent receives the same saved mission snapshot.
3. Start analysis and run each available agent. Provider failures or malformed responses are shown as errors; they are not replaced with guessed analyses.
4. Start discussion after at least two analyses have been recorded. Ask participating agents to consider their peers' actual analyses.
5. Start voting and record each member's vote manually. Votes can be revised with an explanation. The result identifies any missing voters.
6. Review the majority result and record a commander decision. Authorizing an unapproved motion is rejected by the backend.

Meetings and mission updates are persisted as they are made and can be continued after a service restart. Creating another meeting is a separate operator action; requesting one does not automatically start it.

## API outline

| Route | Purpose |
| --- | --- |
| `GET /api/status` | Service health |
| `GET /api/agents` | Council seats and roles |
| `GET /api/providers` | Provider status by agent |
| `GET /api/missions` | List saved missions |
| `POST /api/missions` | Create a mission |
| `GET/PATCH /api/missions/{mission_id}` | Read or update mission state |
| `GET /api/missions/{mission_id}/events` | Read append-only mission events |
| `GET/POST /api/missions/{mission_id}/meetings` | List or create council meetings |
| `/api/meetings/{meeting_id}/...` | Progress a meeting through analysis, discussion, voting, and commander decision |
| `WS /ws/events` | Live update notifications |

The generated OpenAPI specification is available at `/openapi.json`.

## Configuration and troubleshooting

- If the provider panel reports Ollama unavailable, start the Ollama service and check `OLLAMA_BASE_URL`.
- If it reports the model missing, run `ollama pull qwen2.5:3b`.
- AI generation may take time on a CPU-only machine. Provider timeouts and invalid responses are returned as explicit API errors.
- Bind to `127.0.0.1` for local use. The API has no authentication; **do not expose it to a public or untrusted network**.
- WebSocket live updates require the declared `websockets` runtime dependency, installed by `requirements.txt`.

## Tests

Run the backend unit and API integration suite:

```powershell
python -m unittest discover -v
```

The tests use controlled provider responses and do not require Ollama or the game. A real Ollama smoke test was separately completed during development; local provider availability can change.

## Project layout

```text
backend/                 Council, mission state, providers, SQLite, FastAPI, tests
frontend/index.html      Dashboard document
frontend/assets/         Dashboard JavaScript and styles
AUDIT.md                 Initial audit and implementation status
GAME_INTEGRATION_RESEARCH.md
requirements.txt         Runtime dependencies
requirements-dev.txt     Test dependencies
```
