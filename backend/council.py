"""Provider-independent council workflow and voting rules."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from backend.mission_state import MissionSnapshot


DEFAULT_SEATS = (
    "squad_leader",
    "tactical_advisor",
    "intelligence_officer",
)


class MeetingState(str, Enum):
    IDLE = "IDLE"
    MEETING_STARTED = "MEETING_STARTED"
    ANALYSIS = "ANALYSIS"
    DISCUSSION = "DISCUSSION"
    INITIAL_VOTING = "INITIAL_VOTING"
    REVISION = "REVISION"
    FINALIZED = "FINALIZED"
    COMMANDER_DECISION = "COMMANDER_DECISION"


class VoteChoice(str, Enum):
    YES = "YES"
    NO = "NO"


class CommanderOption(str, Enum):
    AUTHORIZE = "AUTHORIZE"
    REJECT = "REJECT"
    REQUEST_ANOTHER_MEETING = "REQUEST_ANOTHER_MEETING"


@dataclass(frozen=True)
class AgentAnalysis:
    agent_id: str
    recommendation: str
    reasoning: str
    facts: tuple[str, ...]
    assumptions: tuple[str, ...]
    main_risks: str
    missing_information: str
    suggested_improvements: str
    confidence: float
    supporting_evidence: tuple[str, ...] = ()
    main_concerns: tuple[str, ...] = ()
    alternative_recommendations: tuple[str, ...] = ()
    provider: str = "unknown"
    model: str = "unknown"
    response_type: str = "real_ai"
    completed_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.response_type == "legacy":
            if not isinstance(self.recommendation, str) or not self.recommendation.strip():
                raise ValueError("legacy recommendation must not be empty")
        elif self.recommendation not in ("YES", "NO"):
            raise ValueError("recommendation must be YES or NO")
        if not self.reasoning.strip():
            raise ValueError("reasoning must not be empty")
        for field_name in (
            "facts",
            "assumptions",
            "supporting_evidence",
            "main_concerns",
            "alternative_recommendations",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"{field_name} must contain non-empty strings")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be between 0 and 1")
        _validate_provenance(self.provider, self.model, self.response_type)


@dataclass(frozen=True)
class VoteRecord:
    agent_id: str
    choice: VoteChoice
    revision: int
    explanation: str | None = None


@dataclass(frozen=True)
class AgentDiscussion:
    agent_id: str
    challenges: str
    reconsideration: str
    recommendation_changed: bool
    updated_recommendation: str
    reasoning: str
    confidence: float
    round_number: int = 1
    agreement: str = "Not stated"
    disagreement: str = "Not stated"
    weak_reasoning: str = "Not stated"
    missing_information: str = "Not stated"
    possible_risks: str = "Not stated"
    unresolved_disagreements: tuple[str, ...] = ()
    provider: str = "unknown"
    model: str = "unknown"
    response_type: str = "real_ai"
    completed_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        for field_name in (
            "challenges",
            "reconsideration",
            "updated_recommendation",
            "reasoning",
            "agreement",
            "disagreement",
            "weak_reasoning",
            "missing_information",
            "possible_risks",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if not isinstance(self.recommendation_changed, bool):
            raise ValueError("recommendation_changed must be a boolean")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be between 0 and 1")
        if (
            isinstance(self.round_number, bool)
            or not isinstance(self.round_number, int)
            or self.round_number < 1
        ):
            raise ValueError("round_number must be a positive integer")
        if not isinstance(self.unresolved_disagreements, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.unresolved_disagreements
        ):
            raise ValueError("unresolved_disagreements must contain non-empty strings")
        _validate_provenance(self.provider, self.model, self.response_type)
        if self.response_type == "legacy":
            if not self.updated_recommendation.strip():
                raise ValueError("legacy recommendation must not be empty")
        elif self.updated_recommendation not in ("YES", "NO"):
            raise ValueError("updated_recommendation must be YES or NO")


def _validate_provenance(provider: str, model: str, response_type: str) -> None:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider provenance must not be empty")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model provenance must not be empty")
    if response_type not in ("real_ai", "simulated", "legacy"):
        raise ValueError("response_type must be real_ai, simulated, or legacy")


@dataclass(frozen=True)
class CouncilResult:
    motion: str
    approved: bool
    incomplete: bool
    yes_votes: int
    no_votes: int
    required_yes_votes: int
    missing_voters: tuple[str, ...]
    votes: tuple[VoteRecord, ...]


@dataclass(frozen=True)
class CommanderDecision:
    option: CommanderOption
    explanation: str
    decided_at: str = ""


class CouncilMeeting:
    """Track one canonical motion through analysis, voting, and command."""

    def __init__(
        self,
        motion: str,
        seats: tuple[str, ...] = DEFAULT_SEATS,
        *,
        mission_snapshot: MissionSnapshot | None = None,
        max_discussion_rounds: int = 3,
    ) -> None:
        normalized_motion = motion.strip()
        if not normalized_motion:
            raise ValueError("motion must not be empty")
        if not seats or any(not seat.strip() for seat in seats):
            raise ValueError("seats must contain non-empty identifiers")
        if len(set(seats)) != len(seats):
            raise ValueError("seat identifiers must be unique")
        if (
            isinstance(max_discussion_rounds, bool)
            or not isinstance(max_discussion_rounds, int)
            or not 1 <= max_discussion_rounds <= 8
        ):
            raise ValueError("max_discussion_rounds must be between 1 and 8")

        self._motion = normalized_motion
        self._seats = tuple(seats)
        if mission_snapshot is not None and not isinstance(
            mission_snapshot, MissionSnapshot
        ):
            raise ValueError("mission_snapshot must be a MissionSnapshot")
        self._mission_snapshot = mission_snapshot
        self._state = MeetingState.IDLE
        self._max_discussion_rounds = max_discussion_rounds
        self._discussion_round = 0
        self._analyses: dict[str, AgentAnalysis] = {}
        self._discussions: dict[tuple[int, str], AgentDiscussion] = {}
        self._vote_history: list[VoteRecord] = []
        self._commander_decision: CommanderDecision | None = None

    @classmethod
    def restore(
        cls,
        *,
        motion: str,
        seats: tuple[str, ...],
        mission_snapshot: MissionSnapshot,
        state: MeetingState,
        analyses: tuple[AgentAnalysis, ...],
        discussions: tuple[AgentDiscussion, ...],
        vote_history: tuple[VoteRecord, ...],
        commander_decision: CommanderDecision | None,
        discussion_round: int | None = None,
        max_discussion_rounds: int = 3,
    ) -> "CouncilMeeting":
        if not isinstance(state, MeetingState):
            raise ValueError("state must be a MeetingState")
        meeting = cls(
            motion,
            seats,
            mission_snapshot=mission_snapshot,
            max_discussion_rounds=max_discussion_rounds,
        )
        states = tuple(MeetingState)
        target_index = states.index(state)

        if target_index >= states.index(MeetingState.MEETING_STARTED):
            meeting.start()
        if target_index >= states.index(MeetingState.ANALYSIS):
            meeting.start_analysis()
            for analysis in analyses:
                meeting.submit_analysis(analysis)
        elif analyses:
            raise ValueError("analyses cannot exist before the analysis phase")
        if target_index >= states.index(MeetingState.DISCUSSION):
            meeting.start_discussion()
            for discussion in sorted(
                discussions, key=lambda item: (item.round_number, item.agent_id)
            ):
                while meeting.discussion_round < discussion.round_number:
                    meeting.start_next_discussion_round()
                meeting.submit_discussion(discussion)
        elif discussions:
            raise ValueError("discussions cannot exist before the discussion phase")
        if target_index >= states.index(MeetingState.DISCUSSION):
            requested_round = discussion_round or (
                max((item.round_number for item in discussions), default=1)
            )
            while meeting.discussion_round < requested_round:
                meeting.start_next_discussion_round()
        if target_index >= states.index(MeetingState.INITIAL_VOTING):
            meeting.start_initial_voting()
            for vote in vote_history:
                if vote.revision == 0:
                    meeting.submit_initial_vote(vote.agent_id, vote.choice)
                elif state is MeetingState.INITIAL_VOTING:
                    raise ValueError("revised votes cannot exist before revision")
        elif vote_history:
            raise ValueError("votes cannot exist before the voting phase")
        if target_index >= states.index(MeetingState.REVISION):
            meeting.start_revision()
            for vote in vote_history:
                if vote.revision > 0:
                    meeting.revise_vote(
                        vote.agent_id,
                        vote.choice,
                        vote.explanation or "",
                    )
                    if meeting.vote_history[-1] != vote:
                        raise ValueError("stored vote revision history is invalid")
        if target_index >= states.index(MeetingState.FINALIZED):
            meeting.finalize()

        if state is MeetingState.COMMANDER_DECISION:
            if commander_decision is None:
                raise ValueError("commander decision is missing")
            meeting.decide(
                commander_decision.option,
                commander_decision.explanation,
                commander_decision.decided_at,
            )
        elif commander_decision is not None:
            raise ValueError("commander decision exists before the final phase")
        return meeting

    @property
    def motion(self) -> str:
        return self._motion

    @property
    def seats(self) -> tuple[str, ...]:
        return self._seats

    @property
    def mission_snapshot(self) -> MissionSnapshot | None:
        return self._mission_snapshot

    @property
    def state(self) -> MeetingState:
        return self._state

    @property
    def analyses(self) -> Mapping[str, AgentAnalysis]:
        return MappingProxyType(self._analyses)

    @property
    def discussions(self) -> Mapping[tuple[int, str], AgentDiscussion]:
        return MappingProxyType(self._discussions)

    @property
    def discussion_round(self) -> int:
        return self._discussion_round

    @property
    def max_discussion_rounds(self) -> int:
        return self._max_discussion_rounds

    @property
    def unresolved_disagreements(self) -> tuple[str, ...]:
        recommendations: dict[str, str] = {}
        for agent_id, analysis in self._analyses.items():
            discussions = [
                discussion
                for (round_number, seat), discussion in self._discussions.items()
                if seat == agent_id
            ]
            latest = max(discussions, key=lambda item: item.round_number, default=None)
            recommendations[agent_id] = (
                latest.updated_recommendation if latest else analysis.recommendation
            )
        values = {
            discussion
            for (round_number, _), response in self._discussions.items()
            if round_number == self._discussion_round
            for discussion in response.unresolved_disagreements
        }
        yes = tuple(
            agent_id for agent_id, choice in recommendations.items() if choice == "YES"
        )
        no = tuple(
            agent_id for agent_id, choice in recommendations.items() if choice == "NO"
        )
        if yes and no:
            values.add(
                "Final recommendations remain split: YES from "
                + ", ".join(yes)
                + "; NO from "
                + ", ".join(no)
            )
        return tuple(sorted(values))

    @property
    def commander_decision(self) -> CommanderDecision | None:
        return self._commander_decision

    @property
    def vote_history(self) -> tuple[VoteRecord, ...]:
        """Return the append-only vote history."""
        return tuple(self._vote_history)

    @property
    def current_votes(self) -> tuple[VoteRecord, ...]:
        """Return each voting seat's latest recorded vote in seat order."""
        latest_by_agent: dict[str, VoteRecord] = {}
        for record in self._vote_history:
            latest_by_agent[record.agent_id] = record
        return tuple(
            latest_by_agent[seat] for seat in self.seats if seat in latest_by_agent
        )

    def start(self) -> None:
        self._transition(MeetingState.IDLE, MeetingState.MEETING_STARTED)

    def start_analysis(self) -> None:
        self._require_state(MeetingState.MEETING_STARTED)
        if self.mission_snapshot is None:
            raise ValueError("mission snapshot must be set before analysis")
        self._state = MeetingState.ANALYSIS

    def submit_analysis(self, analysis: AgentAnalysis) -> None:
        self._require_state(MeetingState.ANALYSIS)
        self._require_seat(analysis.agent_id)
        if analysis.agent_id in self.analyses:
            raise ValueError(f"analysis already submitted by {analysis.agent_id}")
        self._analyses[analysis.agent_id] = analysis

    def start_discussion(self) -> None:
        self._require_state(MeetingState.ANALYSIS)
        if len(self._analyses) < 2:
            raise RuntimeError(
                "at least two actual analyses are required for peer discussion"
            )
        self._state = MeetingState.DISCUSSION
        self._discussion_round = 1

    def submit_discussion(self, discussion: AgentDiscussion) -> None:
        self._require_state(MeetingState.DISCUSSION)
        self._require_seat(discussion.agent_id)
        if discussion.agent_id not in self.analyses:
            raise ValueError("an agent must submit analysis before discussion")
        if not any(
            agent_id != discussion.agent_id for agent_id in self.analyses
        ):
            raise ValueError("discussion requires another agent's analysis")
        if discussion.round_number != self.discussion_round:
            raise ValueError("discussion response round does not match the current round")
        key = (discussion.round_number, discussion.agent_id)
        if key in self.discussions:
            raise ValueError(
                f"discussion already submitted by {discussion.agent_id} "
                f"for round {discussion.round_number}"
            )
        self._discussions[key] = discussion

    def start_next_discussion_round(self) -> None:
        self._require_state(MeetingState.DISCUSSION)
        if self.discussion_round >= self.max_discussion_rounds:
            raise RuntimeError("maximum discussion rounds reached")
        if not self._discussion_round_complete(self.discussion_round):
            raise RuntimeError("current discussion round is incomplete")
        self._discussion_round += 1

    def start_initial_voting(self) -> None:
        self._require_state(MeetingState.DISCUSSION)
        if any(
            analysis.response_type == "legacy"
            for analysis in self._analyses.values()
        ) or any(
            response.response_type == "legacy"
            for response in self._discussions.values()
        ):
            raise RuntimeError(
                "legacy responses lack verified recommendation/provenance fields"
            )
        if not self._discussion_round_complete(self.discussion_round):
            raise RuntimeError("current discussion round is incomplete")
        if self.discussion_round < self.max_discussion_rounds:
            raise RuntimeError("maximum discussion rounds have not been completed")
        self._state = MeetingState.INITIAL_VOTING

    def _discussion_round_complete(self, round_number: int) -> bool:
        return bool(self.analyses) and all(
            (round_number, agent_id) in self._discussions
            for agent_id in self.analyses
        )

    def submit_initial_vote(self, agent_id: str, choice: VoteChoice) -> None:
        self._require_state(MeetingState.INITIAL_VOTING)
        self._require_seat(agent_id)
        if any(record.agent_id == agent_id for record in self._vote_history):
            raise ValueError(f"initial vote already submitted by {agent_id}")
        self._vote_history.append(
            VoteRecord(agent_id, self._validate_choice(choice), revision=0)
        )

    def start_revision(self) -> None:
        self._transition(MeetingState.INITIAL_VOTING, MeetingState.REVISION)

    def revise_vote(
        self,
        agent_id: str,
        choice: VoteChoice,
        explanation: str,
    ) -> None:
        self._require_state(MeetingState.REVISION)
        self._require_seat(agent_id)
        normalized_explanation = explanation.strip()
        if not normalized_explanation:
            raise ValueError("a vote revision requires an explanation")

        prior_votes = [
            record for record in self._vote_history if record.agent_id == agent_id
        ]
        if not prior_votes:
            raise ValueError(f"{agent_id} has no initial vote to revise")

        validated_choice = self._validate_choice(choice)
        if validated_choice == prior_votes[-1].choice:
            raise ValueError("a revised vote must change the previous choice")
        self._vote_history.append(
            VoteRecord(
                agent_id,
                validated_choice,
                revision=len(prior_votes),
                explanation=normalized_explanation,
            )
        )

    def finalize(self) -> CouncilResult:
        self._transition(MeetingState.REVISION, MeetingState.FINALIZED)
        return self.result

    @property
    def result(self) -> CouncilResult:
        if self.state not in (
            MeetingState.FINALIZED,
            MeetingState.COMMANDER_DECISION,
        ):
            raise RuntimeError("council result is not finalized")

        votes = self.current_votes
        yes_votes = sum(vote.choice is VoteChoice.YES for vote in votes)
        no_votes = sum(vote.choice is VoteChoice.NO for vote in votes)
        voted_agents = {vote.agent_id for vote in votes}
        missing_voters = tuple(seat for seat in self.seats if seat not in voted_agents)
        required_yes_votes = len(self.seats) // 2 + 1
        return CouncilResult(
            motion=self.motion,
            approved=yes_votes >= required_yes_votes,
            incomplete=bool(missing_voters),
            yes_votes=yes_votes,
            no_votes=no_votes,
            required_yes_votes=required_yes_votes,
            missing_voters=missing_voters,
            votes=votes,
        )

    def decide(
        self,
        option: CommanderOption,
        explanation: str = "",
        decided_at: str | None = None,
    ) -> None:
        self._require_state(MeetingState.FINALIZED)
        validated_option = self._validate_commander_option(option)
        normalized_explanation = explanation.strip()
        if (
            validated_option is CommanderOption.AUTHORIZE
            and not self.result.approved
        ):
            raise ValueError("the commander cannot authorize an unapproved motion")
        if (
            validated_option is CommanderOption.REQUEST_ANOTHER_MEETING
            and not normalized_explanation
        ):
            raise ValueError("requesting another meeting requires an explanation")

        self._commander_decision = CommanderDecision(
            validated_option,
            normalized_explanation,
            (
                decided_at
                if decided_at is not None
                else datetime.now(timezone.utc).isoformat()
            ),
        )
        self._state = MeetingState.COMMANDER_DECISION

    def _transition(self, expected: MeetingState, target: MeetingState) -> None:
        self._require_state(expected)
        self._state = target

    def _require_state(self, expected: MeetingState) -> None:
        if self.state is not expected:
            raise RuntimeError(
                f"operation requires {expected.value}; current state is {self.state.value}"
            )

    def _require_seat(self, agent_id: str) -> None:
        if agent_id not in self.seats:
            raise ValueError(f"unknown voting seat: {agent_id}")

    @staticmethod
    def _validate_choice(choice: VoteChoice) -> VoteChoice:
        if not isinstance(choice, VoteChoice):
            raise ValueError("vote choice must be YES or NO")
        return choice

    @staticmethod
    def _validate_commander_option(option: CommanderOption) -> CommanderOption:
        if not isinstance(option, CommanderOption):
            raise ValueError("invalid commander decision")
        return option
