"""SQLite persistence for mission, council, and non-secret provider settings."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from backend.council import (
    AgentAnalysis,
    AgentDiscussion,
    CommanderDecision,
    CommanderOption,
    CouncilMeeting,
    CouncilResult,
    MeetingState,
    VoteChoice,
    VoteRecord,
)
from backend.mission_state import (
    MissionEvent,
    MissionSnapshot,
    MissionStatus,
    Operator,
)


class PersistenceError(RuntimeError):
    """Base error for persistence failures."""


class PersistenceConflictError(PersistenceError):
    """An update conflicts with immutable persisted history."""


class StoredDataError(PersistenceError):
    """Persisted data could not be decoded into application models."""


@dataclass(frozen=True)
class StoredMeeting:
    meeting_id: str
    mission_id: str
    motion: str
    state: MeetingState
    seats: tuple[str, ...]
    mission_snapshot: MissionSnapshot
    analyses: tuple[AgentAnalysis, ...]
    discussions: tuple[AgentDiscussion, ...]
    vote_history: tuple[VoteRecord, ...]
    commander_decision: CommanderDecision | None
    result: CouncilResult | None
    discussion_round: int = 0
    max_discussion_rounds: int = 3
    created_at: str = ""


@dataclass(frozen=True)
class StoredProviderConfig:
    config_id: str
    provider: str
    model: str
    base_url: str | None
    enabled: bool


class SQLiteStore:
    """Persist application history in a local SQLite database."""

    _STATE_ORDER = {
        state: index
        for index, state in enumerate(
            (
                MeetingState.IDLE,
                MeetingState.MEETING_STARTED,
                MeetingState.ANALYSIS,
                MeetingState.DISCUSSION,
                MeetingState.INITIAL_VOTING,
                MeetingState.REVISION,
                MeetingState.FINALIZED,
                MeetingState.COMMANDER_DECISION,
            )
        )
    }

    _MISSION_ITEM_FIELDS = {
        "equipment": "available_equipment",
        "known_threat": "known_threats",
        "unknown_information": "unknown_information",
        "observation": "observations",
        "council_decision": "council_decisions",
        "commander_order": "commander_orders",
    }

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(str(database_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def save_mission(self, mission: MissionSnapshot) -> None:
        if not isinstance(mission, MissionSnapshot):
            raise ValueError("mission must be a MissionSnapshot")
        with self._connection:
            existing_mission = self._connection.execute(
                "SELECT status FROM missions WHERE mission_id = ?",
                (mission.mission_id,),
            ).fetchone()
            if existing_mission is not None:
                self._validate_mission_status_transition(
                    MissionStatus(existing_mission["status"]), mission.status
                )
            self._connection.execute(
                """
                INSERT INTO missions (mission_id, name, objective, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    name = excluded.name,
                    objective = excluded.objective,
                    status = excluded.status
                """,
                (mission.mission_id, mission.name, mission.objective, mission.status.value),
            )
            self._connection.execute(
                "DELETE FROM mission_operators WHERE mission_id = ?",
                (mission.mission_id,),
            )
            self._connection.executemany(
                """
                INSERT INTO mission_operators
                    (mission_id, position, operator_id, status, assignment, available)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        mission.mission_id,
                        index,
                        operator.operator_id,
                        operator.status,
                        operator.assignment,
                        None if operator.available is None else int(operator.available),
                    )
                    for index, operator in enumerate(mission.operators)
                ],
            )

            for category, field_name in self._MISSION_ITEM_FIELDS.items():
                self._save_mission_items(
                    mission.mission_id,
                    category,
                    getattr(mission, field_name),
                    append_only=category != "equipment",
                )
            self._save_mission_events(mission.mission_id, mission.events)

    def load_mission(self, mission_id: str) -> MissionSnapshot | None:
        row = self._connection.execute(
            "SELECT mission_id, name, objective, status FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None

        operators = self._connection.execute(
            """
            SELECT operator_id, status, assignment, available
            FROM mission_operators WHERE mission_id = ? ORDER BY position
            """,
            (mission_id,),
        ).fetchall()
        items = self._connection.execute(
            """
            SELECT category, value FROM mission_items
            WHERE mission_id = ? ORDER BY category, position
            """,
            (mission_id,),
        ).fetchall()
        items_by_category: dict[str, list[str]] = {
            category: [] for category in self._MISSION_ITEM_FIELDS
        }
        for item in items:
            category = item["category"]
            if category not in items_by_category:
                raise StoredDataError("Stored mission item category is invalid")
            items_by_category[category].append(item["value"])
        events = self._connection.execute(
            """
            SELECT sequence, category, description, occurred_at
            FROM mission_events WHERE mission_id = ? ORDER BY sequence
            """,
            (mission_id,),
        ).fetchall()
        try:
            return MissionSnapshot(
                mission_id=row["mission_id"],
                name=row["name"],
                objective=row["objective"],
                status=MissionStatus(row["status"]),
                operators=tuple(
                    Operator(
                        operator_id=operator["operator_id"],
                        status=operator["status"],
                        assignment=operator["assignment"],
                        available=(
                            None
                            if operator["available"] is None
                            else bool(operator["available"])
                        ),
                    )
                    for operator in operators
                ),
                available_equipment=tuple(items_by_category["equipment"]),
                known_threats=tuple(items_by_category["known_threat"]),
                unknown_information=tuple(
                    items_by_category["unknown_information"]
                ),
                observations=tuple(items_by_category["observation"]),
                council_decisions=tuple(items_by_category["council_decision"]),
                commander_orders=tuple(items_by_category["commander_order"]),
                events=tuple(
                    MissionEvent(
                        sequence=event["sequence"],
                        category=event["category"],
                        description=event["description"],
                        occurred_at=datetime.fromisoformat(event["occurred_at"]),
                    )
                    for event in events
                ),
            )
        except (TypeError, ValueError) as exc:
            raise StoredDataError("Stored mission data is invalid") from exc

    def list_missions(self) -> tuple[MissionSnapshot, ...]:
        rows = self._connection.execute(
            "SELECT mission_id FROM missions ORDER BY mission_id"
        ).fetchall()
        missions: list[MissionSnapshot] = []
        for row in rows:
            mission = self.load_mission(row["mission_id"])
            if mission is None:
                raise StoredDataError("Stored mission disappeared while listing")
            missions.append(mission)
        return tuple(missions)

    def save_meeting(
        self,
        meeting_id: str,
        mission_id: str,
        meeting: CouncilMeeting,
    ) -> None:
        self._required_text(meeting_id, "meeting_id")
        self._required_text(mission_id, "mission_id")
        if not isinstance(meeting, CouncilMeeting):
            raise ValueError("meeting must be a CouncilMeeting")
        mission_snapshot = meeting.mission_snapshot
        if mission_snapshot is None:
            raise ValueError("meeting has no mission snapshot")
        if mission_snapshot.mission_id != mission_id:
            raise ValueError("meeting snapshot does not match mission_id")

        seats_json = json.dumps(meeting.seats)
        snapshot_json = self._mission_to_json(mission_snapshot)
        with self._connection:
            if self._connection.execute(
                "SELECT 1 FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone() is None:
                raise ValueError("mission must be saved before its meeting")

            existing = self._connection.execute(
                """
                SELECT mission_id, motion, seats_json, mission_snapshot_json, state,
                       discussion_round, max_discussion_rounds, created_at
                FROM meetings WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO meetings
                        (meeting_id, mission_id, motion, seats_json,
                         mission_snapshot_json, state, discussion_round,
                         max_discussion_rounds, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        mission_id,
                        meeting.motion,
                        seats_json,
                        snapshot_json,
                        meeting.state.value,
                        meeting.discussion_round,
                        meeting.max_discussion_rounds,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            else:
                identity = (
                    existing["mission_id"],
                    existing["motion"],
                    existing["seats_json"],
                    existing["mission_snapshot_json"],
                    existing["max_discussion_rounds"],
                )
                expected_identity = (
                    mission_id,
                    meeting.motion,
                    seats_json,
                    snapshot_json,
                    meeting.max_discussion_rounds,
                )
                if identity != expected_identity:
                    raise PersistenceConflictError(
                        "meeting motion, seats, and mission snapshot are immutable"
                    )
                stored_state = MeetingState(existing["state"])
                if self._STATE_ORDER[meeting.state] < self._STATE_ORDER[stored_state]:
                    raise PersistenceConflictError("meeting state cannot move backward")
                if meeting.discussion_round < existing["discussion_round"]:
                    raise PersistenceConflictError(
                        "discussion round cannot move backward"
                    )
                self._connection.execute(
                    """
                    UPDATE meetings
                    SET state = ?, discussion_round = ?
                    WHERE meeting_id = ?
                    """,
                    (meeting.state.value, meeting.discussion_round, meeting_id),
                )

            for analysis in meeting.analyses.values():
                self._save_immutable_row(
                    "agent_analyses",
                    ("meeting_id", "agent_id"),
                    (meeting_id, analysis.agent_id),
                    (
                        "recommendation",
                        "reasoning",
                        "facts_json",
                        "assumptions_json",
                        "main_risks",
                        "missing_information",
                        "suggested_improvements",
                        "confidence",
                        "supporting_evidence_json",
                        "main_concerns_json",
                        "alternative_recommendations_json",
                        "provider",
                        "model",
                        "response_type",
                        "completed_at",
                    ),
                    (
                        analysis.recommendation,
                        analysis.reasoning,
                        json.dumps(analysis.facts),
                        json.dumps(analysis.assumptions),
                        analysis.main_risks,
                        analysis.missing_information,
                        analysis.suggested_improvements,
                        analysis.confidence,
                        json.dumps(analysis.supporting_evidence),
                        json.dumps(analysis.main_concerns),
                        json.dumps(analysis.alternative_recommendations),
                        analysis.provider,
                        analysis.model,
                        analysis.response_type,
                        analysis.completed_at,
                    ),
                )
            for discussion in meeting.discussions.values():
                self._save_immutable_row(
                    "discussion_rounds",
                    ("meeting_id", "round_number", "agent_id"),
                    (meeting_id, discussion.round_number, discussion.agent_id),
                    (
                        "challenges",
                        "reconsideration",
                        "recommendation_changed",
                        "updated_recommendation",
                        "reasoning",
                        "confidence",
                        "agreement",
                        "disagreement",
                        "weak_reasoning",
                        "missing_information",
                        "possible_risks",
                        "unresolved_disagreements_json",
                        "provider",
                        "model",
                        "response_type",
                        "completed_at",
                    ),
                    (
                        discussion.challenges,
                        discussion.reconsideration,
                        int(discussion.recommendation_changed),
                        discussion.updated_recommendation,
                        discussion.reasoning,
                        discussion.confidence,
                        discussion.agreement,
                        discussion.disagreement,
                        discussion.weak_reasoning,
                        discussion.missing_information,
                        discussion.possible_risks,
                        json.dumps(discussion.unresolved_disagreements),
                        discussion.provider,
                        discussion.model,
                        discussion.response_type,
                        discussion.completed_at,
                    ),
                )
            self._save_votes(meeting_id, meeting.vote_history)

            decision = meeting.commander_decision
            if decision is not None:
                self._save_immutable_row(
                    "commander_decisions",
                    ("meeting_id",),
                    (meeting_id,),
                    ("option", "explanation", "decided_at"),
                    (
                        decision.option.value,
                        decision.explanation,
                        decision.decided_at,
                    ),
                )

    def load_meeting(self, meeting_id: str) -> StoredMeeting | None:
        row = self._connection.execute(
            """
            SELECT meeting_id, mission_id, motion, state, seats_json,
                   mission_snapshot_json, discussion_round, max_discussion_rounds,
                   created_at
            FROM meetings WHERE meeting_id = ?
            """,
            (meeting_id,),
        ).fetchone()
        if row is None:
            return None

        try:
            seats = tuple(json.loads(row["seats_json"]))
            mission_snapshot = self._mission_from_json(
                row["mission_snapshot_json"]
            )
            analyses_by_id = {
                item["agent_id"]: AgentAnalysis(
                    agent_id=item["agent_id"],
                    recommendation=item["recommendation"],
                    reasoning=item["reasoning"],
                    facts=tuple(json.loads(item["facts_json"])),
                    assumptions=tuple(json.loads(item["assumptions_json"])),
                    main_risks=item["main_risks"],
                    missing_information=item["missing_information"],
                    suggested_improvements=item["suggested_improvements"],
                    confidence=item["confidence"],
                    supporting_evidence=tuple(
                        json.loads(item["supporting_evidence_json"])
                    ),
                    main_concerns=tuple(json.loads(item["main_concerns_json"])),
                    alternative_recommendations=tuple(
                        json.loads(item["alternative_recommendations_json"])
                    ),
                    provider=(
                        "legacy record"
                        if item["provider"] == "unknown"
                        else item["provider"]
                    ),
                    model=item["model"],
                    response_type=(
                        "legacy"
                        if item["provider"] == "unknown"
                        and not item["completed_at"]
                        else item["response_type"]
                    ),
                    completed_at=item["completed_at"],
                )
                for item in self._connection.execute(
                    """
                    SELECT agent_id, recommendation, reasoning, facts_json,
                           assumptions_json, main_risks, missing_information,
                           suggested_improvements, confidence,
                           supporting_evidence_json, main_concerns_json,
                           alternative_recommendations_json, provider, model,
                           response_type, completed_at
                    FROM agent_analyses WHERE meeting_id = ?
                    """,
                    (meeting_id,),
                )
            }
            discussion_rows = self._connection.execute(
                """
                SELECT round_number, agent_id, challenges, reconsideration,
                       recommendation_changed, updated_recommendation,
                       reasoning, confidence, agreement, disagreement,
                       weak_reasoning, missing_information, possible_risks,
                       unresolved_disagreements_json, provider, model,
                       response_type, completed_at
                FROM discussion_rounds WHERE meeting_id = ?
                ORDER BY round_number, agent_id
                """,
                (meeting_id,),
            ).fetchall()
            if not discussion_rows:
                discussion_rows = self._connection.execute(
                    """
                    SELECT 1 AS round_number, agent_id, challenges, reconsideration,
                           recommendation_changed, updated_recommendation,
                           reasoning, confidence, 'Not stated' AS agreement,
                           'Not stated' AS disagreement,
                           'Not stated' AS weak_reasoning,
                           'Not stated' AS missing_information,
                           'Not stated' AS possible_risks,
                           '[]' AS unresolved_disagreements_json,
                           'unknown' AS provider, 'unknown' AS model,
                           'legacy' AS response_type, '' AS completed_at
                    FROM agent_discussions WHERE meeting_id = ?
                    """,
                    (meeting_id,),
                ).fetchall()
            discussions_by_round = {
                (item["round_number"], item["agent_id"]): AgentDiscussion(
                    agent_id=item["agent_id"],
                    challenges=item["challenges"],
                    reconsideration=item["reconsideration"],
                    recommendation_changed=bool(item["recommendation_changed"]),
                    updated_recommendation=item["updated_recommendation"],
                    reasoning=item["reasoning"],
                    confidence=item["confidence"],
                    round_number=item["round_number"],
                    agreement=item["agreement"],
                    disagreement=item["disagreement"],
                    weak_reasoning=item["weak_reasoning"],
                    missing_information=item["missing_information"],
                    possible_risks=item["possible_risks"],
                    unresolved_disagreements=tuple(
                        json.loads(item["unresolved_disagreements_json"])
                    ),
                    provider=item["provider"],
                    model=item["model"],
                    response_type=item["response_type"],
                    completed_at=item["completed_at"],
                )
                for item in discussion_rows
            }
            vote_history = tuple(
                VoteRecord(
                    agent_id=item["agent_id"],
                    choice=VoteChoice(item["choice"]),
                    revision=item["revision"],
                    explanation=item["explanation"],
                )
                for item in self._connection.execute(
                    """
                    SELECT agent_id, choice, revision, explanation
                    FROM votes WHERE meeting_id = ? ORDER BY vote_order
                    """,
                    (meeting_id,),
                )
            )
            decision_row = self._connection.execute(
                "SELECT option, explanation, decided_at FROM commander_decisions WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            decision = (
                None
                if decision_row is None
                else CommanderDecision(
                    option=CommanderOption(decision_row["option"]),
                    explanation=decision_row["explanation"],
                    decided_at=decision_row["decided_at"],
                )
            )
            state = MeetingState(row["state"])
            result = self._make_result(
                row["motion"], seats, vote_history, state
            )
            return StoredMeeting(
                meeting_id=row["meeting_id"],
                mission_id=row["mission_id"],
                motion=row["motion"],
                state=state,
                seats=seats,
                mission_snapshot=mission_snapshot,
                analyses=tuple(
                    analyses_by_id[seat]
                    for seat in seats
                    if seat in analyses_by_id
                ),
                discussions=tuple(
                    discussions_by_round[key]
                    for key in sorted(
                        discussions_by_round,
                        key=lambda item: (item[0], seats.index(item[1])),
                    )
                ),
                vote_history=vote_history,
                commander_decision=decision,
                result=result,
                discussion_round=(
                    row["discussion_round"]
                    or (
                        1
                        if self._STATE_ORDER[state]
                        >= self._STATE_ORDER[MeetingState.DISCUSSION]
                        else 0
                    )
                ),
                max_discussion_rounds=row["max_discussion_rounds"],
                created_at=row["created_at"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoredDataError("Stored council meeting data is invalid") from exc

    def list_meetings(self, mission_id: str | None = None) -> tuple[StoredMeeting, ...]:
        if mission_id is None:
            rows = self._connection.execute(
                "SELECT meeting_id FROM meetings ORDER BY rowid"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT meeting_id FROM meetings WHERE mission_id = ? ORDER BY rowid",
                (mission_id,),
            ).fetchall()
        meetings: list[StoredMeeting] = []
        for row in rows:
            meeting = self.load_meeting(row["meeting_id"])
            if meeting is None:
                raise StoredDataError("Stored meeting disappeared while listing")
            meetings.append(meeting)
        return tuple(meetings)

    def search_mission_history(
        self, query: str, mission_id: str | None = None, limit: int = 50
    ) -> tuple[dict[str, object], ...]:
        normalized_query = self._required_text(query, "query").casefold()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        entries: list[dict[str, object]] = []

        def add(
            mission: str,
            content: str,
            source: str,
            occurred_at: str,
        ) -> None:
            if normalized_query in content.casefold():
                entries.append(
                    {
                        "mission_id": mission,
                        "content": content,
                        "source": source,
                        "occurred_at": occurred_at
                        or "Date unavailable (legacy record)",
                        "historical": True,
                        "warning": (
                            "Historical information only; verify before treating "
                            "it as current."
                        ),
                    }
                )

        mission_filter = "" if mission_id is None else "WHERE mission_id = ?"
        mission_args = () if mission_id is None else (mission_id,)
        missions = self._connection.execute(
            f"SELECT mission_id FROM missions {mission_filter}",
            mission_args,
        ).fetchall()
        event_categories = {
            "known_threat": "mission.threat_recorded",
            "unknown_information": "mission.unknown_information_recorded",
            "observation": "mission.observation_recorded",
            "council_decision": "mission.council_decision",
            "commander_order": "mission.commander_order",
        }
        source_labels = {
            "known_threat": "confirmed threat record",
            "unknown_information": "unknown information record",
            "observation": "historical observation",
            "council_decision": "council decision",
            "commander_order": "commander order",
        }
        for mission_row in missions:
            current_mission_id = mission_row["mission_id"]
            events_by_category: dict[str, list[str]] = {}
            for event in self._connection.execute(
                """
                SELECT category, occurred_at FROM mission_events
                WHERE mission_id = ? ORDER BY sequence
                """,
                (current_mission_id,),
            ):
                events_by_category.setdefault(event["category"], []).append(
                    event["occurred_at"]
                )
            for item in self._connection.execute(
                """
                SELECT category, position, value FROM mission_items
                WHERE mission_id = ? AND category != 'equipment'
                ORDER BY category, position
                """,
                (current_mission_id,),
            ):
                category = item["category"]
                event_category = event_categories.get(category, "")
                dates = events_by_category.get(event_category, [])
                occurred_at = (
                    dates[item["position"]]
                    if item["position"] < len(dates)
                    else ""
                )
                add(
                    current_mission_id,
                    item["value"],
                    source_labels.get(category, category),
                    occurred_at,
                )

        if mission_id is None:
            meeting_rows = self._connection.execute(
                """
                SELECT meeting_id, mission_id, motion, created_at
                FROM meetings ORDER BY rowid
                """
            ).fetchall()
        else:
            meeting_rows = self._connection.execute(
                """
                SELECT meeting_id, mission_id, motion, created_at
                FROM meetings WHERE mission_id = ? ORDER BY rowid
                """,
                (mission_id,),
            ).fetchall()
        for meeting in meeting_rows:
            meeting_id = meeting["meeting_id"]
            current_mission_id = meeting["mission_id"]
            add(
                current_mission_id,
                meeting["motion"],
                f"council motion · {meeting_id}",
                meeting["created_at"],
            )
            for analysis in self._connection.execute(
                """
                SELECT agent_id, recommendation, main_risks,
                       missing_information, main_concerns_json, completed_at
                FROM agent_analyses WHERE meeting_id = ?
                """,
                (meeting_id,),
            ):
                for label, content in (
                    ("recommendation", analysis["recommendation"]),
                    ("identified risk", analysis["main_risks"]),
                    ("missing information", analysis["missing_information"]),
                    (
                        "main concerns",
                        "; ".join(json.loads(analysis["main_concerns_json"])),
                    ),
                ):
                    add(
                        current_mission_id,
                        content,
                        f"council analysis · {analysis['agent_id']} · {label}",
                        analysis["completed_at"],
                    )

            discussion_rows = self._connection.execute(
                """
                SELECT round_number, agent_id, disagreement, weak_reasoning,
                       possible_risks, unresolved_disagreements_json, completed_at
                FROM discussion_rounds WHERE meeting_id = ?
                ORDER BY round_number, agent_id
                """,
                (meeting_id,),
            ).fetchall()
            if not discussion_rows:
                discussion_rows = self._connection.execute(
                    """
                    SELECT 1 AS round_number, agent_id, challenges AS disagreement,
                           reasoning AS weak_reasoning,
                           'Not recorded' AS possible_risks,
                           '[]' AS unresolved_disagreements_json,
                           '' AS completed_at
                    FROM agent_discussions WHERE meeting_id = ?
                    """,
                    (meeting_id,),
                ).fetchall()
            for discussion in discussion_rows:
                for label, content in (
                    ("disagreement", discussion["disagreement"]),
                    ("weak reasoning", discussion["weak_reasoning"]),
                    ("possible risk", discussion["possible_risks"]),
                    (
                        "unresolved disagreement",
                        "; ".join(
                            json.loads(
                                discussion["unresolved_disagreements_json"]
                            )
                        ),
                    ),
                ):
                    add(
                        current_mission_id,
                        content,
                        (
                            f"council discussion · round "
                            f"{discussion['round_number']} · "
                            f"{discussion['agent_id']} · {label}"
                        ),
                        discussion["completed_at"],
                    )

            decision = self._connection.execute(
                """
                SELECT option, explanation, decided_at
                FROM commander_decisions WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()
            if decision is not None:
                add(
                    current_mission_id,
                    f"{decision['option']}: {decision['explanation']}",
                    f"commander decision · {meeting_id}",
                    decision["decided_at"],
                )

        entries.sort(key=lambda item: item["occurred_at"], reverse=True)
        entries.sort(
            key=lambda item: item["occurred_at"]
            == "Date unavailable (legacy record)"
        )
        return tuple(entries[:limit])

    def save_provider_config(
        self,
        config_id: str,
        provider: str,
        model: str,
        base_url: str | None = None,
        enabled: bool = True,
    ) -> None:
        config_id = self._required_text(config_id, "config_id")
        provider = self._required_text(provider, "provider")
        model = self._required_text(model, "model")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if base_url is not None:
            base_url = self._required_text(base_url, "base_url")
            parsed_url = urlsplit(base_url)
            if (
                parsed_url.scheme not in ("http", "https")
                or not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError(
                    "base_url must be HTTP(S) without credentials or query parameters"
                )
            try:
                parsed_url.port
            except ValueError as exc:
                raise ValueError("base_url contains an invalid port") from exc
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO provider_configs
                    (config_id, provider, model, base_url, enabled)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(config_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    base_url = excluded.base_url,
                    enabled = excluded.enabled
                """,
                (config_id, provider, model, base_url, int(enabled)),
            )

    def load_provider_config(
        self, config_id: str
    ) -> StoredProviderConfig | None:
        row = self._connection.execute(
            """
            SELECT config_id, provider, model, base_url, enabled
            FROM provider_configs WHERE config_id = ?
            """,
            (config_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredProviderConfig(
            config_id=row["config_id"],
            provider=row["provider"],
            model=row["model"],
            base_url=row["base_url"],
            enabled=bool(row["enabled"]),
        )

    def _save_votes(
        self, meeting_id: str, vote_history: Sequence[VoteRecord]
    ) -> None:
        stored = self._connection.execute(
            """
            SELECT agent_id, choice, revision, explanation
            FROM votes WHERE meeting_id = ? ORDER BY vote_order
            """,
            (meeting_id,),
        ).fetchall()
        if len(stored) > len(vote_history):
            raise PersistenceConflictError("vote history cannot be shortened")
        for index, row in enumerate(stored):
            vote = vote_history[index]
            if (
                row["agent_id"],
                row["choice"],
                row["revision"],
                row["explanation"],
            ) != (
                vote.agent_id,
                vote.choice.value,
                vote.revision,
                vote.explanation,
            ):
                raise PersistenceConflictError("vote history is append-only")

        for index, vote in enumerate(vote_history[len(stored) :], start=len(stored)):
            self._connection.execute(
                """
                INSERT INTO votes
                    (meeting_id, vote_order, agent_id, choice, revision, explanation)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    index,
                    vote.agent_id,
                    vote.choice.value,
                    vote.revision,
                    vote.explanation,
                ),
            )

    def _save_mission_items(
        self,
        mission_id: str,
        category: str,
        values: Sequence[str],
        *,
        append_only: bool,
    ) -> None:
        stored = self._connection.execute(
            """
            SELECT position, value FROM mission_items
            WHERE mission_id = ? AND category = ? ORDER BY position
            """,
            (mission_id, category),
        ).fetchall()
        if not append_only:
            self._connection.execute(
                "DELETE FROM mission_items WHERE mission_id = ? AND category = ?",
                (mission_id, category),
            )
            self._connection.executemany(
                """
                INSERT INTO mission_items (mission_id, category, position, value)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (mission_id, category, index, value)
                    for index, value in enumerate(values)
                ],
            )
            return

        stored_values = tuple(row["value"] for row in stored)
        if len(stored_values) > len(values) or stored_values != tuple(
            values[: len(stored_values)]
        ):
            raise PersistenceConflictError(
                f"mission {category} history cannot be shortened or overwritten"
            )
        self._connection.executemany(
            """
            INSERT INTO mission_items (mission_id, category, position, value)
            VALUES (?, ?, ?, ?)
            """,
            [
                (mission_id, category, index, value)
                for index, value in enumerate(
                    values[len(stored_values) :], start=len(stored_values)
                )
            ],
        )

    def _save_mission_events(
        self, mission_id: str, events: Sequence[MissionEvent]
    ) -> None:
        stored = self._connection.execute(
            """
            SELECT sequence, category, description, occurred_at
            FROM mission_events WHERE mission_id = ? ORDER BY sequence
            """,
            (mission_id,),
        ).fetchall()
        stored_events = tuple(
            (
                row["sequence"],
                row["category"],
                row["description"],
                row["occurred_at"],
            )
            for row in stored
        )
        expected_prefix = tuple(
            (
                event.sequence,
                event.category,
                event.description,
                event.occurred_at.isoformat(),
            )
            for event in events[: len(stored_events)]
        )
        if len(stored_events) > len(events) or stored_events != expected_prefix:
            raise PersistenceConflictError("mission event history is append-only")
        self._connection.executemany(
            """
            INSERT INTO mission_events
                (mission_id, sequence, category, description, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    mission_id,
                    event.sequence,
                    event.category,
                    event.description,
                    event.occurred_at.isoformat(),
                )
                for event in events[len(stored_events) :]
            ],
        )

    @staticmethod
    def _validate_mission_status_transition(
        current: MissionStatus, target: MissionStatus
    ) -> None:
        allowed_transitions = {
            MissionStatus.PLANNING: {
                MissionStatus.PLANNING,
                MissionStatus.ACTIVE,
                MissionStatus.ABORTED,
            },
            MissionStatus.ACTIVE: {
                MissionStatus.ACTIVE,
                MissionStatus.COMPLETED,
                MissionStatus.ABORTED,
            },
            MissionStatus.COMPLETED: {MissionStatus.COMPLETED},
            MissionStatus.ABORTED: {MissionStatus.ABORTED},
        }
        if target not in allowed_transitions[current]:
            raise PersistenceConflictError(
                f"mission status cannot move from {current.value} to {target.value}"
            )

    def _save_immutable_row(
        self,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[object, ...],
        value_columns: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        where_clause = " AND ".join(
            f"{column} = ?" for column in key_columns
        )
        existing = self._connection.execute(
            f"SELECT {', '.join(value_columns)} FROM {table} WHERE {where_clause}",
            key_values,
        ).fetchone()
        if existing is not None:
            if tuple(existing[column] for column in value_columns) != values:
                raise PersistenceConflictError(
                    f"persisted {table} history cannot be overwritten"
                )
            return

        columns = key_columns + value_columns
        placeholders = ", ".join("?" for _ in columns)
        self._connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            key_values + values,
        )

    def _initialize_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise PersistenceError(f"unsupported database schema version: {version}")
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    mission_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('PLANNING', 'ACTIVE', 'COMPLETED', 'ABORTED'))
                );
                CREATE TABLE IF NOT EXISTS mission_operators (
                    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    operator_id TEXT NOT NULL,
                    status TEXT,
                    assignment TEXT,
                    available INTEGER CHECK (available IN (0, 1) OR available IS NULL),
                    PRIMARY KEY (mission_id, operator_id),
                    UNIQUE (mission_id, position)
                );
                CREATE TABLE IF NOT EXISTS mission_items (
                    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
                    category TEXT NOT NULL CHECK (category IN (
                        'equipment', 'known_threat', 'unknown_information',
                        'observation', 'council_decision', 'commander_order'
                    )),
                    position INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (mission_id, category, position)
                );
                CREATE TABLE IF NOT EXISTS mission_events (
                    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (mission_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                    motion TEXT NOT NULL,
                    seats_json TEXT NOT NULL,
                    mission_snapshot_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'IDLE', 'MEETING_STARTED', 'ANALYSIS', 'DISCUSSION',
                        'INITIAL_VOTING', 'REVISION', 'FINALIZED',
                        'COMMANDER_DECISION'
                    )),
                    discussion_round INTEGER NOT NULL DEFAULT 0,
                    max_discussion_rounds INTEGER NOT NULL DEFAULT 3
                        CHECK (max_discussion_rounds BETWEEN 1 AND 8),
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS agent_analyses (
                    meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    assumptions_json TEXT NOT NULL,
                    main_risks TEXT NOT NULL,
                    missing_information TEXT NOT NULL,
                    suggested_improvements TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    supporting_evidence_json TEXT NOT NULL DEFAULT '[]',
                    main_concerns_json TEXT NOT NULL DEFAULT '[]',
                    alternative_recommendations_json TEXT NOT NULL DEFAULT '[]',
                    provider TEXT NOT NULL DEFAULT 'unknown',
                    model TEXT NOT NULL DEFAULT 'unknown',
                    response_type TEXT NOT NULL DEFAULT 'real_ai',
                    completed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (meeting_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS agent_discussions (
                    meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    challenges TEXT NOT NULL,
                    reconsideration TEXT NOT NULL,
                    recommendation_changed INTEGER NOT NULL
                        CHECK (recommendation_changed IN (0, 1)),
                    updated_recommendation TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    PRIMARY KEY (meeting_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS discussion_rounds (
                    meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    round_number INTEGER NOT NULL CHECK (round_number >= 1),
                    agent_id TEXT NOT NULL,
                    challenges TEXT NOT NULL,
                    reconsideration TEXT NOT NULL,
                    recommendation_changed INTEGER NOT NULL
                        CHECK (recommendation_changed IN (0, 1)),
                    updated_recommendation TEXT NOT NULL CHECK (
                        updated_recommendation IN ('YES', 'NO')
                    ),
                    reasoning TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    agreement TEXT NOT NULL,
                    disagreement TEXT NOT NULL,
                    weak_reasoning TEXT NOT NULL,
                    missing_information TEXT NOT NULL,
                    possible_risks TEXT NOT NULL,
                    unresolved_disagreements_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    response_type TEXT NOT NULL CHECK (
                        response_type IN ('real_ai', 'simulated')
                    ),
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (meeting_id, round_number, agent_id)
                );
                CREATE TABLE IF NOT EXISTS votes (
                    meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    vote_order INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    choice TEXT NOT NULL CHECK (choice IN ('YES', 'NO')),
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    explanation TEXT,
                    PRIMARY KEY (meeting_id, agent_id, revision),
                    UNIQUE (meeting_id, vote_order)
                );
                CREATE TABLE IF NOT EXISTS commander_decisions (
                    meeting_id TEXT PRIMARY KEY REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    option TEXT NOT NULL CHECK (option IN (
                        'AUTHORIZE', 'REJECT', 'REQUEST_ANOTHER_MEETING'
                    )),
                    explanation TEXT NOT NULL,
                    decided_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS provider_configs (
                    config_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                );
                """
            )
            self._ensure_column(
                "meetings", "discussion_round", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                "meetings",
                "max_discussion_rounds",
                "INTEGER NOT NULL DEFAULT 3",
            )
            self._ensure_column("meetings", "created_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                "commander_decisions", "decided_at", "TEXT NOT NULL DEFAULT ''"
            )
            for name, declaration in (
                ("supporting_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("main_concerns_json", "TEXT NOT NULL DEFAULT '[]'"),
                (
                    "alternative_recommendations_json",
                    "TEXT NOT NULL DEFAULT '[]'",
                ),
                ("provider", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("model", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("response_type", "TEXT NOT NULL DEFAULT 'real_ai'"),
                ("completed_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                self._ensure_column("agent_analyses", name, declaration)
            if version == 1:
                self._connection.execute(
                    """
                    UPDATE meetings SET max_discussion_rounds = 1
                    WHERE state IN (
                        'DISCUSSION', 'INITIAL_VOTING', 'REVISION',
                        'FINALIZED', 'COMMANDER_DECISION'
                    )
                    """
                )
            self._install_immutable_history_triggers()
            self._connection.execute("PRAGMA user_version = 2")

    def _install_immutable_history_triggers(self) -> None:
        for table in (
            "mission_events",
            "agent_analyses",
            "agent_discussions",
            "discussion_rounds",
            "votes",
            "commander_decisions",
        ):
            for operation in ("UPDATE", "DELETE"):
                name = f"immutable_{table}_{operation.lower()}"
                self._connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {name}
                    BEFORE {operation} ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, '{table} history is append-only');
                    END
                    """
                )
        self._connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS immutable_meeting_identity
            BEFORE UPDATE ON meetings
            WHEN NEW.meeting_id != OLD.meeting_id
              OR NEW.mission_id != OLD.mission_id
              OR NEW.motion != OLD.motion
              OR NEW.seats_json != OLD.seats_json
              OR NEW.mission_snapshot_json != OLD.mission_snapshot_json
              OR NEW.max_discussion_rounds != OLD.max_discussion_rounds
              OR NEW.created_at != OLD.created_at
              OR NEW.discussion_round < OLD.discussion_round
              OR NOT (
                    NEW.state = OLD.state
                    OR (OLD.state = 'MEETING_STARTED' AND NEW.state = 'ANALYSIS')
                    OR (OLD.state = 'ANALYSIS' AND NEW.state = 'DISCUSSION')
                    OR (OLD.state = 'DISCUSSION' AND NEW.state = 'INITIAL_VOTING')
                    OR (OLD.state = 'INITIAL_VOTING' AND NEW.state = 'REVISION')
                    OR (OLD.state = 'REVISION' AND NEW.state = 'FINALIZED')
                    OR (OLD.state = 'FINALIZED' AND NEW.state = 'COMMANDER_DECISION')
              )
            BEGIN
                SELECT RAISE(ABORT, 'meeting audit history is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS immutable_meeting_delete
            BEFORE DELETE ON meetings
            BEGIN
                SELECT RAISE(ABORT, 'meeting audit history is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS immutable_mission_item_update
            BEFORE UPDATE ON mission_items
            WHEN OLD.category != 'equipment'
            BEGIN
                SELECT RAISE(ABORT, 'mission item history is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS immutable_mission_item_delete
            BEFORE DELETE ON mission_items
            WHEN OLD.category != 'equipment'
            BEGIN
                SELECT RAISE(ABORT, 'mission item history is append-only');
            END;
            """
        )

    def _ensure_column(
        self, table: str, column: str, declaration: str
    ) -> None:
        existing = {
            row["name"]
            for row in self._connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column not in existing:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    @staticmethod
    def _mission_to_json(mission: MissionSnapshot) -> str:
        payload = {
            "mission_id": mission.mission_id,
            "name": mission.name,
            "objective": mission.objective,
            "status": mission.status.value,
            "operators": [
                {
                    "operator_id": operator.operator_id,
                    "status": operator.status,
                    "assignment": operator.assignment,
                    "available": operator.available,
                }
                for operator in mission.operators
            ],
            "available_equipment": mission.available_equipment,
            "known_threats": mission.known_threats,
            "unknown_information": mission.unknown_information,
            "observations": mission.observations,
            "council_decisions": mission.council_decisions,
            "commander_orders": mission.commander_orders,
            "events": [
                {
                    "sequence": event.sequence,
                    "category": event.category,
                    "description": event.description,
                    "occurred_at": event.occurred_at.isoformat(),
                }
                for event in mission.events
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _mission_from_json(cls, serialized: str) -> MissionSnapshot:
        try:
            payload = json.loads(serialized)
            return MissionSnapshot(
                mission_id=payload["mission_id"],
                name=payload["name"],
                objective=payload["objective"],
                status=MissionStatus(payload["status"]),
                operators=tuple(
                    Operator(
                        operator_id=item["operator_id"],
                        status=item["status"],
                        assignment=item["assignment"],
                        available=item["available"],
                    )
                    for item in payload["operators"]
                ),
                available_equipment=cls._json_text_tuple(
                    payload, "available_equipment"
                ),
                known_threats=cls._json_text_tuple(payload, "known_threats"),
                unknown_information=cls._json_text_tuple(
                    payload, "unknown_information"
                ),
                observations=cls._json_text_tuple(payload, "observations"),
                council_decisions=cls._json_text_tuple(
                    payload, "council_decisions"
                ),
                commander_orders=cls._json_text_tuple(
                    payload, "commander_orders"
                ),
                events=tuple(
                    MissionEvent(
                        sequence=item["sequence"],
                        category=item["category"],
                        description=item["description"],
                        occurred_at=datetime.fromisoformat(item["occurred_at"]),
                    )
                    for item in payload["events"]
                ),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StoredDataError("Stored mission snapshot is invalid") from exc

    @staticmethod
    def _json_text_tuple(payload: dict[str, object], field_name: str) -> tuple[str, ...]:
        values = payload[field_name]
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            raise StoredDataError(f"Stored mission field {field_name} is invalid")
        return tuple(values)

    @staticmethod
    def _make_result(
        motion: str,
        seats: tuple[str, ...],
        history: tuple[VoteRecord, ...],
        state: MeetingState,
    ) -> CouncilResult | None:
        if state not in (MeetingState.FINALIZED, MeetingState.COMMANDER_DECISION):
            return None
        latest_by_agent: dict[str, VoteRecord] = {}
        for vote in history:
            latest_by_agent[vote.agent_id] = vote
        votes = tuple(
            latest_by_agent[seat] for seat in seats if seat in latest_by_agent
        )
        missing_voters = tuple(seat for seat in seats if seat not in latest_by_agent)
        yes_votes = sum(vote.choice is VoteChoice.YES for vote in votes)
        no_votes = sum(vote.choice is VoteChoice.NO for vote in votes)
        required_yes_votes = len(seats) // 2 + 1
        return CouncilResult(
            motion=motion,
            approved=yes_votes >= required_yes_votes,
            incomplete=bool(missing_voters),
            yes_votes=yes_votes,
            no_votes=no_votes,
            required_yes_votes=required_yes_votes,
            missing_voters=missing_voters,
            votes=votes,
        )

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be empty")
        return value.strip()
