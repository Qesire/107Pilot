import unittest

from pilot107.core.platform import (
    SourceAuthority,
    capability_profile_from_real107_probe,
    capability_profile_from_simulator_behavior,
    docker_sim_capability_profile,
    docker_sim_configuration_snapshot,
)


class PlatformProfileTests(unittest.TestCase):
    def test_docker_sim_configuration_snapshot_contains_competition_baseline(self) -> None:
        snapshot = docker_sim_configuration_snapshot(captured_at="2026-07-12T00:00:00+00:00")
        payload = snapshot.to_payload()

        self.assertEqual(payload["cluster"]["api_version"], "v0.0.41")
        self.assertEqual(
            payload["cluster"]["source_authority"],
            SourceAuthority.SIMULATOR_PROBE.value,
        )
        self.assertIn("/public", payload["cluster"]["shared_roots"])
        self.assertIn("/home", payload["cluster"]["shared_roots"])
        self.assertIn("Students", payload["cluster"]["partitions"])
        self.assertIn("qos_stu_medium_2gpu", payload["cluster"]["qos"])
        self.assertEqual(payload["auth_strategy"], "trusted_header_simulated_users")
        self.assertEqual(payload["captured_at"], "2026-07-12T00:00:00+00:00")

    def test_user_entitlement_profiles_are_separated_by_owner(self) -> None:
        snapshot = docker_sim_configuration_snapshot()
        users = {user.username: user for user in snapshot.users}

        self.assertEqual(users["alice"].allowed_roots, ("/public/home/alice",))
        self.assertEqual(users["bob"].allowed_roots, ("/public/home/bob",))
        self.assertEqual(users["alice"].default_partition, "Students")
        self.assertEqual(users["alice"].default_qos, "qos_stu_medium_2gpu")
        self.assertNotEqual(users["alice"].allowed_roots, users["bob"].allowed_roots)

    def test_docker_capability_profile_contains_docs_main_qos_limits(self) -> None:
        profile = docker_sim_capability_profile(captured_at="2026-07-12T00:00:00+00:00")
        payload = profile.to_payload()

        qos = {item["name"]: item for item in payload["qos"]}
        self.assertEqual(payload["default_partition"], "Students")
        self.assertEqual(payload["default_qos"], "qos_stu_medium_2gpu")
        self.assertEqual(qos["qos_stu_default"]["max_cpus"], 4)
        self.assertEqual(qos["qos_stu_medium_2gpu"]["max_gpus"], 2)
        self.assertEqual(qos["qos_stu_medium_2gpu"]["max_wall_hours"], 24)
        self.assertEqual(qos["qos_stu_large"]["max_gpus"], 4)
        self.assertTrue(payload["rest"]["partial_payload_with_errors"])
        self.assertIn("Students", profile.partition_qos())

    def test_simulator_behavior_profile_is_loaded_from_declarative_source(self) -> None:
        profile = capability_profile_from_simulator_behavior(
            {
                "schema": "pilot107.simulator_real107_behavior.v1",
                "profile_id": "fixture",
                "slurm": {"api_version": "v0.0.41", "auth": {}},
                "users": [
                    {
                        "name": "alice",
                        "default_partition": "Students",
                        "default_qos": "qos_stu_default",
                    }
                ],
                "nodes": [{"name": "anode16"}],
                "partitions": [
                    {"name": "Students", "nodes": "anode16", "allow_qos": ["qos_stu_default"]}
                ],
                "qos": [
                    {
                        "name": "qos_stu_default",
                        "max_cpus": 4,
                        "max_gpus": 1,
                        "max_memory": "16G",
                        "max_wall": "04:00:00",
                    }
                ],
                "storage": {
                    "shared_paths": [{"path": "/public", "semantics": "shared"}],
                    "local_paths": [{"path": "/tmp"}],
                },
            }
        )

        self.assertEqual(profile.default_partition, "Students")
        self.assertEqual(profile.default_qos, "qos_stu_default")
        self.assertEqual(profile.partitions[0].total_nodes, 1)
        self.assertEqual(profile.qos[0].max_memory_gb, 16)
        self.assertEqual(profile.qos[0].max_wall_hours, 4)

    def test_real107_probe_ingest_preserves_partial_partition_payload(self) -> None:
        profile = capability_profile_from_real107_probe(
            configuration_snapshot={
                "captured_at": "2026-07-12T00:48:34+00:00",
                "auth_strategy": "single_user_jwt_bearer",
                "openapi_digest": "digest",
                "cluster": {
                    "api_version": "v0.0.41",
                    "shared_roots": ["/public"],
                    "local_roots": ["/tmp"],
                },
                "endpoints": {"slurm_rest_url": "http://107.ustc.edu.cn:6820"},
                "users": [{"default_partition": "CPU-6530", "default_qos": None}],
            },
            probe_report={
                "probes": [
                    {
                        "name": "partitions",
                        "http_status": 500,
                        "payload_summary": {
                            "errors": [{"description": "Slurmdb query failed"}],
                            "partitions": [
                                {
                                    "name": "Students",
                                    "nodes": {"configured": "anode[05-17]", "total": 13},
                                    "partition": {"state": ["UP"]},
                                    "qos": {"allowed": "qos_stu_default,qos_stu_medium_2gpu"},
                                }
                            ],
                        },
                    }
                ]
            },
        )

        self.assertTrue(profile.rest.partial_payload_with_errors)
        self.assertEqual(profile.partitions[0].name, "Students")
        self.assertEqual(
            profile.partitions[0].allow_qos,
            ("qos_stu_default", "qos_stu_medium_2gpu"),
        )


if __name__ == "__main__":
    unittest.main()
