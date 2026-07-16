import unittest

from pilot107.adapters.platform_parsers import (
    normalize_slurm_node_state,
    parse_scontrol_show_job,
    parse_scontrol_show_nodes,
    parse_scontrol_show_part,
    parse_sinfo_pipe,
    parse_squeue_pipe,
)
from pilot107.core.platform_snapshot import NormalizedNodeState, ObservationSourceType


class PlatformParserTests(unittest.TestCase):
    def test_parse_scontrol_show_part_preserves_official_fields(self) -> None:
        text = """
PartitionName=Students AllowAccounts=student AllowQos=qos_stu_default,qos_stu_medium_2gpu
   AllocNodes=ALL Default=YES MaxTime=12:00:00 State=UP Nodes=anode[05-17]
   TotalCPUs=1664 TotalNodes=13 TRES=cpu=1664,mem=7500G,node=13,billing=1664,gres/gpu=104

PartitionName=GPU-A100 AllowAccounts=ALL AllowQos=qos_gpu-a100
   Default=NO MaxTime=01:00:00 State=DOWN Nodes=anode[16-17]
"""
        partitions = parse_scontrol_show_part(
            text,
            raw_artifact="raw/scontrol-show-part.txt",
            captured_at="2026-07-15T00:00:00+00:00",
        )
        by_name = {partition.name: partition for partition in partitions}

        students = by_name["Students"]
        self.assertEqual(students.allow_accounts, ("student",))
        self.assertEqual(students.allow_qos, ("qos_stu_default", "qos_stu_medium_2gpu"))
        self.assertEqual(students.nodes, "anode[05-17]")
        self.assertEqual(students.max_time, "12:00:00")
        self.assertTrue(students.default)
        self.assertEqual(students.tres["gres/gpu"], "104")
        self.assertEqual(students.total_nodes, 13)
        self.assertEqual(students.captured_at, "2026-07-15T00:00:00+00:00")
        self.assertEqual(students.source_type, ObservationSourceType.CLI)
        self.assertEqual(students.raw_artifact, "raw/scontrol-show-part.txt")
        self.assertEqual(students.state_normalized, NormalizedNodeState.UNKNOWN)

        self.assertEqual(by_name["GPU-A100"].state_normalized, NormalizedNodeState.DOWN)

    def test_parse_scontrol_show_nodes_extracts_gres_and_reason(self) -> None:
        text = """
NodeName=anode16 Arch=x86_64 CoresPerSocket=16
   CPUAlloc=2 CPUTot=32 RealMemory=8192 Gres=gpu:A100:2
   Partitions=Students,GPU-A100 State=MIXED Reason=None
"""
        nodes = parse_scontrol_show_nodes(text)

        self.assertEqual(nodes[0].node_name, "anode16")
        self.assertEqual(nodes[0].partitions, ("Students", "GPU-A100"))
        self.assertEqual(nodes[0].cpus_allocated, 2)
        self.assertEqual(nodes[0].cpus_total, 32)
        self.assertEqual(nodes[0].memory_mb, 8192)
        self.assertEqual(nodes[0].gres["gpu:A100"], "2")
        self.assertEqual(nodes[0].state_normalized, NormalizedNodeState.MIXED)

    def test_parse_sinfo_pipe_normalizes_states(self) -> None:
        nodes = parse_sinfo_pipe(
            "anode16|Students*,GPU-A100|mix|32|8192|gpu:A100:2|\n"
            "anode17|Students|drng|32|8192|gpu:A100:2|maintenance\n"
        )

        self.assertEqual(nodes[0].partitions, ("Students", "GPU-A100"))
        self.assertEqual(nodes[0].state_normalized, NormalizedNodeState.MIXED)
        self.assertEqual(nodes[1].state_normalized, NormalizedNodeState.DRAINING)
        self.assertEqual(nodes[1].reason, "maintenance")

    def test_parse_squeue_pipe_preserves_pending_reason_and_tres(self) -> None:
        jobs = parse_squeue_pipe(
            "21039|PENDING|QOSMaxCpuPerUserLimit|Students|train|cpu=4,mem=8G,gres/gpu=1\n"
        )

        self.assertEqual(jobs[0].job_id, "21039")
        self.assertEqual(jobs[0].state_raw, "PENDING")
        self.assertEqual(jobs[0].pending_reason, "QOSMaxCpuPerUserLimit")
        self.assertEqual(jobs[0].partition, "Students")
        self.assertEqual(jobs[0].name, "train")
        self.assertEqual(jobs[0].tres["gres/gpu"], "1")

    def test_parse_scontrol_show_job_preserves_state_reason_and_tres(self) -> None:
        text = """
JobId=21039 JobName=train
   JobState=PENDING Reason=QOSMaxWallDurationPerJobLimit Partition=Students QOS=qos_stu_default
   ReqTRES=cpu=4,mem=8G,node=1,billing=4,gres/gpu=1 AllocTRES=(null)
"""

        jobs = parse_scontrol_show_job(
            text,
            raw_artifact="raw/scontrol-show-job-21039.txt",
            captured_at="2026-07-15T00:00:00+00:00",
        )

        self.assertEqual(jobs[0].job_id, "21039")
        self.assertEqual(jobs[0].state_raw, "PENDING")
        self.assertEqual(jobs[0].reason, "QOSMaxWallDurationPerJobLimit")
        self.assertEqual(jobs[0].partition, "Students")
        self.assertEqual(jobs[0].qos, "qos_stu_default")
        self.assertEqual(jobs[0].req_tres["gres/gpu"], "1")
        self.assertEqual(jobs[0].alloc_tres, {})
        self.assertEqual(jobs[0].raw_artifact, "raw/scontrol-show-job-21039.txt")

    def test_normalize_slurm_node_state(self) -> None:
        self.assertEqual(normalize_slurm_node_state("idle"), NormalizedNodeState.IDLE)
        self.assertEqual(normalize_slurm_node_state("allocated"), NormalizedNodeState.ALLOCATED)
        self.assertEqual(normalize_slurm_node_state("comp"), NormalizedNodeState.COMPLETING)
        self.assertEqual(normalize_slurm_node_state("down*"), NormalizedNodeState.DOWN)
        self.assertEqual(normalize_slurm_node_state("unknown"), NormalizedNodeState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
