import unittest

from pilot107.core.states import RunState, normalize_slurm_state


class SlurmStateTests(unittest.TestCase):
    def test_normalize_scalar_completed(self) -> None:
        state, flags = normalize_slurm_state("COMPLETED")
        self.assertEqual(state, RunState.SUCCEEDED)
        self.assertEqual(flags, ["COMPLETED"])

    def test_normalize_list_preserves_flags(self) -> None:
        state, flags = normalize_slurm_state(["COMPLETED", "SPECIAL_EXIT"])
        self.assertEqual(state, RunState.SUCCEEDED)
        self.assertEqual(flags, ["COMPLETED", "SPECIAL_EXIT"])

    def test_unknown_state_is_not_forced(self) -> None:
        state, flags = normalize_slurm_state(["SOMETHING_NEW"])
        self.assertEqual(state, RunState.UNKNOWN)
        self.assertEqual(flags, ["SOMETHING_NEW"])

    def test_normalize_cancelled_actor_suffix_preserves_raw_state(self) -> None:
        state, flags = normalize_slurm_state("CANCELLED by 11001")

        self.assertEqual(state, RunState.CANCELLED)
        self.assertEqual(flags, ["CANCELLED BY 11001"])


if __name__ == "__main__":
    unittest.main()
