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
        recommendation="YES",
        reasoning="This follows from the supplied mission facts.",
        facts=("The objective is present in the mission brief.",),
        assumptions=("The brief remains current.",),
        main_risks="Current conditions may differ.",
        missing_information="Current conditions are unknown.",
        suggested_improvements="Confirm conditions before acting.",
        confidence=0.7,
        provider="test-provider",
        model="test-model",
        completed_at="2025-01-01T00:00:00+00:00",
    )


def sample_discussion(agent_id: str, round_number: int = 1) -> AgentDiscussion:
    return AgentDiscussion(
        agent_id=agent_id,
        challenges="The mission conditions are not confirmed.",
        reconsideration="The recommendation should account for that uncertainty.",
        recommendation_changed=True,
        updated_recommendation="NO",
        reasoning="The missing information is relevant to the motion.",
        confidence=0.8,
        round_number=round_number,
        provider="test-provider",
        model="test-model",
        completed_at="2025-01-01T00:01:00+00:00",
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
        for round_number in range(2, meeting.max_discussion_rounds + 1):
            meeting.start_next_discussion_round()
            for seat in DEFAULT_SEATS:
                meeting.submit_discussion(
                    sample_discussion(seat, round_number)
                )
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

    def test_search_returns_dated_historical_source_labels(self) -> None:
        mission = self.create_mission()
        with SQLiteStore(self.database_path) as store:
            store.save_mission(mission.snapshot)
            results = store.search_mission_history("obstruction", "mission-1")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "confirmed threat record")
        self.assertTrue(results[0]["historical"])
        self.assertNotEqual(
            results[0]["occurred_at"], "Date unavailable (legacy record)"
        )
        self.assertIn("verify before treating it as current", results[0]["warning"])

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
        for seat in DEFAULT_SEATS[:2]:
            meeting.submit_analysis(sample_analysis(seat))
        meeting.start_discussion()
        for round_number in range(1, meeting.max_discussion_rounds + 1):
            if round_number > 1:
                meeting.start_next_discussion_round()
            for seat in DEFAULT_SEATS[:2]:
                meeting.submit_discussion(sample_discussion(seat, round_number))
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
        for round_number in range(1, meeting.max_discussion_rounds + 1):
            if round_number > 1:
                meeting.start_next_discussion_round()
            for seat in DEFAULT_SEATS:
                meeting.submit_discussion(sample_discussion(seat, round_number))
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

    def test_database_triggers_reject_direct_audit_history_overwrites(self) -> None:
        mission = self.create_mission()
        meeting = self.create_finalized_meeting(mission)
        with SQLiteStore(self.database_path) as store:
            store.save_mission(mission.snapshot)
            store.save_meeting("meeting-immutable", "mission-1", meeting)

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    """
                    UPDATE votes SET choice = 'NO'
                    WHERE meeting_id = 'meeting-immutable' AND revision = 0
                    """
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    """
                    UPDATE mission_events SET description = 'rewritten'
                    WHERE mission_id = 'mission-1' AND sequence = 1
                    """
                )
        finally:
            connection.close()

    def test_schema_v1_database_migrates_with_legacy_discussion_limit(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE missions (
                mission_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                objective TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE meetings (
                meeting_id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL REFERENCES missions(mission_id),
                motion TEXT NOT NULL,
                seats_json TEXT NOT NULL,
                mission_snapshot_json TEXT NOT NULL,
                state TEXT NOT NULL
            );
            INSERT INTO missions VALUES ('mission-1', 'Training', 'Objective', 'ACTIVE');
            INSERT INTO meetings VALUES (
                'meeting-1', 'mission-1', 'Motion', '[]', '{}', 'INITIAL_VOTING'
            );
            PRAGMA user_version = 1;
            """
        )
        connection.close()

        with SQLiteStore(self.database_path) as migrated:
            row = migrated._connection.execute(
                """
                SELECT discussion_round, max_discussion_rounds
                FROM meetings WHERE meeting_id = 'meeting-1'
                """
            ).fetchone()
            version = migrated._connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

        self.assertEqual(version, 2)
        self.assertEqual(row["discussion_round"], 0)
        self.assertEqual(row["max_discussion_rounds"], 1)

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

    def test_database_unavailable_fails_explicitly(self) -> None:
        unavailable_path = Path(self.temp_dir.name) / "missing" / "squad.db"
        with self.assertRaises(sqlite3.OperationalError):
            SQLiteStore(unavailable_path)


if __name__ == "__main__":
    unittest.main()
