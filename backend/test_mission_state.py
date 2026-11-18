import unittest
from dataclasses import FrozenInstanceError

from backend.mission_state import MissionState, MissionStatus, Operator


class MissionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mission = MissionState(
            mission_id="mission-001",
            name="Training",
            objective="Secure the objective",
        )

    def test_snapshot_starts_with_unknowns_unconfirmed(self) -> None:
        snapshot = self.mission.snapshot

        self.assertEqual(snapshot.status, MissionStatus.PLANNING)
        self.assertEqual(snapshot.operators, ())
        self.assertEqual(snapshot.known_threats, ())
        self.assertEqual(snapshot.unknown_information, ())
        self.assertEqual(snapshot.events[0].category, "mission.created")

    def test_observations_do_not_promote_unknowns_to_facts(self) -> None:
        self.mission.add_unknown_information("Threat location not confirmed")
        self.mission.add_observation("A sound was reported near the north entrance")
        self.mission.add_known_threat("Confirmed obstruction at the west gate")

        snapshot = self.mission.snapshot
        self.assertEqual(
            snapshot.unknown_information, ("Threat location not confirmed",)
        )
        self.assertEqual(
            snapshot.observations,
            ("A sound was reported near the north entrance",),
        )
        self.assertEqual(
            snapshot.known_threats,
            ("Confirmed obstruction at the west gate",),
        )

    def test_operator_unknown_status_is_preserved(self) -> None:
        operator = Operator(operator_id="operator-1")
        self.mission.set_operators((operator,))

        self.assertIsNone(self.mission.snapshot.operators[0].status)
        self.assertIsNone(self.mission.snapshot.operators[0].available)

    def test_duplicate_observation_does_not_duplicate_state_or_events(self) -> None:
        self.mission.add_observation("Reported sound")
        event_count = len(self.mission.snapshot.events)
        self.mission.add_observation("Reported sound")

        self.assertEqual(self.mission.snapshot.observations, ("Reported sound",))
        self.assertEqual(len(self.mission.snapshot.events), event_count)

    def test_snapshots_and_history_are_immutable_and_sequenced(self) -> None:
        first = self.mission.snapshot
        self.mission.update_objective("Reach the rally point")
        self.mission.record_commander_order("Hold position")
        second = self.mission.snapshot

        self.assertEqual(first.objective, "Secure the objective")
        self.assertEqual(second.objective, "Reach the rally point")
        self.assertEqual(
            tuple(event.sequence for event in second.events),
            (1, 2, 3),
        )
        with self.assertRaises(FrozenInstanceError):
            second.objective = "Changed outside the mission state"

    def test_mission_state_can_resume_from_a_persisted_snapshot(self) -> None:
        self.mission.add_unknown_information("Current visibility")
        original = self.mission.snapshot

        restored = MissionState.from_snapshot(original)
        restored.add_observation("A new report")

        self.assertEqual(restored.snapshot.events[: len(original.events)], original.events)
        self.assertEqual(
            restored.snapshot.unknown_information,
            ("Current visibility",),
        )
        self.assertEqual(len(restored.snapshot.events), len(original.events) + 1)

    def test_mission_status_transitions_are_validated(self) -> None:
        self.mission.set_status(MissionStatus.ACTIVE)
        self.mission.set_status(MissionStatus.COMPLETED)

        with self.assertRaisesRegex(ValueError, "cannot change mission status"):
            self.mission.set_status(MissionStatus.ACTIVE)

    def test_invalid_or_duplicate_updates_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "objective"):
            self.mission.update_objective(" ")
        with self.assertRaisesRegex(ValueError, "unique"):
            self.mission.set_operators(
                (Operator("same-id"), Operator("same-id"))
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            self.mission.set_available_equipment(("radio", "radio"))


if __name__ == "__main__":
    unittest.main()
