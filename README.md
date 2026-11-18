# Operation AI Squad

Operation AI Squad is a local-first mission-planning and AI council application. It has a FastAPI backend, a responsive browser dashboard, SQLite persistence, and an Ollama-backed analysis/discussion flow.

The app can detect whether `DoorKickers2.exe` is running on Windows, but this is only process detection, not a game connection. There is no supported mission-state reader or in-game command interface in this project. Mission information is entered by the operator and labeled as such. See [GAME_INTEGRATION_RESEARCH.md](./GAME_INTEGRATION_RESEARCH.md).

## Features

- Create and update missions, operator rosters, equipment, confirmed threats, unknowns, and observations.
- Preserve immutable mission snapshots and append-only council history in SQLite, including every analysis, discussion round, vote/revision, result, and Commander decision.
- Ask three distinct council roles for separate structured analyses. The default roles all use the same configured Ollama provider and model; role prompts are not three independent AI models.
- Run bounded, multi-round peer discussion (three rounds by default), preserving each actual response and stopping if a participating agent has not responded.
- Require each analysis to include a YES/NO recommendation, confidence, supporting evidence, concerns, missing information, assumptions, and alternatives. Highlight low confidence and unresolved disagreements for Commander review.
- Search prior observations, threats, decisions, orders, risks, and council disagreements. Results include source/date and are explicitly historical; they are not promoted to current mission facts.
- Record initial votes and explained vote revisions manually. The AI does not cast votes or issue orders.
- Calculate a majority result, show missing voters without fabricating votes, and record a commander decision.
- Resume in-progress meetings after restarting the service, including an interrupted discussion round.
- View provider/model availability, request failures, and CPU-inference processing status; inference requests are serialized.
- See a five-second, read-only Windows process check for `DoorKickers2.exe`, including its PID, clearly separate from mission-state detection.
- Use a modular `GameBridge` contract for future supported readers. The live Windows bridge always reports mission data unavailable; `backend/fixtures/mock_game_status.json` is a clearly labeled development/test fixture and is never used as live telemetry.
- Protect local access with a configured token, authenticated HTTP-only session cookie, CSRF/origin checks, and authenticated WebSocket connections. Requests from non-loopback clients are rejected.
- Browse the interactive API reference at `/docs`.

## Requirements

- Python 3.10 or later (the current project was tested with Python 3.14).
- Ollama running locally with the configured model for live AI analysis. The default is `qwen2.5:3b`.
- Door Kickers 2 is not required to run this application. Process detection is available only on Windows.

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

Configure a private local access token and optional inference/debate limits:

```powershell
$env:AI_SQUAD_ACCESS_TOKEN = (& py -3 -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$env:OLLAMA_TIMEOUT_SECONDS = "120"
$env:AI_SQUAD_MAX_DISCUSSION_ROUNDS = "3"
```

Keep the access token private. The service refuses to start without one.

If Ollama listens somewhere other than `http://127.0.0.1:11434`, configure its HTTP endpoint for the current PowerShell session:

```powershell
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
```

Start the application bound to the local computer:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
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
3. Optionally search mission history. Matching, source-labeled records are passed separately to the agents as historical context, never as current facts.
4. Start analysis and run each available agent. Each response records its actual provider/model. Provider failures or malformed responses are shown as errors; they are not replaced with guessed analyses.
5. Start discussion after at least two analyses have been recorded. Each participating agent receives the other agents' actual analyses; round 3 onward also receives prior-round responses. Complete each configured round before moving to voting.
6. Record each member's vote manually. Votes can be revised with an explanation. The result identifies any missing voters.
7. Review confidence and unresolved disagreements, then record a Commander decision. Authorizing an unapproved motion is rejected by the backend.

Meetings and mission updates are persisted as they are made and can be continued after a service restart. Creating another meeting is a separate operator action; requesting one does not automatically start it.

## API outline

| Route | Purpose |
| --- | --- |
| `GET /api/status` | Service health |
| `GET /api/game/status` | Read-only game process status; mission data remains unavailable |
| `GET /api/agents` | Council seats and roles |
| `GET /api/providers` | Provider status by agent |
| `POST /api/auth/login` | Exchange the configured access token for a local session |
| `GET /api/missions` | List saved missions |
| `POST /api/missions` | Create a mission |
| `GET/PATCH /api/missions/{mission_id}` | Read or update mission state |
| `GET /api/missions/{mission_id}/events` | Read append-only mission events |
| `GET /api/missions/{mission_id}/history/search?query=...` | Search source- and date-labeled historical records |
| `GET/POST /api/missions/{mission_id}/meetings` | List or create council meetings |
| `/api/meetings/{meeting_id}/...` | Progress a meeting through analysis, bounded discussion rounds, voting, and Commander decision |
| `GET /api/processing` | Read current serialized-model processing status |
| `WS /ws/events` | Live update notifications |

The generated OpenAPI specification is available at `/openapi.json`.

## Configuration and troubleshooting

- If the provider panel reports Ollama unavailable, start the Ollama service and check `OLLAMA_BASE_URL`.
- If it reports the model missing, run `ollama pull qwen2.5:3b`.
- AI generation may take time on a CPU-only machine. Set `OLLAMA_TIMEOUT_SECONDS` to adjust the request timeout; invalid values prevent startup.
- Set `AI_SQUAD_MAX_DISCUSSION_ROUNDS` to an integer from 1 to 8. The default is 3.
- The default model is `qwen2.5:3b`; all three roles use that same model/provider unless the application is configured with a different provider factory. The dashboard identifies shared provider/model assignments and does not claim model independence.
- The service defaults to loopback binding and also rejects non-loopback clients. Do not place it behind a public or untrusted network proxy.
- Browser/API data access requires the configured token. State-changing requests require same-origin and CSRF validation; WebSockets require an authenticated local session and matching origin.
- Legacy database records without provider provenance are labeled as legacy. Existing in-progress legacy meetings may be readable but cannot be advanced into voting when their recommendations/provenance do not satisfy current validation.
- WebSocket live updates require the declared `websockets` runtime dependency, installed by `requirements.txt`.

## Tests

Run the backend unit and API integration suite:

```powershell
python -m unittest discover -v
```

The tests use controlled provider responses and do not require Ollama or the game. Local provider availability can change.

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
