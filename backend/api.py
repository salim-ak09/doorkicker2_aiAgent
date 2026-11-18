"""FastAPI application for missions and the democratic council workflow."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
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


class MissionCreate(StrictModel):
    mission_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=4096)


class OperatorInput(StrictModel):
    operator_id: str = Field(min_length=1, max_length=128)
    status: str | None = None
    assignment: str | None = None
    available: bool | None = None


class MissionUpdate(StrictModel):
    objective: str | None = Field(default=None, max_length=4096)
    status: MissionStatus | None = None
    operators: list[OperatorInput] | None = None
    available_equipment: list[str] | None = None
    known_threats: list[str] | None = None
    unknown_information: list[str] | None = None
    observations: list[str] | None = None


class MeetingCreate(StrictModel):
    motion: str = Field(min_length=1, max_length=4096)
    meeting_id: str | None = Field(default=None, min_length=1, max_length=128)


class AgentAction(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)


class InitialVote(StrictModel):
    agent_id: str = Field(min_length=1, max_length=128)
    choice: VoteChoice


class VoteRevision(StrictModel):
    choice: VoteChoice
    explanation: str = Field(min_length=1, max_length=4096)


class CommanderAction(StrictModel):
    option: CommanderOption
    explanation: str = Field(default="", max_length=4096)


class EventHub:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)

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
) -> FastAPI:
    resolved_database_path = database_path or os.environ.get(
        "AI_SQUAD_DB_PATH", "ai_squad.sqlite3"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = SQLiteStore(resolved_database_path)
        provider_maker = provider_factory or (lambda _seat_id: OllamaProvider())
        app.state.store = store
        app.state.lock = asyncio.Lock()
        app.state.event_hub = EventHub()
        app.state.missions = {}
        app.state.meetings = {}
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

    frontend_directory = Path(__file__).parent.parent / "frontend"

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(frontend_directory / "index.html")

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
        async with request.app.state.lock:
            agents = request.app.state.agents
            agent_ids = tuple(agents)
            statuses = await asyncio.gather(
                *(
                    run_in_threadpool(agent.provider.get_status)
                    for agent in agents.values()
                )
            )
        return {
            "providers": [
                {
                    "agent_id": agent_id,
                    **jsonable_encoder(status),
                }
                for agent_id, status in zip(agent_ids, statuses)
            ]
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
            try:
                analysis = await run_in_threadpool(agent.analyze, meeting)
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

    @app.post("/api/meetings/{meeting_id}/discussion")
    async def submit_discussion(
        meeting_id: str, body: AgentAction, request: Request
    ) -> object:
        async with request.app.state.lock:
            meeting = _get_active_meeting(request, meeting_id)
            agent = _get_agent(request, body.agent_id)
            try:
                discussion = await run_in_threadpool(agent.discuss, meeting)
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
        hub: EventHub = websocket.app.state.event_hub
        await hub.connect(websocket)
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
