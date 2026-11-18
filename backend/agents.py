"""Provider-backed council agents that analyze and discuss one mission."""

import json
from typing import Mapping

from backend.council import (
    AgentAnalysis,
    AgentDiscussion,
    CouncilMeeting,
    MeetingState,
)
from backend.providers import (
    AIProvider,
    InvalidProviderResponseError,
    ProviderStatus,
    StructuredAIProvider,
)


class CouncilAgent:
    """Use a configured AI provider for one voting seat and role."""

    def __init__(
        self,
        seat_id: str,
        name: str,
        role: str,
        responsibilities: tuple[str, ...],
        provider: AIProvider,
    ) -> None:
        self.seat_id = self._required_text(seat_id, "seat_id")
        self.name = self._required_text(name, "name")
        self.role = self._required_text(role, "role")
        self.responsibilities = tuple(
            self._required_text(item, "responsibility")
            for item in responsibilities
        )
        if not self.responsibilities:
            raise ValueError("responsibilities must not be empty")
        self.provider = provider

    @property
    def system_prompt(self) -> str:
        responsibilities = "; ".join(self.responsibilities)
        return (
            f"You are {self.name}, serving as {self.role}. "
            f"Your responsibilities are: {responsibilities}. "
            "Use only the supplied mission information. Clearly distinguish facts, "
            "assumptions, and unknowns. Do not invent mission state. In reasoning, "
            "give a brief high-level rationale, not private chain-of-thought. Treat "
            "the response schema as "
            "mandatory: include every field with the requested type, use an empty "
            "array when no facts or assumptions are supplied, and state when "
            "information is unavailable instead of omitting a field. Return only "
            "one JSON object with no markdown."
        )

    @property
    def connection_status(self) -> ProviderStatus:
        return self.provider.get_status()

    def analyze(self, meeting: CouncilMeeting) -> AgentAnalysis:
        if meeting.state is not MeetingState.ANALYSIS:
            raise RuntimeError("agent analysis requires the ANALYSIS phase")
        if self.seat_id not in meeting.seats:
            raise ValueError(f"agent seat is not part of this meeting: {self.seat_id}")
        if self.seat_id in meeting.analyses:
            raise ValueError(f"analysis already submitted by {self.seat_id}")
        mission = meeting.mission_snapshot
        if mission is None:
            raise ValueError("meeting has no mission snapshot")

        payload = {
            "motion": meeting.motion,
            "mission": self._mission_payload(mission),
            "response_schema": {
                "recommendation": "non-empty string",
                "reasoning": "non-empty string",
                "facts": "array of strings; empty if none are supplied",
                "assumptions": "array of strings; empty if none are made",
                "main_risks": "non-empty string",
                "missing_information": "non-empty string; state unknowns",
                "suggested_improvements": "non-empty string",
                "confidence": "number from 0 to 1",
            },
        }
        response = self._generate_response(
            self.system_prompt,
            json.dumps(payload, ensure_ascii=False),
            {
                "type": "object",
                "properties": {
                    "recommendation": {"type": "string", "minLength": 1},
                    "reasoning": {"type": "string", "minLength": 1},
                    "facts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "main_risks": {"type": "string", "minLength": 1},
                    "missing_information": {"type": "string", "minLength": 1},
                    "suggested_improvements": {"type": "string", "minLength": 1},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": [
                    "recommendation",
                    "reasoning",
                    "facts",
                    "assumptions",
                    "main_risks",
                    "missing_information",
                    "suggested_improvements",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        )
        analysis = self._parse_analysis(response)
        meeting.submit_analysis(analysis)
        return analysis

    def discuss(self, meeting: CouncilMeeting) -> AgentDiscussion:
        if meeting.state is not MeetingState.DISCUSSION:
            raise RuntimeError("agent discussion requires the DISCUSSION phase")
        mission = meeting.mission_snapshot
        if mission is None:
            raise ValueError("meeting has no mission snapshot")
        own_analysis = meeting.analyses.get(self.seat_id)
        if own_analysis is None:
            raise ValueError("agent must submit analysis before discussion")
        peer_analyses = {
            agent_id: self._analysis_payload(analysis)
            for agent_id, analysis in meeting.analyses.items()
            if agent_id != self.seat_id
        }
        if not peer_analyses:
            raise ValueError("discussion requires another agent's analysis")

        payload = {
            "motion": meeting.motion,
            "mission": self._mission_payload(mission),
            "your_initial_analysis": self._analysis_payload(own_analysis),
            "other_agents_analyses": peer_analyses,
            "response_schema": {
                "challenges": "non-empty string",
                "reconsideration": "non-empty string",
                "recommendation_changed": "boolean",
                "updated_recommendation": "non-empty string",
                "reasoning": "non-empty string",
                "confidence": "number from 0 to 1",
            },
        }
        discussion_system_prompt = (
            f"{self.system_prompt} Consider the other agents' actual analyses. "
            "Identify substantive challenges and missing information, then state "
            "whether you changed your recommendation and why."
        )
        response = self._generate_response(
            discussion_system_prompt,
            json.dumps(payload, ensure_ascii=False),
            {
                "type": "object",
                "properties": {
                    "challenges": {"type": "string", "minLength": 1},
                    "reconsideration": {"type": "string", "minLength": 1},
                    "recommendation_changed": {"type": "boolean"},
                    "updated_recommendation": {"type": "string", "minLength": 1},
                    "reasoning": {"type": "string", "minLength": 1},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": [
                    "challenges",
                    "reconsideration",
                    "recommendation_changed",
                    "updated_recommendation",
                    "reasoning",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        )
        discussion = self._parse_discussion(response)
        meeting.submit_discussion(discussion)
        return discussion

    def _generate_response(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, object],
    ) -> str:
        if isinstance(self.provider, StructuredAIProvider):
            return self.provider.generate_structured(
                system_prompt,
                user_prompt,
                response_schema,
            )
        return self.provider.generate(system_prompt, user_prompt)

    def _parse_analysis(self, response: str) -> AgentAnalysis:
        payload = self._parse_json_object(response, "analysis")
        return AgentAnalysis(
            agent_id=self.seat_id,
            recommendation=self._response_text(payload, "recommendation"),
            reasoning=self._response_text(payload, "reasoning"),
            facts=self._response_string_list(payload, "facts"),
            assumptions=self._response_string_list(payload, "assumptions"),
            main_risks=self._response_text(payload, "main_risks"),
            missing_information=self._response_text(
                payload, "missing_information"
            ),
            suggested_improvements=self._response_text(
                payload, "suggested_improvements"
            ),
            confidence=self._response_confidence(payload),
        )

    def _parse_discussion(self, response: str) -> AgentDiscussion:
        payload = self._parse_json_object(response, "discussion")
        recommendation_changed = payload.get("recommendation_changed")
        if not isinstance(recommendation_changed, bool):
            raise InvalidProviderResponseError(
                "Agent discussion field 'recommendation_changed' must be a boolean"
            )
        return AgentDiscussion(
            agent_id=self.seat_id,
            challenges=self._response_text(payload, "challenges"),
            reconsideration=self._response_text(payload, "reconsideration"),
            recommendation_changed=recommendation_changed,
            updated_recommendation=self._response_text(
                payload, "updated_recommendation"
            ),
            reasoning=self._response_text(payload, "reasoning"),
            confidence=self._response_confidence(payload),
        )

    @staticmethod
    def _parse_json_object(response: str, response_type: str) -> dict[str, object]:
        if not isinstance(response, str):
            raise InvalidProviderResponseError(
                f"Agent {response_type} must be a JSON string"
            )
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise InvalidProviderResponseError(
                f"Agent returned invalid JSON for {response_type}"
            ) from exc
        if not isinstance(payload, dict):
            raise InvalidProviderResponseError(
                f"Agent {response_type} must be a JSON object"
            )
        return payload

    @staticmethod
    def _response_text(payload: Mapping[str, object], field_name: str) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise InvalidProviderResponseError(
                f"Agent response field '{field_name}' must be non-empty text"
            )
        return value.strip()

    @staticmethod
    def _response_string_list(
        payload: Mapping[str, object], field_name: str
    ) -> tuple[str, ...]:
        value = payload.get(field_name)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise InvalidProviderResponseError(
                f"Agent response field '{field_name}' must be an array of strings"
            )
        return tuple(item.strip() for item in value)

    @staticmethod
    def _response_confidence(payload: Mapping[str, object]) -> float:
        value = payload.get("confidence")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= value <= 1.0
        ):
            raise InvalidProviderResponseError(
                "Agent response field 'confidence' must be between 0 and 1"
            )
        return float(value)

    @staticmethod
    def _analysis_payload(analysis: AgentAnalysis) -> dict[str, object]:
        return {
            "recommendation": analysis.recommendation,
            "reasoning": analysis.reasoning,
            "facts": analysis.facts,
            "assumptions": analysis.assumptions,
            "main_risks": analysis.main_risks,
            "missing_information": analysis.missing_information,
            "suggested_improvements": analysis.suggested_improvements,
            "confidence": analysis.confidence,
        }

    @staticmethod
    def _mission_payload(mission: MissionSnapshot) -> dict[str, object]:
        return {
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
        }

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be empty")
        return value.strip()
