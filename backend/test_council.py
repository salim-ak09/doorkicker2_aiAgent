import unittest

from backend.council import (
    DEFAULT_SEATS,
    AgentAnalysis,
    CommanderOption,
    CouncilMeeting,
    MeetingState,
    VoteChoice,
)
from backend.mission_state import MissionState


def ready_for_voting() -> CouncilMeeting:
    mission = MissionState(
        "mission-1", "Training", "Move to the extraction point"
    )
    meeting = CouncilMeeting(
        "Move the squad to the extraction point",
        mission_snapshot=mission.snapshot,
    )
    meeting.start()
    meeting.start_analysis()
    for seat in DEFAULT_SEATS:
        meeting.submit_analysis(
            AgentAnalysis(
                agent_id=seat,
                recommendation="Proceed with caution",
                reasoning="The route is available in the provided mission brief.",
                facts=("The route is listed in the mission brief.",),
                assumptions=("The brief remains current.",),
                main_risks="The route may change.",
                missing_information="Current visibility is unknown.",
                suggested_improvements="Confirm conditions before acting.",
                confidence=0.7,
            )
        )
    meeting.start_discussion()
    meeting.start_initial_voting()
    return meeting


class CouncilMeetingTests(unittest.TestCase):
    def test_motion_and_independent_analyses_are_recorded(self) -> None:
        mission = MissionState("mission-1", "Training", "Secure the objective")
        meeting = CouncilMeeting(
            "Secure the objective", mission_snapshot=mission.snapshot
        )
        meeting.start()
        meeting.start_analysis()
        analyses = [
            AgentAnalysis(
                agent_id=seat,
                recommendation=f"Recommendation from {seat}",
                reasoning="Based on the supplied mission information.",
                facts=("The objective is listed in the mission brief.",),
                assumptions=("The mission brief is current.",),
                main_risks="Threat information may be incomplete.",
                missing_information="No current observation is available.",
                suggested_improvements="Request updated observations.",
                confidence=0.5,
            )
            for seat in DEFAULT_SEATS
        ]
        for analysis in analyses:
            meeting.submit_analysis(analysis)

        self.assertEqual(meeting.motion, "Secure the objective")
        self.assertEqual(set(meeting.analyses), set(DEFAULT_SEATS))
        self.assertEqual(meeting.state, MeetingState.ANALYSIS)

    def test_majority_and_commander_authorization(self) -> None:
        meeting = ready_for_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.YES)
        meeting.submit_initial_vote(DEFAULT_SEATS[1], VoteChoice.YES)
        meeting.submit_initial_vote(DEFAULT_SEATS[2], VoteChoice.NO)
        meeting.start_revision()

        result = meeting.finalize()

        self.assertTrue(result.approved)
        self.assertFalse(result.incomplete)
        self.assertEqual((result.yes_votes, result.no_votes), (2, 1))
        meeting.decide(CommanderOption.AUTHORIZE, "Approved by the commander.")
        self.assertEqual(meeting.state, MeetingState.COMMANDER_DECISION)

    def test_revision_requires_reason_and_preserves_history(self) -> None:
        meeting = ready_for_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.NO)
        initial = meeting.vote_history[0]
        meeting.start_revision()

        with self.assertRaisesRegex(ValueError, "requires an explanation"):
            meeting.revise_vote(DEFAULT_SEATS[0], VoteChoice.YES, "  ")
        self.assertEqual(meeting.vote_history, (initial,))

        meeting.revise_vote(
            DEFAULT_SEATS[0], VoteChoice.YES, "New information changed my assessment."
        )
        self.assertEqual(meeting.vote_history[0], initial)
        self.assertEqual(len(meeting.vote_history), 2)
        self.assertEqual(meeting.vote_history[1].revision, 1)

    def test_incomplete_votes_are_reported_without_fabricated_votes(self) -> None:
        meeting = ready_for_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.YES)
        meeting.submit_initial_vote(DEFAULT_SEATS[1], VoteChoice.YES)
        meeting.start_revision()

        result = meeting.finalize()

        self.assertTrue(result.approved)
        self.assertTrue(result.incomplete)
        self.assertEqual(result.missing_voters, (DEFAULT_SEATS[2],))
        self.assertEqual(len(result.votes), 2)

    def test_unapproved_motion_cannot_be_authorized(self) -> None:
        meeting = ready_for_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.NO)
        meeting.submit_initial_vote(DEFAULT_SEATS[1], VoteChoice.YES)
        meeting.submit_initial_vote(DEFAULT_SEATS[2], VoteChoice.NO)
        meeting.start_revision()
        meeting.finalize()

        with self.assertRaisesRegex(ValueError, "cannot authorize"):
            meeting.decide(CommanderOption.AUTHORIZE)
        meeting.decide(CommanderOption.REJECT)
        self.assertEqual(meeting.state, MeetingState.COMMANDER_DECISION)

    def test_invalid_state_transition_and_duplicate_votes_fail(self) -> None:
        meeting = CouncilMeeting("Secure the objective")
        with self.assertRaises(RuntimeError):
            meeting.start_discussion()
        meeting.start()
        with self.assertRaisesRegex(ValueError, "mission snapshot"):
            meeting.start_analysis()

        meeting = ready_for_voting()
        meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.YES)
        with self.assertRaisesRegex(ValueError, "already submitted"):
            meeting.submit_initial_vote(DEFAULT_SEATS[0], VoteChoice.NO)

    def test_analysis_confidence_and_canonical_motion_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "motion must not be empty"):
            CouncilMeeting("  ")
        with self.assertRaisesRegex(ValueError, "confidence"):
            AgentAnalysis(
                agent_id=DEFAULT_SEATS[0],
                recommendation="Proceed",
                reasoning="Reasoning",
                facts=(),
                assumptions=(),
                main_risks="Risks",
                missing_information="Unknown",
                suggested_improvements="Improve",
                confidence=1.1,
            )

    def test_workflow_state_and_motion_cannot_be_mutated_directly(self) -> None:
        meeting = CouncilMeeting("Secure the objective")
        with self.assertRaises(AttributeError):
            meeting.motion = "Change the motion"
        with self.assertRaises(AttributeError):
            meeting.state = MeetingState.FINALIZED

    def test_persisted_history_restores_complete_meeting_state(self) -> None:
        meeting = ready_for_voting()
        for seat, choice in zip(
            DEFAULT_SEATS,
            (VoteChoice.NO, VoteChoice.YES, VoteChoice.NO),
        ):
            meeting.submit_initial_vote(seat, choice)
        meeting.start_revision()
        meeting.revise_vote(
            DEFAULT_SEATS[0], VoteChoice.YES, "Updated conditions were confirmed."
        )
        meeting.finalize()
        meeting.decide(CommanderOption.AUTHORIZE, "Approved by the commander.")

        restored = CouncilMeeting.restore(
            motion=meeting.motion,
            seats=meeting.seats,
            mission_snapshot=meeting.mission_snapshot,
            state=meeting.state,
            analyses=tuple(meeting.analyses.values()),
            discussions=tuple(meeting.discussions.values()),
            vote_history=meeting.vote_history,
            commander_decision=meeting.commander_decision,
        )

        self.assertEqual(restored.state, meeting.state)
        self.assertEqual(restored.analyses, meeting.analyses)
        self.assertEqual(restored.discussions, meeting.discussions)
        self.assertEqual(restored.vote_history, meeting.vote_history)
        self.assertEqual(restored.result, meeting.result)
        self.assertEqual(restored.commander_decision, meeting.commander_decision)


if __name__ == "__main__":
    unittest.main()
