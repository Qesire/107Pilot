import unittest

from pilot107.core.resources import (
    REAL107_SIM_PARTITION_QOS,
    PreflightSeverity,
    QosResourceLimit,
    ResourcePlan,
    validate_resource_plan,
)


class ResourcePlanTests(unittest.TestCase):
    def test_derived_totals(self) -> None:
        plan = ResourcePlan(
            partition="P107-RTX5090",
            qos="normal",
            nodes=2,
            ntasks=4,
            cpus_per_task=8,
            gpus_per_node=2,
            time_limit="00:30:00",
        )

        self.assertEqual(plan.derived_cpu_upper_bound, 32)
        self.assertEqual(plan.derived_gpu_total, 4)

    def test_gpu_total_conflict_blocks(self) -> None:
        plan = ResourcePlan(
            partition="P107-RTX5090",
            qos="normal",
            nodes=2,
            ntasks=1,
            cpus_per_task=1,
            gpus_per_node=2,
            gpus_total=3,
            time_limit="00:30:00",
        )

        findings = validate_resource_plan(plan)

        self.assertTrue(any(f.code == "RESOURCE.GPU_TOTAL_CONFLICT" for f in findings))
        self.assertTrue(any(f.severity == PreflightSeverity.BLOCK for f in findings))

    def test_time_limit_required(self) -> None:
        plan = ResourcePlan(
            partition="P107-RTX5090",
            qos=None,
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
        )

        findings = validate_resource_plan(plan)

        self.assertTrue(any(f.code == "RESOURCE.TIME_LIMIT_REQUIRED" for f in findings))

    def test_real107_profile_requires_explicit_matching_qos(self) -> None:
        plan = ResourcePlan(
            partition="Students",
            qos=None,
            nodes=1,
            ntasks=1,
            cpus_per_task=4,
            gpus_total=1,
            gpu_type="A100",
            time_limit="00:05:00",
        )

        findings = validate_resource_plan(plan, partition_qos=REAL107_SIM_PARTITION_QOS)

        self.assertTrue(any(f.code == "RESOURCE.QOS_REQUIRED" for f in findings))

    def test_real107_profile_rejects_qos_not_allowed_by_partition(self) -> None:
        plan = ResourcePlan(
            partition="Students",
            qos="qos_p107-a100",
            nodes=1,
            ntasks=1,
            cpus_per_task=4,
            gpus_total=1,
            gpu_type="A100",
            time_limit="00:05:00",
        )

        findings = validate_resource_plan(plan, partition_qos=REAL107_SIM_PARTITION_QOS)

        self.assertTrue(any(f.code == "RESOURCE.QOS_NOT_ALLOWED" for f in findings))

    def test_real107_students_medium_2gpu_profile_is_valid(self) -> None:
        plan = ResourcePlan(
            partition="Students",
            qos="qos_stu_medium_2gpu",
            nodes=1,
            ntasks=1,
            cpus_per_task=4,
            gpus_total=1,
            gpu_type="A100",
            time_limit="00:05:00",
        )

        findings = validate_resource_plan(plan, partition_qos=REAL107_SIM_PARTITION_QOS)

        self.assertEqual([f.code for f in findings], [])

    def test_qos_limits_block_cpu_gpu_memory_and_walltime(self) -> None:
        plan = ResourcePlan(
            partition="Students",
            qos="qos_stu_default",
            nodes=1,
            ntasks=2,
            cpus_per_task=4,
            memory_value=32,
            memory_unit="G",
            gpus_total=2,
            time_limit="05:00:00",
        )

        findings = validate_resource_plan(
            plan,
            qos_limits={
                "qos_stu_default": QosResourceLimit(
                    max_cpus=4,
                    max_gpus=1,
                    max_memory_gb=16,
                    max_wall_hours=4,
                    source_authority="docs-main",
                )
            },
        )

        self.assertEqual(
            {finding.code for finding in findings},
            {
                "RESOURCE.QOS_CPU_LIMIT_EXCEEDED",
                "RESOURCE.QOS_GPU_LIMIT_EXCEEDED",
                "RESOURCE.QOS_MEMORY_LIMIT_EXCEEDED",
                "RESOURCE.QOS_WALLTIME_LIMIT_EXCEEDED",
            },
        )
        self.assertTrue(all(f.severity == PreflightSeverity.BLOCK for f in findings))

    def test_qos_limits_unknown_profile_warns(self) -> None:
        plan = ResourcePlan(
            partition="Students",
            qos="qos_stu_medium",
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
            time_limit="00:05:00",
        )

        findings = validate_resource_plan(plan, qos_limits={})

        self.assertEqual([finding.code for finding in findings], ["RESOURCE.QOS_LIMITS_UNKNOWN"])
        self.assertEqual(findings[0].severity, PreflightSeverity.WARN)


if __name__ == "__main__":
    unittest.main()
