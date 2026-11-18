import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api import create_app
from backend.providers import (
    ModelUnavailableError,
    ProviderTimeoutError,
    ProviderStatus,
    ProviderUnavailableError,
)

ACCESS_TOKEN = "test-access-token"


def create_test_app(database_path, provider_factory):
    return create_app(
        database_path,
        provider_factory=provider_factory,
        access_token=ACCESS_TOKEN,
    )


@contextmanager
def authenticated_client(app):
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        login = client.post(
            "/api/auth/login",
            json={"token": ACCESS_TOKEN},
            headers={"Origin": "http://127.0.0.1"},
        )
        if login.status_code != 200:
            raise AssertionError(login.text)
        client.headers.update(
            {
                "Origin": "http://127.0.0.1",
                "X-CSRF-Token": login.json()["csrf_token"],
            }
        )
        yield client


class FakeProvider:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.calls: list[tuple[str, str]] = []

    def get_status(self) -> ProviderStatus:
        return ProviderStatus("test", True, "test-model", True, "connected")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "Consider the other agents' actual analyses" in system_prompt:
            return json.dumps(
                {
                    "agreement": "The objective is common to both analyses.",
                    "disagreement": "The preferred timing differs.",
                    "weak_reasoning": "No major weakness identified.",
                    "missing_information": "Current conditions.",
                    "possible_risks": "Conditions may change.",
                    "challenges": "The peer notes an important unknown.",
                    "reconsideration": "The recommendation remains cautious.",
                    "recommendation_changed": False,
                    "updated_recommendation": "YES",
                    "reasoning": "The supplied discussion was considered.",
                    "confidence": 0.6,
                    "unresolved_disagreements": [],
                }
            )
        return json.dumps(
            {
                "recommendation": "YES",
                "reasoning": "This uses only the supplied mission information.",
                "facts": ["The objective is present in the mission snapshot."],
                "assumptions": ["The snapshot remains current."],
                "supporting_evidence": ["The objective is in the snapshot."],
                "main_concerns": ["Conditions may change."],
                "alternative_recommendations": ["Wait for updated information."],
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


class FailingProvider:
    def __init__(self, failure: Exception | None = None, response: str = "") -> None:
        self.failure = failure
        self.response = response

    def get_status(self) -> ProviderStatus:
        return ProviderStatus("test", True, "test-model", True, "connected")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if self.failure:
            raise self.failure
        return self.response


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "api.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_authentication_csrf_and_loopback_checks_fail_closed(self) -> None:
        app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with TestClient(
            app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ) as client:
            self.assertEqual(client.get("/api/status").status_code, 401)
            with self.assertRaises(WebSocketDisconnect) as unauthenticated_socket:
                with client.websocket_connect(
                    "ws://127.0.0.1/ws/events",
                    headers={"Origin": "http://127.0.0.1"},
                ):
                    pass
            self.assertEqual(unauthenticated_socket.exception.code, 4401)
            invalid_login = client.post(
                "/api/auth/login",
                json={"token": "wrong"},
                headers={"Origin": "http://127.0.0.1"},
            )
            self.assertEqual(invalid_login.status_code, 401)

            login = client.post(
                "/api/auth/login",
                json={"token": ACCESS_TOKEN},
                headers={"Origin": "http://127.0.0.1"},
            )
            self.assertEqual(login.status_code, 200)
            csrf_token = login.json()["csrf_token"]
            with self.assertRaises(WebSocketDisconnect) as foreign_origin:
                with client.websocket_connect(
                    "ws://127.0.0.1/ws/events",
                    headers={"Origin": "http://evil.example"},
                ):
                    pass
            self.assertEqual(foreign_origin.exception.code, 4403)
            no_csrf = client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
                headers={"Origin": "http://127.0.0.1"},
            )
            self.assertEqual(no_csrf.status_code, 403)
            valid_write = client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
                headers={
                    "Origin": "http://127.0.0.1",
                    "X-CSRF-Token": csrf_token,
                },
            )
            self.assertEqual(valid_write.status_code, 201)

        with TestClient(
            app,
            base_url="http://127.0.0.1",
            client=("192.0.2.10", 50000),
        ) as remote_client:
            self.assertEqual(remote_client.get("/").status_code, 403)

    def test_application_refuses_startup_without_access_token(self) -> None:
        with patch.dict("os.environ", {"AI_SQUAD_ACCESS_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "AI_SQUAD_ACCESS_TOKEN"):
                create_app(
                    self.database_path,
                    provider_factory=lambda _id: None,
                )

    def test_discussion_round_limit_is_configurable(self) -> None:
        with patch.dict("os.environ", {"AI_SQUAD_MAX_DISCUSSION_ROUNDS": "2"}):
            app = create_test_app(
                self.database_path,
                lambda agent_id: FakeProvider(agent_id),
            )
            with authenticated_client(app) as client:
                client.post(
                    "/api/missions",
                    json={
                        "mission_id": "mission-1",
                        "name": "Training",
                        "objective": "Review a plan",
                    },
                )
                meeting = client.post(
                    "/api/missions/mission-1/meetings",
                    json={"meeting_id": "meeting-1", "motion": "Review the plan"},
                )
                self.assertEqual(meeting.status_code, 201)
                self.assertEqual(meeting.json()["max_discussion_rounds"], 2)

    def test_api_status_mission_and_provider_endpoints(self) -> None:
        app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(app) as client:
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
            self.assertTrue(all(provider["shared_with"] == 3 for provider in providers))
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
        app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(app) as client:
            with client.websocket_connect(
                "ws://127.0.0.1/ws/events",
                headers={"Origin": "http://127.0.0.1"},
            ) as websocket:
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

                participating_agents = ("squad_leader", "tactical_advisor")
                for agent_id in participating_agents:
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
                for round_number in range(1, 4):
                    if round_number > 1:
                        self.assertEqual(
                            client.post(
                                "/api/meetings/meeting-1/discussion/next"
                            ).status_code,
                            200,
                        )
                    for agent_id in participating_agents:
                        discussion = client.post(
                            "/api/meetings/meeting-1/discussion",
                            json={"agent_id": agent_id},
                        )
                        self.assertEqual(
                            discussion.status_code, 200, discussion.text
                        )
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
                stored_meeting = client.get("/api/meetings/meeting-1").json()
                self.assertEqual(stored_meeting["max_discussion_rounds"], 3)
                self.assertEqual(
                    [item["round_number"] for item in stored_meeting["discussions"]],
                    [1, 1, 2, 2, 3, 3],
                )
                self.assertTrue(
                    all(item["provider"] == "test" for item in stored_meeting["discussions"])
                )

    def test_search_context_is_source_labeled_and_never_promoted_to_current(self) -> None:
        providers = {}

        def provider_factory(agent_id):
            providers[agent_id] = FakeProvider(agent_id)
            return providers[agent_id]

        app = create_test_app(self.database_path, provider_factory)
        with authenticated_client(app) as client:
            client.post(
                "/api/missions",
                json={
                    "mission_id": "mission-1",
                    "name": "Training",
                    "objective": "Review a plan",
                },
            )
            client.patch(
                "/api/missions/mission-1",
                json={"observations": ["Signal observed during prior review"]},
            )
            search = client.get(
                "/api/missions/mission-1/history/search?query=Signal"
            )
            self.assertEqual(search.status_code, 200)
            result = search.json()["results"][0]
            self.assertTrue(result["historical"])
            self.assertEqual(result["source"], "historical observation")
            self.assertIn("verify before treating it as current", result["warning"])

            client.post(
                "/api/missions/mission-1/meetings",
                json={"meeting_id": "meeting-1", "motion": "Review the plan"},
            )
            client.post("/api/meetings/meeting-1/analysis/start")
            response = client.post(
                "/api/meetings/meeting-1/analysis",
                json={
                    "agent_id": "squad_leader",
                    "history_query": "Signal",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["provider"], "test")
        self.assertEqual(response.json()["model"], "test-model")
        self.assertEqual(response.json()["response_type"], "real_ai")
        self.assertTrue(response.json()["completed_at"])
        request_payload = json.loads(providers["squad_leader"].calls[0][1])
        history = request_payload["historical_context"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], "historical observation")
        self.assertTrue(history[0]["historical"])

    def test_provider_failure_does_not_invent_or_record_analysis(self) -> None:
        app = create_test_app(
            self.database_path,
            lambda _agent_id: OfflineProvider(),
        )
        with authenticated_client(app) as client:
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
            provider = client.get("/api/providers").json()["providers"][0]
            self.assertEqual(provider["request_status"], "request_failed")
            self.assertEqual(provider["availability"], "provider_unavailable")

    def test_timeout_missing_model_and_malformed_output_are_explicit(self) -> None:
        cases = (
            (ProviderTimeoutError("Provider request timed out"), "", 504),
            (ModelUnavailableError("Configured model is missing"), "", 503),
            (None, "not valid JSON", 502),
        )
        for index, (failure, content, expected_status) in enumerate(cases):
            with self.subTest(expected_status=expected_status):
                provider = FailingProvider(failure, content)
                app = create_test_app(
                    self.database_path.with_name(f"failure-{index}.db"),
                    lambda _agent_id: provider,
                )
                with authenticated_client(app) as client:
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
                    self.assertEqual(response.status_code, expected_status)
                    meeting = client.get("/api/meetings/meeting-1").json()
                    self.assertEqual(meeting["analyses"], [])

    def test_persisted_history_is_readable_after_app_restart(self) -> None:
        first_app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(first_app) as first_client:
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

        second_app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(second_app) as second_client:
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

    def test_interrupted_discussion_resumes_without_invented_responses(self) -> None:
        first_app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(first_app) as client:
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
            response = client.post(
                "/api/meetings/meeting-1/discussion",
                json={"agent_id": "squad_leader"},
            )
            self.assertEqual(response.status_code, 200, response.text)

        second_app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(second_app) as client:
            stored = client.get("/api/meetings/meeting-1").json()
            self.assertEqual(stored["discussion_round"], 1)
            self.assertEqual(len(stored["discussions"]), 1)
            resumed = client.post(
                "/api/meetings/meeting-1/discussion",
                json={"agent_id": "tactical_advisor"},
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            next_round = client.post(
                "/api/meetings/meeting-1/discussion/next"
            )
            self.assertEqual(next_round.status_code, 200, next_round.text)
            stored = client.get("/api/meetings/meeting-1").json()
            self.assertEqual(stored["discussion_round"], 2)
            self.assertEqual(
                sorted(item["round_number"] for item in stored["discussions"]),
                [1, 1],
            )

    def test_final_result_and_commander_decision_survive_restart(self) -> None:
        first_app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(first_app) as client:
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
            for round_number in range(1, 4):
                if round_number > 1:
                    client.post(
                        "/api/meetings/meeting-1/discussion/next"
                    )
                for agent_id in ("squad_leader", "tactical_advisor"):
                    client.post(
                        "/api/meetings/meeting-1/discussion",
                        json={"agent_id": agent_id},
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

        second_app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(second_app) as client:
            result = client.get("/api/meetings/meeting-1/result")
            meeting = client.get("/api/meetings/meeting-1")

        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["approved"])
        self.assertEqual(meeting.json()["commander_decision"]["option"], "AUTHORIZE")

    def test_invalid_requests_and_state_transitions_are_rejected(self) -> None:
        app = create_test_app(
            self.database_path,
            lambda agent_id: FakeProvider(agent_id),
        )
        with authenticated_client(app) as client:
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
