"""Validated, immutable snapshots for shared mission information."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class MissionStatus(str, Enum):
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class Operator:
    operator_id: str
    status: str | None = None
    assignment: str | None = None
    available: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operator_id, str) or not self.operator_id.strip():
            raise ValueError("operator_id must not be empty")
        object.__setattr__(self, "operator_id", self.operator_id.strip())
        if self.status is not None:
            if not isinstance(self.status, str) or not self.status.strip():
                raise ValueError("operator status must be non-empty when provided")
            object.__setattr__(self, "status", self.status.strip())
        if self.assignment is not None:
            if not isinstance(self.assignment, str) or not self.assignment.strip():
                raise ValueError(
                    "operator assignment must be non-empty when provided"
                )
            object.__setattr__(self, "assignment", self.assignment.strip())
        if self.available is not None and not isinstance(self.available, bool):
            raise ValueError("operator availability must be a boolean when provided")


@dataclass(frozen=True)
class MissionEvent:
    sequence: int
    category: str
    description: str
    occurred_at: datetime


@dataclass(frozen=True)
class MissionSnapshot:
    mission_id: str
    name: str
    objective: str
    status: MissionStatus
    operators: tuple[Operator, ...]
    available_equipment: tuple[str, ...]
    known_threats: tuple[str, ...]
    unknown_information: tuple[str, ...]
    observations: tuple[str, ...]
    council_decisions: tuple[str, ...]
    commander_orders: tuple[str, ...]
    events: tuple[MissionEvent, ...]


class MissionState:
    """Own mission updates and expose a consistent immutable snapshot."""

    def __init__(self, mission_id: str, name: str, objective: str) -> None:
        self._mission_id = self._required_text(mission_id, "mission_id")
        self._name = self._required_text(name, "name")
        self._objective = self._required_text(objective, "objective")
        self._status = MissionStatus.PLANNING
        self._operators: tuple[Operator, ...] = ()
        self._available_equipment: tuple[str, ...] = ()
        self._known_threats: tuple[str, ...] = ()
        self._unknown_information: tuple[str, ...] = ()
        self._observations: tuple[str, ...] = ()
        self._council_decisions: tuple[str, ...] = ()
        self._commander_orders: tuple[str, ...] = ()
        self._events: tuple[MissionEvent, ...] = ()
        self._append_event("mission.created", f"Mission {self._mission_id} created")

    @classmethod
    def from_snapshot(cls, snapshot: MissionSnapshot) -> "MissionState":
        if not isinstance(snapshot, MissionSnapshot):
            raise ValueError("snapshot must be a MissionSnapshot")
        if not isinstance(snapshot.status, MissionStatus):
            raise ValueError("snapshot has an invalid mission status")
        mission = cls(snapshot.mission_id, snapshot.name, snapshot.objective)
        mission._status = snapshot.status
        mission._operators = tuple(snapshot.operators)
        mission._available_equipment = tuple(snapshot.available_equipment)
        mission._known_threats = tuple(snapshot.known_threats)
        mission._unknown_information = tuple(snapshot.unknown_information)
        mission._observations = tuple(snapshot.observations)
        mission._council_decisions = tuple(snapshot.council_decisions)
        mission._commander_orders = tuple(snapshot.commander_orders)
        mission._events = tuple(snapshot.events)
        return mission

    @property
    def snapshot(self) -> MissionSnapshot:
        return MissionSnapshot(
            mission_id=self._mission_id,
            name=self._name,
            objective=self._objective,
            status=self._status,
            operators=self._operators,
            available_equipment=self._available_equipment,
            known_threats=self._known_threats,
            unknown_information=self._unknown_information,
            observations=self._observations,
            council_decisions=self._council_decisions,
            commander_orders=self._commander_orders,
            events=self._events,
        )

    def update_objective(self, objective: str) -> None:
        normalized = self._required_text(objective, "objective")
        self._objective = normalized
        self._append_event("mission.objective_updated", "Mission objective updated")

    def set_status(self, status: MissionStatus) -> None:
        if not isinstance(status, MissionStatus):
            raise ValueError("status must be a MissionStatus")
        allowed_transitions = {
            MissionStatus.PLANNING: {MissionStatus.ACTIVE, MissionStatus.ABORTED},
            MissionStatus.ACTIVE: {MissionStatus.COMPLETED, MissionStatus.ABORTED},
            MissionStatus.COMPLETED: set(),
            MissionStatus.ABORTED: set(),
        }
        if status not in allowed_transitions[self._status]:
            raise ValueError(
                f"cannot change mission status from {self._status.value} "
                f"to {status.value}"
            )
        self._status = status
        self._append_event(
            "mission.status_changed", f"Mission status set to {status.value}"
        )

    def set_operators(self, operators: tuple[Operator, ...]) -> None:
        normalized = tuple(operators)
        if any(not isinstance(operator, Operator) for operator in normalized):
            raise ValueError("operators must contain Operator values")
        operator_ids = [operator.operator_id for operator in normalized]
        if len(set(operator_ids)) != len(operator_ids):
            raise ValueError("operator identifiers must be unique")
        self._operators = normalized
        self._append_event("mission.operators_updated", "Operator roster updated")

    def set_available_equipment(self, equipment: tuple[str, ...]) -> None:
        normalized = self._unique_text_items(equipment, "equipment")
        self._available_equipment = normalized
        self._append_event("mission.equipment_updated", "Available equipment updated")

    def add_known_threat(self, description: str) -> None:
        updated = self._append_unique(self._known_threats, description, "threat")
        if updated == self._known_threats:
            return
        self._known_threats = updated
        self._append_event("mission.threat_recorded", "Known threat recorded")

    def add_unknown_information(self, description: str) -> None:
        updated = self._append_unique(
            self._unknown_information, description, "unknown information"
        )
        if updated == self._unknown_information:
            return
        self._unknown_information = updated
        self._append_event(
            "mission.unknown_information_recorded",
            "Unknown information recorded",
        )

    def add_observation(self, description: str) -> None:
        updated = self._append_unique(
            self._observations, description, "observation"
        )
        if updated == self._observations:
            return
        self._observations = updated
        self._append_event("mission.observation_recorded", "Observation recorded")

    def record_council_decision(self, decision: str) -> None:
        self._council_decisions += (self._required_text(decision, "decision"),)
        self._append_event("mission.council_decision", "Council decision recorded")

    def record_commander_order(self, order: str) -> None:
        self._commander_orders += (self._required_text(order, "order"),)
        self._append_event("mission.commander_order", "Commander order recorded")

    def _append_event(self, category: str, description: str) -> None:
        event = MissionEvent(
            sequence=len(self._events) + 1,
            category=category,
            description=description,
            occurred_at=datetime.now(timezone.utc),
        )
        self._events += (event,)

    @classmethod
    def _append_unique(
        cls, current: tuple[str, ...], item: str, field_name: str
    ) -> tuple[str, ...]:
        normalized = cls._required_text(item, field_name)
        return current if normalized in current else current + (normalized,)

    @classmethod
    def _unique_text_items(
        cls, items: tuple[str, ...], field_name: str
    ) -> tuple[str, ...]:
        normalized = tuple(cls._required_text(item, field_name) for item in items)
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{field_name} entries must be unique")
        return normalized

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be empty")
        return value.strip()
