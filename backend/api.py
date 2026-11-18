"""FastAPI application for missions and the democratic council workflow."""

import asyncio
import hmac
import ipaddress
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import urlsplit

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.agents import CouncilAgent
from backend.council import (
    CommanderOption,
    CouncilMeeting,
    MeetingState,
    VoteChoice,
)
from backend.mission_state import MissionState, MissionStatus, Operator
from backend.providers import (
    AIProvider,
    InvalidProviderResponseError,
    ModelUnavailableError,
    OllamaProvider,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from backend.storage import PersistenceError, SQLiteStore, StoredMeeting


_AGENT_PROFILES = {
    "squad_leader": (
        "Squad Leader",
        "Coordinator",
        (
            "Coordinate mission planning",
            "Summarize disagreements",
            "Prepare council decisions",
        ),
    ),
    "tactical_advisor": (
        "Tactical Advisor",
        "Risk Reviewer",
        (
            "Identify risks",
            "Challenge assumptions",
            "Suggest safer alternatives",
        ),
    ),
    "intelligence_officer": (
        "Intelligence Officer",
        "Information Analyst",
        (
            "Analyze supplied intelligence",
            "Identify missing information",
            "Compare possible approaches",
        ),
    ),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("string values must not be blank")
        return value


MissionText = Annotated[str, Field(min_length=1, max_length=512)]


class MissionCreate(StrictModel):
    mission_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=4096)


class OperatorInput(StrictModel):
    operator_id: str = Field(min_length=1, max_length=128)
    status: str | None = Field(default=None, max_length=256)
    assignment: str | None = Field(default=None, max_length=256)
    available: bool | None = None


class MissionUpdate(StrictModel):
    objective: str | None = Field(default=None, max_length=4096)
    status: MissionStatus | None = None
    operators: list[OperatorInput] | None = Field(default=None, max_length=100)
    available_equipment: list[MissionText] | None = Field(
        default=None, max_length=100
    )
    known_threats: list[MissionText] | None = Field(default=None, max_length=100)
    unknown_information: list[MissionText] | None = Field(
        default=None, max_length=100
    )
    observations: list[MissionText] | None = Field(default=None, max_length=100)


class MeetingCreate(StrictModel):
    motion: str = Field(min_length=1, max_length=4096)
    meeting_id: str | None = Field(default=None, min_length=1, max_length=128)


class AgentAction(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    history_query: str | None = Field(default=None, max_length=256)


class InitialVote(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    choice: VoteChoice


class VoteRevision(StrictModel):
    choice: VoteChoice
    explanation: str = Field(min_length=1, max_length=4096)


class CommanderAction(StrictModel):
    option: CommanderOption
    explanation: str = Field(default="", max_length=4096)


class LoginRequest(StrictModel):
    token: str = Field(min_length=1, max_length=512)


class EventHub:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        self._connections[websocket] = session_id

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    async def disconnect_session(self, session_id: str) -> None:
        for websocket, owner in tuple(self._connections.items()):
            if owner == session_id:
                self.disconnect(websocket)
                try:
                    await websocket.close(code=4401)
                except (WebSocketDisconnect, RuntimeError):
                    pass

    async def broadcast(self, event: dict[str, object]) -> None:
        disconnected: list[WebSocket] = []
        for websocket in tuple(self._connections):
            try:
                await websocket.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                disconnected.append(websocket)
        for websocket in disconnected:
            self.disconnect(websocket)


def create_app(
    database_path: str | Path | None = None,
    provider_factory: Callable[[str], AIProvider] | None = None,
    access_token: str | None = None,
) -> FastAPI:
    resolved_database_path = database_path or os.environ.get(
        "AI_SQUAD_DB_PATH", "ai_squad.sqlite3"
    )
    configured_token = access_token or os.environ.get("AI_SQUAD_ACCESS_TOKEN")
    if configured_token is not None and (
        not configured_token.isascii()
        or not configured_token.strip()
        or len(configured_token) > 512
    ):
        raise ValueError(
            "AI_SQUAD_ACCESS_TOKEN must be non-empty ASCII text of at most 512 characters"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not configured_token:
            raise RuntimeError(
                "AI_SQUAD_ACCESS_TOKEN must be configured before startup"
            )
        store = SQLiteStore(resolved_database_path)
        provider_maker = provider_factory or _ollama_provider_factory()
        app.state.store = store
        app.state.lock = asyncio.Lock()
        app.state.event_hub = EventHub()
        app.state.missions = {}
        app.state.meetings = {}
        app.state.sessions = {}
        app.state.access_token = configured_token
        app.state.inference_lock = asyncio.Lock()
        app.state.max_discussion_rounds = _configured_discussion_rounds()
        app.state.processing = {"active": False, "agent_id": None, "task": None}
        app.state.agent_request_status = {
            seat_id: {"status": "idle", "detail": ""}
            for seat_id in _AGENT_PROFILES
        }
        app.state.agents = {
            seat_id: CouncilAgent(
                seat_id=seat_id,
                name=profile[0],
                role=profile[1],
                responsibilities=profile[2],
                provider=provider_maker(seat_id),
            )
            for seat_id, profile in _AGENT_PROFILES.items()
        }
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Operation AI Squad API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def enforce_local_authentication(request: Request, call_next):
        client = request.client
        try:
            if client is None or not ipaddress.ip_address(client.host).is_loopback:
                return _json_error(403, "Loopback access only")
        except ValueError:
            return _json_error(403, "Loopback access only")

        if request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
            return _json_error(403, "Loopback access only")

        if request.url.path.startswith("/api/") and request.url.path != "/api/auth/login":
            session_id = request.cookies.get("ai_squad_session", "")
            session = request.app.state.sessions.get(session_id)
            if session is not None and session["expires_at"] <= time.time():
                request.app.state.sessions.pop(session_id, None)
                session = None
            if session is None:
                return _json_error(401, "Authentication required")
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.url.netloc}"
                csrf_token = request.headers.get("x-csrf-token", "")
                if (
                    origin != expected_origin
                    or not csrf_token.isascii()
                    or not hmac.compare_digest(csrf_token, session["csrf_token"])
                ):
                    return _json_error(403, "CSRF validation failed")
        return await call_next(request)

    frontend_directory = Path(__file__).parent.parent / "frontend"

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(frontend_directory / "index.html")

    @app.post("/api/auth/login")
    async def login(body: LoginRequest, request: Request) -> object:
        if not body.token.isascii() or not hmac.compare_digest(
            body.token, request.app.state.access_token
        ):
            raise HTTPException(status_code=401, detail="Invalid access token")
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        request.app.state.sessions[session_id] = {
            "csrf_token": csrf_token,
            "expires_at": time.time() + 43200,
        }
        result = JSONResponse({"csrf_token": csrf_token})
        result.set_cookie(
            "ai_squad_session",
            session_id,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=43200,
            path="/",
        )
        return result

    @app.get("/api/auth/session")
    async def auth_session(request: Request) -> object:
        session = request.app.state.sessions[request.cookies["ai_squad_session"]]
        if session["expires_at"] <= time.time():
            raise HTTPException(status_code=401, detail="Authentication required")
        return {"csrf_token": session["csrf_token"]}

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> object:
        session_id = request.cookies.get("ai_squad_session", "")
        request.app.state.sessions.pop(session_id, None)
        await request.app.state.event_hub.disconnect_session(session_id)
        result = JSONResponse({"status": "logged_out"})
        result.delete_cookie("ai_squad_session", path="/", samesite="strict")
        return result

    @app.get("/api/status")
    async def application_status() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/agents")
    async def council_agents() -> dict[str, object]:
        return {
            "agents": [
                {
                    "agent_id": agent_id,
                    "name": profile[0],
                    "role": profile[1],
                    "responsibilities": profile[2],
                }
                for agent_id, profile in _AGENT_PROFILES.items()
            ]
        }

    @app.get("/api/providers")
    async def provider_statuses(request: Request) -> dict[str, object]:
        agents = request.app.state.agents
        agent_ids = tuple(agents)
        statuses = await asyncio.gather(
            *(
                run_in_threadpool(agent.provider.get_status)
                for agent in agents.values()
            )
        )
        identity_counts: dict[tuple[str, str], int] = {}
        for status in statuses:
            identity = (status.provider, status.model)
            identity_counts[identity] = identity_counts.get(identity, 0) + 1
        return {
            "providers": [
                {
                    "agent_id": agent_id,
                    "shared_with": identity_counts[
                        (status.provider, status.model)
                    ],
                    "availability": (
                        "simulated"
                        if getattr(agents[agent_id].provider, "simulated", False)
                        else (
                            "available"
                            if status.available and status.model_available
                            else "provider_unavailable"
                            if not status.available
                            else "model_unavailable"
                        )
                    ),
                    "request_status": request.app.state.agent_request_status[
                        agent_id
                    ]["status"],
                    "request_detail": request.app.state.agent_request_status[
                        agent_id
                    ]["detail"],
                    **jsonable_encoder(status),
                }
                for agent_id, status in zip(agent_ids, statuses)
            ]
        }

    @app.get("/api/processing")
    async def processing_status(request: Request) -> object:
        return {
            **request.app.state.processing,
            "agents": request.app.state.agent_request_status,
        }

    @app.get("/api/providers/{agent_id}/status")
    async def provider_status(agent_id: str, request: Request) -> object:
        agent = request.app.state.agents.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Unknown agent")
        status = await run_in_threadpool(agent.provider.get_status)
        return jsonable_encoder(status)

    @app.post("/api/missions", status_code=201)
    async def create_mission(body: MissionCreate, request: Request) -> object:
        async with request.app.state.lock:
            if request.app.state.store.load_mission(body.mission_id) is not None:
                raise HTTPException(status_code=409, detail="Mission already exists")
            mission = MissionState(body.mission_id, body.name, body.objective)
            request.app.state.store.save_mission(mission.snapshot)
            request.app.state.missions[body.mission_id] = mission
            result = jsonable_encoder(mission.snapshot)
        await _broadcast(
            request,
            "mission.created",
            {"mission_id": body.mission_id},
        )
        return result

    @app.get("/api/missions")
    async def list_missions(request: Request) -> object:
        async with request.app.state.lock:
            missions = request.app.state.store.list_missions()
            request.app.state.missions.update(
                {
                    mission.mission_id: MissionState.from_snapshot(mission)
                    for mission in missions
                    if mission.mission_id not in request.app.state.missions
                }
            )
            return {"missions": jsonable_encoder(missions)}

    @app.get("/api/missions/{mission_id}")
    async def get_mission(mission_id: str, request: Request) -> object:
        async with request.app.state.lock:
            mission = _get_mission_state(request, mission_id)
            return jsonable_encoder(mission.snapshot)

    @app.patch("/api/missions/{mission_id}")
    async def update_mission(
        mission_id: str, body: MissionUpdate, request: Request
    ) -> object:
        async with request.app.state.lock:
            current = _get_mission_state(request, mission_id)
            changes = body.model_dump(exclude_unset=True)
            if not any(value is not None for value in changes.values()):
                raise HTTPException(status_code=422, detail="No mission updates supplied")
            updated = MissionState.from_snapshot(current.snapshot)
            try:
                if body.objective is not None:
                    updated.update_objective(body.objective)
                if body.operators is not None:
                    updated.set_operators(
                        tuple(
                            Operator(
                                operator_id=operator.operator_id,
                                status=operator.status,
                                assignment=operator.assignment,
                                available=operator.available,
                            )
                            for operator in body.operators
                        )
                    )
                if body.available_equipment is not None:
                    updated.set_available_equipment(tuple(body.available_equipment))
                for threat in body.known_threats or ():
                    updated.add_known_threat(threat)
                for unknown in body.unknown_information or ():
                    updated.add_unknown_information(unknown)
                for observation in body.observations or ():
                    updated.add_observation(observation)
                if body.status is not None:
                    updated.set_status(body.status)
                request.app.state.store.save_mission(updated.snapshot)
            except PersistenceError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            request.app.state.missions[mission_id] = updated
            result = jsonable_encoder(updated.snapshot)
        await _broadcast(
            request,
            "mission.updated",
            {"mission_id": mission_id},
        )
        return result

    @app.get("/api/missions/{mission_id}/events")
    async def mission_events(mission_id: str, request: Request) -> object:
        async with request.app.state.lock:
            mission = _get_mission_state(request, mission_id)
            return {
                "mission_id": mission_id,
                "events": jsonable_encoder(mission.snapshot.events),
            }

    @app.get("/api/missions/{mission_id}/history/search")
    async def search_mission_history(
        mission_id: str,
        request: Request,
        query: str = Query(min_length=1, max_length=256),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> object:
        if not query.strip():
            raise HTTPException(status_code=422, detail="query must not be blank")
        async with request.app.state.lock:
            _get_mission_state(request, mission_id)
            results = request.app.state.store.search_mission_history(
                query, mission_id, limit
            )
            return {
                "mission_id": mission_id,
                "query": query,
                "results": jsonable_encoder(results),
            }

    @app.post("/api/missions/{mission_id}/meetings", status_code=201)
    async def create_meeting(
        mission_id: str, body: MeetingCreate, request: Request
    ) -> object:
        async with request.app.state.lock:
            mission = _get_mission_state(request, mission_id)
            meeting_id = body.meeting_id or uuid.uuid4().hex
            if (
                meeting_id in request.app.state.meetings
                or request.app.state.store.load_meeting(meeting_id) is not None
            ):
                raise HTTPException(status_code=409, detail="Meeting already exists")
            meeting = CouncilMeeting(
                body.motion,
                mission_snapshot=mission.snapshot,
                max_discussion_rounds=request.app.state.max_discussion_rounds,
            )
            meeting.start()
            request.app.state.store.save_meeting(meeting_id, mission_id, meeting)
            request.app.state.meetings[meeting_id] = meeting
            result = _meeting_response(meeting_id, mission_id, meeting)
        await _broadcast(
            request,
            "council.meeting_created",
            {"mission_id": mission_id, "meeting_id": meeting_id},
        )
        return jsonable_encoder(result)

    @app.get("/api/missions/{mission_id}/meetings")
    async def list_mission_meetings(
        mission_id: str, request: Request
    ) -> object:
        async with request.app.state.lock:
            _get_mission_state(request, mission_id)
            meetings = request.app.state.store.list_meetings(mission_id)
            return {"meetings": jsonable_encoder(meetings)}

    @app.post("/api/meetings/{meeting_id}/analysis/start")
    async def start_analysis(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.start_analysis()
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _save_active_meeting(request, meeting_id, meeting)
        await _broadcast(
            request,
            "council.analysis_started",
            {"meeting_id": meeting_id},
        )
        return {"meeting_id": meeting_id, "state": meeting.state.value}

    @app.get("/api/meetings/{meeting_id}")
    async def get_meeting(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = request.app.state.meetings.get(meeting_id)
            if meeting is not None:
                mission_id = meeting.mission_snapshot.mission_id
                return jsonable_encoder(
                    _meeting_response(meeting_id, mission_id, meeting)
                )
            stored = request.app.state.store.load_meeting(meeting_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="Meeting not found")
            return jsonable_encoder(stored)

    @app.post("/api/meetings/{meeting_id}/analysis")
    async def submit_analysis(
        meeting_id: str, body: AgentAction, request: Request
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            agent = _get_agent(request, body.agent_id)
            historical_context = request.app.state.store.search_mission_history(
                body.history_query or meeting.motion,
                meeting.mission_snapshot.mission_id,
                limit=8,
            )
            try:
                analysis = await _run_inference(
                    request,
                    agent,
                    "analysis",
                    lambda active_meeting: agent.analyze(
                        active_meeting, historical_context
                    ),
                    meeting,
                )
            except ProviderTimeoutError as exc:
                raise HTTPException(status_code=504, detail=str(exc)) from exc
            except ModelUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except ProviderUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except InvalidProviderResponseError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            _save_active_meeting(request, meeting_id, meeting)
        await _broadcast(
            request,
            "council.analysis_submitted",
            {"meeting_id": meeting_id, "agent_id": body.agent_id},
        )
        return jsonable_encoder(analysis)

    @app.post("/api/meetings/{meeting_id}/discussion/start")
    async def start_discussion(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.start_discussion()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _save_active_meeting(request, meeting_id, meeting)
            result = _meeting_response(
                meeting_id, meeting.mission_snapshot.mission_id, meeting
            )
        await _broadcast(
            request,
            "council.discussion_started",
            {"meeting_id": meeting_id},
        )
        return jsonable_encoder(result)

    @app.post("/api/meetings/{meeting_id}/discussion/next")
    async def start_next_discussion_round(
        meeting_id: str, request: Request
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.start_next_discussion_round()
                _save_active_meeting(request, meeting_id, meeting)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            result = _meeting_response(
                meeting_id, meeting.mission_snapshot.mission_id, meeting
            )
        await _broadcast(
            request,
            "council.discussion_round_started",
            {"meeting_id": meeting_id, "round_number": meeting.discussion_round},
        )
        return jsonable_encoder(result)

    @app.post("/api/meetings/{meeting_id}/discussion")
    async def submit_discussion(
        meeting_id: str, body: AgentAction, request: Request
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            agent = _get_agent(request, body.agent_id)
            historical_context = request.app.state.store.search_mission_history(
                body.history_query or meeting.motion,
                meeting.mission_snapshot.mission_id,
                limit=8,
            )
            try:
                discussion = await _run_inference(
                    request,
                    agent,
                    "discussion",
                    lambda active_meeting: agent.discuss(
                        active_meeting, historical_context
                    ),
                    meeting,
                )
            except ProviderTimeoutError as exc:
                raise HTTPException(status_code=504, detail=str(exc)) from exc
            except ModelUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except ProviderUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except InvalidProviderResponseError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            _save_active_meeting(request, meeting_id, meeting)
        await _broadcast(
            request,
            "council.discussion_submitted",
            {"meeting_id": meeting_id, "agent_id": body.agent_id},
        )
        return jsonable_encoder(discussion)

    @app.post("/api/meetings/{meeting_id}/votes/start")
    async def start_initial_voting(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.start_initial_voting()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _save_active_meeting(request, meeting_id, meeting)
        await _broadcast(
            request,
            "council.initial_voting_started",
            {"meeting_id": meeting_id},
        )
        return {"meeting_id": meeting_id, "state": meeting.state.value}

    @app.post("/api/meetings/{meeting_id}/votes")
    async def submit_initial_vote(
        meeting_id: str, body: InitialVote, request: Request
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.submit_initial_vote(body.agent_id, body.choice)
                _save_active_meeting(request, meeting_id, meeting)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        await _broadcast(
            request,
            "council.vote_submitted",
            {"meeting_id": meeting_id, "agent_id": body.agent_id},
        )
        return {"meeting_id": meeting_id, "agent_id": body.agent_id, "choice": body.choice}

    @app.post("/api/meetings/{meeting_id}/revision/start")
    async def start_revision(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.start_revision()
                _save_active_meeting(request, meeting_id, meeting)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _broadcast(
            request,
            "council.revision_started",
            {"meeting_id": meeting_id},
        )
        return {"meeting_id": meeting_id, "state": meeting.state.value}

    @app.post("/api/meetings/{meeting_id}/votes/{agent_id}/revision")
    async def revise_vote(
        meeting_id: str,
        agent_id: str,
        body: VoteRevision,
        request: Request,
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                meeting.revise_vote(agent_id, body.choice, body.explanation)
                _save_active_meeting(request, meeting_id, meeting)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        await _broadcast(
            request,
            "council.vote_revised",
            {"meeting_id": meeting_id, "agent_id": agent_id},
        )
        return {"meeting_id": meeting_id, "agent_id": agent_id, "choice": body.choice}

    @app.post("/api/meetings/{meeting_id}/finalize")
    async def finalize_meeting(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            try:
                result = meeting.finalize()
                _save_active_meeting(request, meeting_id, meeting)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _broadcast(
            request,
            "council.finalized",
            {"meeting_id": meeting_id, "approved": result.approved},
        )
        return jsonable_encoder(result)

    @app.get("/api/meetings/{meeting_id}/result")
    async def meeting_result(meeting_id: str, request: Request) -> object:
        async with request.app.state.lock:
            meeting = request.app.state.meetings.get(meeting_id)
            if meeting is not None:
                try:
                    return jsonable_encoder(meeting.result)
                except RuntimeError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            stored = request.app.state.store.load_meeting(meeting_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="Meeting not found")
            if stored.result is None:
                raise HTTPException(status_code=409, detail="Meeting is not finalized")
            return jsonable_encoder(stored.result)

    @app.post("/api/meetings/{meeting_id}/commander-decision")
    async def commander_decision(
        meeting_id: str, body: CommanderAction, request: Request
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            mission_id = meeting.mission_snapshot.mission_id
            mission = _get_mission_state(request, mission_id)
            updated_mission = MissionState.from_snapshot(mission.snapshot)
            try:
                meeting.decide(body.option, body.explanation)
                updated_mission.record_council_decision(
                    f"{meeting.motion}: {body.option.value}"
                )
                request.app.state.store.save_mission(updated_mission.snapshot)
                _save_active_meeting(request, meeting_id, meeting)
            except PersistenceError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            request.app.state.missions[mission_id] = updated_mission
            result = meeting.commander_decision
        await _broadcast(
            request,
            "council.commander_decision",
            {
                "meeting_id": meeting_id,
                "mission_id": mission_id,
                "option": body.option.value,
            },
        )
        return jsonable_encoder(result)

    @app.websocket("/ws/events")
    async def live_events(websocket: WebSocket) -> None:
        try:
            peer_is_local = ipaddress.ip_address(
                websocket.client.host if websocket.client else ""
            ).is_loopback
        except ValueError:
            peer_is_local = False
        origin = urlsplit(websocket.headers.get("origin", ""))
        expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
        request_host = urlsplit(f"//{websocket.headers.get('host', '')}")
        session_id = websocket.cookies.get("ai_squad_session", "")
        host_is_local = websocket.url.hostname in ("127.0.0.1", "localhost", "::1")
        try:
            origin_port = origin.port or (443 if origin.scheme == "https" else 80)
            request_port = request_host.port or (
                443 if expected_scheme == "https" else 80
            )
            origin_matches = (
                origin.scheme == expected_scheme
                and origin.hostname == request_host.hostname
                and origin_port == request_port
            )
        except ValueError:
            origin_matches = False
        if not peer_is_local:
            await websocket.close(code=4403)
            return
        if not host_is_local:
            await websocket.close(code=4403)
            return
        if not origin_matches:
            await websocket.close(code=4403)
            return
        session = websocket.app.state.sessions.get(session_id)
        if session is None or session["expires_at"] <= time.time():
            await websocket.close(code=4401)
            return
        hub: EventHub = websocket.app.state.event_hub
        await hub.connect(websocket, session_id)
        try:
            await websocket.send_json({"event": "connected"})
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_json({"event": "pong"})
        except WebSocketDisconnect:
            hub.disconnect(websocket)
        except RuntimeError:
            hub.disconnect(websocket)

    app.mount(
        "/assets",
        StaticFiles(directory=frontend_directory / "assets"),
        name="assets",
    )
    return app


def _get_mission_state(request: Request, mission_id: str) -> MissionState:
    mission = request.app.state.missions.get(mission_id)
    if mission is not None:
        return mission
    snapshot = request.app.state.store.load_mission(mission_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    mission = MissionState.from_snapshot(snapshot)
    request.app.state.missions[mission_id] = mission
    return mission


def _json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _ollama_provider_factory() -> Callable[[str], AIProvider]:
    timeout_text = os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120")
    try:
        timeout = float(timeout_text)
    except ValueError as exc:
        raise RuntimeError("OLLAMA_TIMEOUT_SECONDS must be a positive number") from exc
    try:
        OllamaProvider(timeout=timeout)
    except ValueError as exc:
        raise RuntimeError("OLLAMA_TIMEOUT_SECONDS must be a positive number") from exc
    return lambda _seat_id: OllamaProvider(timeout=timeout)


def _configured_discussion_rounds() -> int:
    value = os.environ.get("AI_SQUAD_MAX_DISCUSSION_ROUNDS", "3")
    try:
        rounds = int(value)
    except ValueError as exc:
        raise RuntimeError(
            "AI_SQUAD_MAX_DISCUSSION_ROUNDS must be an integer from 1 to 8"
        ) from exc
    if not 1 <= rounds <= 8:
        raise RuntimeError(
            "AI_SQUAD_MAX_DISCUSSION_ROUNDS must be an integer from 1 to 8"
        )
    return rounds


async def _run_inference(
    request: Request,
    agent: CouncilAgent,
    task: str,
    operation: Callable[[CouncilMeeting], object],
    meeting: CouncilMeeting,
) -> object:
    agent_status = request.app.state.agent_request_status[agent.seat_id]
    request.app.state.processing.update(
        {"active": True, "agent_id": agent.seat_id, "task": task}
    )
    agent_status.update({"status": "processing", "detail": ""})
    failed = True
    try:
        async with request.app.state.inference_lock:
            result = await run_in_threadpool(operation, meeting)
        failed = False
        return result
    except ProviderError as exc:
        agent_status["detail"] = str(exc)
        raise
    finally:
        agent_status["status"] = "request_failed" if failed else "completed"
        request.app.state.processing.update(
            {"active": False, "agent_id": None, "task": None}
        )


def _get_active_meeting(request: Request, meeting_id: str) -> CouncilMeeting:
    meeting = request.app.state.meetings.get(meeting_id)
    if meeting is not None:
        return meeting
    stored = request.app.state.store.load_meeting(meeting_id)
    if stored is not None:
        try:
            meeting = CouncilMeeting.restore(
                motion=stored.motion,
                seats=stored.seats,
                mission_snapshot=stored.mission_snapshot,
                state=stored.state,
                analyses=stored.analyses,
                discussions=stored.discussions,
                vote_history=stored.vote_history,
                commander_decision=stored.commander_decision,
                discussion_round=stored.discussion_round,
                max_discussion_rounds=stored.max_discussion_rounds,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=500,
                detail="Stored council meeting history is inconsistent",
            ) from exc
        request.app.state.meetings[meeting_id] = meeting
        return meeting
    raise HTTPException(status_code=404, detail="Meeting not found")


def _get_agent(request: Request, agent_id: str) -> CouncilAgent:
    agent = request.app.state.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Unknown agent")
    return agent


def _save_active_meeting(
    request: Request, meeting_id: str, meeting: CouncilMeeting
) -> None:
    mission_id = meeting.mission_snapshot.mission_id
    request.app.state.store.save_meeting(meeting_id, mission_id, meeting)


def _meeting_response(
    meeting_id: str, mission_id: str, meeting: CouncilMeeting
) -> dict[str, object]:
    result = None
    if meeting.state in (MeetingState.FINALIZED, MeetingState.COMMANDER_DECISION):
        result = meeting.result
    return {
        "meeting_id": meeting_id,
        "mission_id": mission_id,
        "motion": meeting.motion,
        "state": meeting.state,
        "seats": meeting.seats,
        "mission_snapshot": meeting.mission_snapshot,
        "analyses": tuple(meeting.analyses.values()),
        "discussions": tuple(meeting.discussions.values()),
        "discussion_round": meeting.discussion_round,
        "max_discussion_rounds": meeting.max_discussion_rounds,
        "unresolved_disagreements": meeting.unresolved_disagreements,
        "vote_history": meeting.vote_history,
        "commander_decision": meeting.commander_decision,
        "result": result,
    }


async def _broadcast(
    request: Request, event_name: str, data: dict[str, object]
) -> None:
    await request.app.state.event_hub.broadcast(
        {"event": event_name, "data": data}
    )


app = create_app()
