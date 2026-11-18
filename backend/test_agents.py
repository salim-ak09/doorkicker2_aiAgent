import json
import unittest

from backend.agents import CouncilAgent
from backend.council import AgentAnalysis, CouncilMeeting
from backend.mission_state import MissionState
from backend.providers import (
    InvalidProviderResponseError,
    ProviderStatus,
)


class FakeProvider:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def get_status(self) -> ProviderStatus:
        return ProviderStatus("test-provider", True, "test-model", True, "ready")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.responses.pop(0)


def analysis_response(recommendation: str = "Proceed cautiously") -> str:
    return json.dumps(
        {
            "recommendation": recommendation,
            "reasoning": "This follows from the supplied mission facts.",
            "facts": ["The objective appears in the supplied mission snapshot."],
            "assumptions": ["The mission snapshot is current."],
            "main_risks": "The route may be obstructed.",
            "missing_information": "Current visibility is unknown.",
            "suggested_improvements": "Request an updated observation.",
            "confidence": 0.65,
        }
    )


def discussion_response() -> str:
    return json.dumps(
        {
            "challenges": "The advisor notes that visibility is unconfirmed.",
            "reconsideration": "That uncertainty warrants additional caution.",
            "recommendation_changed": True,
            "updated_recommendation": "Wait for a current observation.",
            "reasoning": "The peer analysis identifies a material unknown.",
            "confidence": 0.8,
        }
    )


class CouncilAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mission = MissionState(
            "mission-1", "Training", "Reach the rally point"
        )
        self.provider = FakeProvider(analysis_response())
        self.agent = CouncilAgent(
            seat_id="squad_leader",
            name="Squad Leader",
            role="Coordinate planning",
            responsibilities=("Summarize options", "Identify disagreements"),
            provider=self.provider,
        )
        self.meeting = CouncilMeeting(
            "Move to the rally point", mission_snapshot=self.mission.snapshot
        )
        self.meeting.start()
        self.meeting.start_analysis()

    def test_analysis_uses_provider_and_records_structured_output(self) -> None:
        self.mission.update_objective("Updated after meeting creation")
        analysis = self.agent.analyze(self.meeting)

        self.assertEqual(analysis.recommendation, "Proceed cautiously")
        self.assertEqual(self.meeting.analyses["squad_leader"], analysis)
        self.assertEqual(len(self.provider.calls), 1)
        system_prompt, user_prompt = self.provider.calls[0]
        self.assertIn("Summarize options", system_prompt)
        request = json.loads(user_prompt)
        self.assertEqual(request["motion"], self.meeting.motion)
        self.assertEqual(request["mission"]["mission_id"], "mission-1")
        self.assertEqual(request["mission"]["objective"], "Reach the rally point")
        self.assertIn("facts", request["response_schema"])

    def test_discussion_receives_peer_analyses_and_records_reconsideration(self) -> None:
        self.provider.responses.append(discussion_response())
        self.agent.analyze(self.meeting)
        peer_analysis = AgentAnalysis(
            agent_id="tactical_advisor",
            recommendation="Wait for updated visibility",
            reasoning="Visibility has not been confirmed.",
            facts=("Visibility is not stated in the snapshot.",),
            assumptions=(),
            main_risks="Route conditions may be different.",
            missing_information="Current visibility.",
            suggested_improvements="Request an observation.",
            confidence=0.8,
        )
        self.meeting.submit_analysis(peer_analysis)
        self.meeting.start_discussion()

        discussion = self.agent.discuss(self.meeting)

        self.assertTrue(discussion.recommendation_changed)
        self.assertEqual(
            self.meeting.discussions["squad_leader"],
            discussion,
        )
        discussion_request = json.loads(self.provider.calls[1][1])
        self.assertEqual(
            discussion_request["other_agents_analyses"]["tactical_advisor"][
                "recommendation"
            ],
            peer_analysis.recommendation,
        )
        self.assertEqual(
            discussion_request["other_agents_analyses"]["tactical_advisor"]["facts"],
            list(peer_analysis.facts),
        )
        self.assertEqual(len(self.provider.calls), 2)

    def test_malformed_provider_output_is_not_recorded_as_analysis(self) -> None:
        self.provider.responses[0] = "not valid JSON"

        with self.assertRaises(InvalidProviderResponseError):
            self.agent.analyze(self.meeting)

        self.assertNotIn("squad_leader", self.meeting.analyses)

    def test_analysis_requires_the_complete_response_schema(self) -> None:
        self.provider.responses[0] = json.dumps(
            {
                "recommendation": "Proceed",
                "reasoning": "Based on the available information.",
                "main_risks": "Visibility is unknown.",
                "missing_information": "Current visibility.",
                "suggested_improvements": "Request an observation.",
                "confidence": 0.6,
            }
        )

        with self.assertRaisesRegex(InvalidProviderResponseError, "facts"):
            self.agent.analyze(self.meeting)
        self.assertNotIn("squad_leader", self.meeting.analyses)

    def test_discussion_requires_peer_analysis(self) -> None:
        self.agent.analyze(self.meeting)
        self.meeting.start_discussion()

        with self.assertRaisesRegex(ValueError, "another agent"):
            self.agent.discuss(self.meeting)
        self.assertEqual(len(self.provider.calls), 1)

    def test_agent_status_is_reported_by_its_configured_provider(self) -> None:
        self.assertEqual(self.agent.connection_status.provider, "test-provider")
        self.assertEqual(self.agent.connection_status.model, "test-model")

    def test_agent_checks_meeting_phase_before_provider_call(self) -> None:
        meeting = CouncilMeeting("Move to the rally point")

        with self.assertRaisesRegex(RuntimeError, "ANALYSIS phase"):
            self.agent.analyze(meeting)
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
