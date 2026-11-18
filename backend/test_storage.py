import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.council import (
    DEFAULT_SEATS,
    AgentAnalysis,
    AgentDiscussion,
    CommanderOption,
    CouncilMeeting,
    MeetingState,
    VoteChoice,
)
from backend.mission_state import MissionState, MissionStatus, Operator
from backend.storage import (
    PersistenceConflictError,
    SQLiteStore,
)


def sample_analysis(agent_id: str) -> AgentAnalysis:
    return AgentAnalysis(
        agent_id=agent_id,
        recommendation="Proceed cautiously",
        reasoning="This follows from the supplied mission facts.",
        facts=("The objective is present in the mission brief.",),
        assumptions=("The brief remains current.",),
        main_risks="Current conditions may differ.",
        missing_information="Current conditions are unknown.",
        suggested_improvements="Confirm conditions before acting.",
        confidence=0.7,
    )


def sample_discussion(agent_id: str) -> AgentDiscussion:
    return AgentDiscussion(
        agent_id=agent_id,
        challenges="The mission conditions are not confirmed.",
        reconsideration="The recommendation should account for that uncertainty.",
        recommendation_changed=True,
        updated_recommendation="Wait for updated conditions.",
        reasoning="The missing information is relevant to the motion.",
        confidence=0.8,
    )


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "squad.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_mission(self) -> MissionState:
        mission = MissionState(
            "mission-1", "Training", "Reach the rally point"
        )
        mission.set_operators(
            (
                Operator(
                    "operator-1",
                    status="ready",
                    assignment="observer",
                    available=True,
                ),
                Operator("operator-2"),
            )
        )
        mission.set_available_equipment(("radio", "map"))
        mission.add_known_threat("Confirmed obstruction")
        mission.add_unknown_information("Current visibility")
        mission.add_observation("A signal was reported")
        mission.record_council_decision("Motion approved")
        mission.record_commander_order("Hold position")
        mission.set_status(MissionStatus.ACTIVE)
        return mission

    def create_finalized_meeting(self, mission: MissionState) -> CouncilMeeting:
        meeting = CouncilMeeting(
            "Move to the rally point",
            mission_snapshot=mission.snapshot,
        )
        meeting.start()
        meeting.start_analysis()
        for seat in DEFAULT_SEATS:
            meeting.submit_analysis(sample_analysis(seat))
        meeting.start_discussion()
        for seat in DEFAULT_SEATS:
            meeting.submit_discussion(sample_discussion(seat))
        meeting.start_initial_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.NO)
        meeting.submit_initial_vote(DEFAULT_SEATS[1], VoteChoice.YES)
        meeting.submit_initial_vote(DEFAULT_SEATS[2], VoteChoice.NO)
        meeting.start_revision()
        meeting.revise_vote(
            DEFAULT_SEATS[0], VoteChoice.YES, "Updated conditions were confirmed."
        )
        meeting.finalize()
        meeting.decide(CommanderOption.AUTHORIZE, "Authorized by commander.")
        return meeting

    def test_mission_snapshot_recovers_after_reopening_database(self) -> None:
        mission = self.create_mission()
        expected = mission.snapshot
        with SQLiteStore(self.database_path) as store:
            store.save_mission(expected)

        with SQLiteStore(self.database_path) as reopened:
            restored = reopened.load_mission("mission-1")

        self.assertEqual(restored, expected)

    def test_mission_event_and_fact_history_cannot_be_rolled_back(self) -> None:
        mission = self.create_mission()
        earlier_snapshot = mission.snapshot
        with SQLiteStore(self.database_path) as store:
            store.save_mission(earlier_snapshot)
            mission.add_known_threat("Confirmed activity near the east side")
            mission.add_observation("A second signal was reported")
            updated_snapshot = mission.snapshot
            store.save_mission(updated_snapshot)

            with self.assertRaises(PersistenceConflictError):
                store.save_mission(earlier_snapshot)
            self.assertEqual(
                store.load_mission("mission-1"), updated_snapshot
            )

    def test_full_council_history_recovers_after_reopening_database(self) -> None:
        mission = self.create_mission()
        meeting = self.create_finalized_meeting(mission)
        with SQLiteStore(self.database_path) as store:
            store.save_mission(mission.snapshot)
            store.save_meeting("meeting-1", "mission-1", meeting)

        mission.update_objective("A later mission update")
        with SQLiteStore(self.database_path) as reopened:
            restored = reopened.load_meeting("meeting-1")

        self.assertIsNotNone(restored)
        self.assertEqual(restored.state, MeetingState.COMMANDER_DECISION)
        self.assertEqual(restored.motion, meeting.motion)
        self.assertEqual(restored.mission_snapshot, meeting.mission_snapshot)
        self.assertEqual(restored.analyses, tuple(meeting.analyses.values()))
        self.assertEqual(restored.discussions, tuple(meeting.discussions.values()))
        self.assertEqual(restored.vote_history, meeting.vote_history)
        self.assertEqual(restored.commander_decision, meeting.commander_decision)
        self.assertTrue(restored.result.approved)
        self.assertFalse(restored.result.incomplete)

    def test_incomplete_vote_result_recovers_without_invented_votes(self) -> None:
        mission = self.create_mission()
        meeting = CouncilMeeting(
            "Move to the rally point",
            mission_snapshot=mission.snapshot,
        )
        meeting.start()
        meeting.start_analysis()
        meeting.submit_analysis(sample_analysis(DEFAULT_SEATS[0]))
        meeting.start_discussion()
        meeting.start_initial_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.YES)
        meeting.start_revision()
        meeting.finalize()

        with SQLiteStore(self.database_path) as store:
            store.save_mission(mission.snapshot)
            store.save_meeting("meeting-incomplete", "mission-1", meeting)
        with SQLiteStore(self.database_path) as reopened:
            restored = reopened.load_meeting("meeting-incomplete")

        self.assertTrue(restored.result.incomplete)
        self.assertEqual(restored.result.missing_voters, DEFAULT_SEATS[1:])
        self.assertEqual(len(restored.vote_history), 1)

    def test_vote_history_can_be_appended_but_not_overwritten(self) -> None:
        mission = self.create_mission()
        meeting = CouncilMeeting(
            "Move to the rally point",
            mission_snapshot=mission.snapshot,
        )
        meeting.start()
        meeting.start_analysis()
        for seat in DEFAULT_SEATS:
            meeting.submit_analysis(sample_analysis(seat))
        meeting.start_discussion()
        meeting.start_initial_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.NO)
        with SQLiteStore(self.database_path) as store:
            store.save_mission(mission.snapshot)
            store.save_meeting("meeting-2", "mission-1", meeting)

            meeting.start_revision()
            meeting.revise_vote(
                DEFAULT_SEATS[0], VoteChoice.YES, "New information changed my vote."
            )
            store.save_meeting("meeting-2", "mission-1", meeting)
            self.assertEqual(
                len(store.load_meeting("meeting-2").vote_history), 2
            )

        conflicting = CouncilMeeting(
            "Different motion",
            mission_snapshot=mission.snapshot,
        )
        with SQLiteStore(self.database_path) as reopened:
            with self.assertRaises(PersistenceConflictError):
                reopened.save_meeting("meeting-2", "mission-1", conflicting)

    def test_provider_settings_persist_without_secret_columns(self) -> None:
        with SQLiteStore(self.database_path) as store:
            store.save_provider_config(
                "local-qwen",
                "ollama",
                "qwen2.5:3b",
                "http://127.0.0.1:11434",
            )
        connection = sqlite3.connect(self.database_path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(provider_configs)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertNotIn("api_key", columns)
        self.assertNotIn("secret", columns)
        with SQLiteStore(self.database_path) as store:
            with self.assertRaises(ValueError):
                store.save_provider_config(
                    "remote",
                    "provider",
                    "model",
                    "https://user:password@example.invalid",
                )
            with self.assertRaises(ValueError):
                store.save_provider_config(
                    "query-secret",
                    "provider",
                    "model",
                    "https://example.invalid?api_key=secret",
                )
            with self.assertRaises(ValueError):
                store.save_provider_config(
                    "invalid-port",
                    "provider",
                    "model",
                    "https://example.invalid:invalid",
                )

        with SQLiteStore(self.database_path) as reopened:
            config = reopened.load_provider_config("local-qwen")
        self.assertEqual(config.provider, "ollama")
        self.assertEqual(config.model, "qwen2.5:3b")
        self.assertTrue(config.enabled)

    def test_meeting_requires_a_persisted_mission(self) -> None:
        mission = self.create_mission()
        meeting = self.create_finalized_meeting(mission)
        with SQLiteStore(self.database_path) as store:
            with self.assertRaisesRegex(ValueError, "mission must be saved"):
                store.save_meeting("meeting-3", "mission-1", meeting)
            self.assertIsNone(store.load_meeting("meeting-3"))

    def test_missing_records_return_none(self) -> None:
        with SQLiteStore(self.database_path) as store:
            self.assertIsNone(store.load_mission("missing"))
            self.assertIsNone(store.load_meeting("missing"))
            self.assertIsNone(store.load_provider_config("missing"))


if __name__ == "__main__":
    unittest.main()
