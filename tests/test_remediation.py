from __future__ import annotations

import unittest

from pilot107.core.remediation import (
    EvaluationOutcome,
    RemediationBudget,
    RemediationInvariantError,
    RemediationState,
    RemediationUsage,
    assert_remediation_transition,
)


class RemediationDomainTests(unittest.TestCase):
    def test_valid_multi_turn_path(self) -> None:
        path = (
            RemediationState.WAITING_EVIDENCE,
            RemediationState.DIAGNOSING,
            RemediationState.PLANNING,
            RemediationState.AWAITING_APPROVAL,
            RemediationState.READY,
            RemediationState.PREPARING,
            RemediationState.EXECUTING,
            RemediationState.EVALUATING,
            RemediationState.PLANNING,
        )
        for current, target in zip(path, path[1:], strict=False):
            assert_remediation_transition(current, target)

    def test_terminal_and_skipped_transitions_are_rejected(self) -> None:
        with self.assertRaises(RemediationInvariantError):
            assert_remediation_transition(
                RemediationState.SUCCEEDED,
                RemediationState.PLANNING,
            )
        with self.assertRaises(RemediationInvariantError):
            assert_remediation_transition(
                RemediationState.PLANNING,
                RemediationState.EXECUTING,
            )

    def test_budget_reports_first_exhausted_dimension(self) -> None:
        budget = RemediationBudget(max_attempts=2, max_submissions=1)
        self.assertEqual(
            RemediationUsage(attempts=2).exhausted_reason(budget),
            "budget_attempts_exhausted",
        )
        self.assertEqual(
            RemediationUsage(attempts=1, submissions=1).exhausted_reason(budget),
            "budget_submissions_exhausted",
        )
        self.assertIsNone(RemediationUsage(attempts=1).exhausted_reason(budget))

    def test_budget_payload_is_strict(self) -> None:
        with self.assertRaises(RemediationInvariantError):
            RemediationBudget(max_attempts=0)
        with self.assertRaises(RemediationInvariantError):
            RemediationBudget.from_payload({"max_attempts": True})

    def test_evaluation_outcomes_are_closed(self) -> None:
        self.assertEqual(
            {item.value for item in EvaluationOutcome},
            {
                "verified_success",
                "execution_success_unverified",
                "failed",
                "inconclusive",
            },
        )


if __name__ == "__main__":
    unittest.main()
