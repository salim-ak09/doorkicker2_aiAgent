import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.providers import (
    ProviderStatus,
    ProviderUnavailableError,
)


class FakeProvider:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def get_status(self) -> ProviderStatus:
        return ProviderStatus("test", True, "test-model", True, "connected")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if "Consider the other agents' actual analyses" in system_prompt:
            return json.dumps(
                {
                    "challenges": "The peer notes an important unknown.",
                    "reconsideration": "The recommendation remains cautious.",
                    "recommendation_changed": False,
                    "updated_recommendation": f"Proceed cautiously ({self.agent_id})",
                    "reasoning": "The supplied discussion was considered.",
                    "confidence": 0.6,
                }
            )
        return json.dumps(
            {
                "recommendation": f"Proceed cautiously ({self.agent_id})",
                "reasoning": "This uses only the supplied mission information.",
                "facts": ["The objective is present in the mission snapshot."],
                "assumptions": ["The snapshot remains current."],
                "main_risks": "Conditions may change.",
                "missing_information": "Current conditions are unknown.",
                "suggested_improvements": "Request updated information.",
                "confidence": 0.65,
            }
        )


class OfflineProvider:
    def get_status(self) -> ProviderStatus:
        return ProviderStatus(
            "ollama", False, "qwen2.5:3b", False, "Ollama endpoint is unavailable"
        )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        raise ProviderUnavailableError("Ollama endpoint is unavailable")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "api.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_api_status_mission_and_provider_endpoints(self) -> None:
        app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/status").json(), {"status": "ok"})
            self.assertIn("Operation AI Squad", client.get("/").text)
            self.assertIn("async function start()", client.get("/assets/app.js").text)
            self.assertEqual(len(client.get("/api/agents").json()["agents"]), 3)
            providers = client.get("/api/providers").json()["providers"]
            self.assertEqual(len(providers), 3)
            self.assertEqual(
                [provider["agent_id"] for provider in providers],
                ["squad_leader", "tactical_advisor", "intelligence_officer"],
            )
            self.assertTrue(all(provider["available"] for provider in providers))
            self.assertEqual(
                client.get("/api/providers/unknown/status").status_code, 404
            )

            created = client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Reach the rally point",
                },
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(
                len(client.get("/api/missions").json()["missions"]), 1
            )
            updated = client.patch(
                "/api/missions/mission-1",
                json={
                    "known_threats": ["Confirmed obstruction"],
                    "unknown_information": ["Current visibility"],
                    "observations": ["A signal was reported"],
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["known_threats"], ["Confirmed obstruction"])
            self.assertEqual(
                client.get("/api/missions/mission-1/events").status_code, 200
            )

    def test_complete_council_workflow_and_live_events(self) -> None:
        app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws/events") as websocket:
                self.assertEqual(websocket.receive_json(), {"event": "connected"})
                mission = client.post(
                    "/api/missions",
                    json={
                        "mission_id": "mission-1",
                        "name": "Training",
                        "objective": "Reach the rally point",
                    },
                )
                self.assertEqual(mission.status_code, 201)
                self.assertEqual(
                    websocket.receive_json()["event"], "mission.created"
                )
                meeting_response = client.post(
                    "/api/missions/mission-1/meetings",
                    json={
                        "meeting_id": "meeting-1",
                        "motion": "Move to the rally point",
                    },
                )
                self.assertEqual(meeting_response.status_code, 201)
                self.assertEqual(
                    len(
                        client.get(
                            "/api/missions/mission-1/meetings"
                        ).json()["meetings"]
                    ),
                    1,
                )
                self.assertEqual(
                    client.post(
                        "/api/meetings/meeting-1/analysis/start"
                    ).status_code,
                    200,
                )

                for agent_id in ("squad_leader", "tactical_advisor"):
                    response = client.post(
                        "/api/meetings/meeting-1/analysis",
                        json={"agent_id": agent_id},
                    )
                    self.assertEqual(response.status_code, 200, response.text)

                self.assertEqual(
                    client.post(
                        "/api/meetings/meeting-1/discussion/start"
                    ).status_code,
                    200,
                )
                discussion = client.post(
                    "/api/meetings/meeting-1/discussion",
                    json={"agent_id": "squad_leader"},
                )
                self.assertEqual(discussion.status_code, 200, discussion.text)
                self.assertEqual(
                    client.post("/api/meetings/meeting-1/votes/start").status_code,
                    200,
                )
                for agent_id, choice in (
                    ("squad_leader", "NO"),
                    ("tactical_advisor", "YES"),
                    ("intelligence_officer", "NO"),
                ):
                    response = client.post(
                        "/api/meetings/meeting-1/votes",
                        json={"agent_id": agent_id, "choice": choice},
                    )
                    self.assertEqual(response.status_code, 200, response.text)

                self.assertEqual(
                    client.post("/api/meetings/meeting-1/revision/start").status_code,
                    200,
                )
                revision = client.post(
                    "/api/meetings/meeting-1/votes/squad_leader/revision",
                    json={
                        "choice": "YES",
                        "explanation": "Updated information changed my assessment.",
                    },
                )
                self.assertEqual(revision.status_code, 200, revision.text)
                result = client.post("/api/meetings/meeting-1/finalize")
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()["approved"])

                decision = client.post(
                    "/api/meetings/meeting-1/commander-decision",
                    json={
                        "option": "AUTHORIZE",
                        "explanation": "Approved by the commander.",
                    },
                )
                self.assertEqual(decision.status_code, 200, decision.text)
                self.assertEqual(
                    client.get("/api/meetings/meeting-1/result").json()["yes_votes"],
                    2,
                )
                self.assertEqual(
                    client.get("/api/meetings/meeting-1").json()["state"],
                    "COMMANDER_DECISION",
                )

    def test_provider_failure_does_not_invent_or_record_analysis(self) -> None:
        app = create_app(
            self.database_path,
            provider_factory=lambda _agent_id: OfflineProvider(),
        )
        with TestClient(app) as client:
            client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
            )
            client.post(
                "/api/missions/mission-1/meetings",
                json={"meeting_id": "meeting-1", "motion": "Review the plan"},
            )
            client.post("/api/meetings/meeting-1/analysis/start")
            response = client.post(
                "/api/meetings/meeting-1/analysis",
                json={"agent_id": "squad_leader"},
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                client.get("/api/meetings/meeting-1").json()["analyses"], []
            )

    def test_persisted_history_is_readable_after_app_restart(self) -> None:
        first_app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(first_app) as first_client:
            first_client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
            )
            created = first_client.post(
                "/api/missions/mission-1/meetings",
                json={"meeting_id": "meeting-1", "motion": "Review the plan"},
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(
                first_client.post(
                    "/api/meetings/meeting-1/analysis/start"
                ).status_code,
                200,
            )
            self.assertEqual(
                first_client.post(
                    "/api/meetings/meeting-1/analysis",
                    json={"agent_id": "squad_leader"},
                ).status_code,
                200,
            )

        second_app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(second_app) as second_client:
            self.assertEqual(
                second_client.get("/api/missions/mission-1").status_code, 200
            )
            stored = second_client.get("/api/meetings/meeting-1")
            self.assertEqual(stored.status_code, 200)
            self.assertEqual(stored.json()["motion"], "Review the plan")
            self.assertEqual(stored.json()["state"], "ANALYSIS")
            resumed = second_client.post(
                "/api/meetings/meeting-1/analysis",
                json={"agent_id": "tactical_advisor"},
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(
                len(second_client.get("/api/meetings/meeting-1").json()["analyses"]),
                2,
            )

    def test_final_result_and_commander_decision_survive_restart(self) -> None:
        first_app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(first_app) as client:
            client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
            )
            client.post(
                "/api/missions/mission-1/meetings",
                json={"meeting_id": "meeting-1", "motion": "Review the plan"},
            )
            client.post("/api/meetings/meeting-1/analysis/start")
            for agent_id in ("squad_leader", "tactical_advisor"):
                client.post(
                    "/api/meetings/meeting-1/analysis",
                    json={"agent_id": agent_id},
                )
            client.post("/api/meetings/meeting-1/discussion/start")
            client.post(
                "/api/meetings/meeting-1/discussion",
                json={"agent_id": "squad_leader"},
            )
            client.post("/api/meetings/meeting-1/votes/start")
            for agent_id, choice in (
                ("squad_leader", "YES"),
                ("tactical_advisor", "YES"),
                ("intelligence_officer", "NO"),
            ):
                client.post(
                    "/api/meetings/meeting-1/votes",
                    json={"agent_id": agent_id, "choice": choice},
                )
            client.post("/api/meetings/meeting-1/revision/start")
            finalized = client.post("/api/meetings/meeting-1/finalize")
            self.assertEqual(finalized.status_code, 200, finalized.text)
            decision = client.post(
                "/api/meetings/meeting-1/commander-decision",
                json={"option": "AUTHORIZE"},
            )
            self.assertEqual(decision.status_code, 200, decision.text)

        second_app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(second_app) as client:
            result = client.get("/api/meetings/meeting-1/result")
            meeting = client.get("/api/meetings/meeting-1")

        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["approved"])
        self.assertEqual(meeting.json()["commander_decision"]["option"], "AUTHORIZE")

    def test_invalid_requests_and_state_transitions_are_rejected(self) -> None:
        app = create_app(
            self.database_path,
            provider_factory=lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(app) as client:
            invalid_mission = client.post(
                "/api/missions",
                json={"mission_id": "", "name": "Bad", "objective": "Bad"},
            )
            self.assertEqual(invalid_mission.status_code, 422)
            blank_mission = client.post(
                "/api/missions",
                json={"mission_id": " ", "name": "Bad", "objective": "Bad"},
            )
            self.assertEqual(blank_mission.status_code, 422)
            client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
            )
            client.post(
                "/api/missions/mission-1/meetings",
                json={"meeting_id": "meeting-1", "motion": "Review the plan"},
            )
            client.post("/api/meetings/meeting-1/analysis/start")
            invalid_vote = client.post(
                "/api/meetings/meeting-1/votes",
                json={"agent_id": "squad_leader", "choice": "MAYBE"},
            )
            self.assertEqual(invalid_vote.status_code, 422)
            invalid_transition = client.post(
                "/api/meetings/meeting-1/finalize"
            )
            self.assertEqual(invalid_transition.status_code, 409)


if __name__ == "__main__":
    unittest.main()
