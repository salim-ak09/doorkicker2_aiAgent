import unittest

from backend.council import (
    DEFAULT_SEATS,
    AgentAnalysis,
    AgentDiscussion,
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
                recommendation="YES",
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
    for round_number in range(1, meeting.max_discussion_rounds + 1):
        if round_number > 1:
            meeting.start_next_discussion_round()
        for seat in DEFAULT_SEATS:
            meeting.submit_discussion(
                AgentDiscussion(
                    agent_id=seat,
                    challenges="No new material challenge.",
                    reconsideration="Recommendation remains supported.",
                    recommendation_changed=False,
                    updated_recommendation="YES",
                    reasoning="The supplied analysis remains adequate.",
                    confidence=0.7,
                    round_number=round_number,
                )
            )
    meeting.start_initial_voting()
    return meeting


class CouncilMeetingTests(unittest.TestCase):
    def test_discussion_rounds_are_complete_bounded_and_append_only(self) -> None:
        mission = MissionState("mission-2", "Training", "Review the route")
        meeting = CouncilMeeting(
            "Review the route",
            mission_snapshot=mission.snapshot,
            max_discussion_rounds=2,
        )
        meeting.start()
        meeting.start_analysis()
        for seat in DEFAULT_SEATS[:2]:
            meeting.submit_analysis(
                AgentAnalysis(
                    agent_id=seat,
                    recommendation="YES",
                    reasoning="The supplied objective is clear.",
                    facts=("The objective is present.",),
                    assumptions=(),
                    main_risks="Conditions may change.",
                    missing_information="Current conditions.",
                    suggested_improvements="Confirm conditions.",
                    confidence=0.6,
                )
            )
        meeting.start_discussion()
        first_round = AgentDiscussion(
            agent_id=DEFAULT_SEATS[0],
            challenges="No additional challenge.",
            reconsideration="Recommendation remains YES.",
            recommendation_changed=False,
            updated_recommendation="YES",
            reasoning="No contrary evidence was provided.",
            confidence=0.6,
        )
        meeting.submit_discussion(first_round)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            meeting.start_next_discussion_round()
        with self.assertRaisesRegex(ValueError, "already submitted"):
            meeting.submit_discussion(first_round)
        meeting.submit_discussion(
            AgentDiscussion(
                agent_id=DEFAULT_SEATS[1],
                challenges="No additional challenge.",
                reconsideration="Recommendation remains NO.",
                recommendation_changed=False,
                updated_recommendation="NO",
                reasoning="The concern remains unresolved.",
                confidence=0.6,
            )
        )
        meeting.start_next_discussion_round()
        self.assertEqual(meeting.discussion_round, 2)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            meeting.start_initial_voting()
        for seat, choice in zip(DEFAULT_SEATS[:2], ("YES", "NO")):
            meeting.submit_discussion(
                AgentDiscussion(
                    agent_id=seat,
                    challenges="No new information.",
                    reconsideration="Maintain the recommendation.",
                    recommendation_changed=False,
                    updated_recommendation=choice,
                    reasoning="The supplied material has not changed.",
                    confidence=0.6,
                    round_number=2,
                )
            )
        meeting.start_initial_voting()
        self.assertEqual(len(meeting.discussions), 4)
        self.assertTrue(meeting.unresolved_disagreements)

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
                recommendation="YES",
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
        with self.assertRaisesRegex(ValueError, "no initial vote"):
            meeting.revise_vote(
                DEFAULT_SEATS[1], VoteChoice.YES, "There was no initial vote."
            )
        with self.assertRaisesRegex(ValueError, "must change"):
            meeting.revise_vote(
                DEFAULT_SEATS[0], VoteChoice.NO, "This does not change the vote."
            )
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
                recommendation="YES",
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
