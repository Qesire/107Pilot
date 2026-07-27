import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config/platform_profiles/simulator-real107-behavior.yaml"


def _load_profile() -> dict:
    lines: list[tuple[int, str]] = []
    for raw_line in PROFILE_PATH.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append((indent, raw_line.strip()))

    def parse_scalar(value: str):
        value = value.strip()
        if value == "null":
            return None
        if value == "true":
            return True
        if value == "false":
            return False
        if value.startswith("["):
            return ast.literal_eval(value)
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            return ast.literal_eval(value)
        try:
            return int(value)
        except ValueError:
            return value

    def parse_block(index: int, indent: int):
        if index >= len(lines) or lines[index][0] < indent:
            return {}, index
        if lines[index][0] == indent and lines[index][1].startswith("- "):
            return parse_list(index, indent)
        return parse_dict(index, indent)

    def parse_list(index: int, indent: int):
        items = []
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            if not text.startswith("- "):
                break
            content = text[2:]
            if not content:
                item, index = parse_block(index + 1, indent + 2)
                items.append(item)
                continue
            if ":" in content:
                key, value = content.split(":", 1)
                item = {}
                if value.strip():
                    item[key] = parse_scalar(value)
                    index += 1
                else:
                    item[key], index = parse_block(index + 1, indent + 2)
                while index < len(lines) and lines[index][0] > indent:
                    child, index = parse_block(index, indent + 2)
                    if isinstance(child, dict):
                        item.update(child)
                    else:
                        raise AssertionError(f"unexpected nested list under {key}")
                items.append(item)
                continue
            items.append(parse_scalar(content))
            index += 1
        return items, index

    def parse_dict(index: int, indent: int):
        data = {}
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            if text.startswith("- "):
                break
            key, value = text.split(":", 1)
            if value.strip():
                data[key] = parse_scalar(value)
                index += 1
            else:
                data[key], index = parse_block(index + 1, indent + 2)
        return data, index

    profile, end = parse_block(0, 0)
    if end != len(lines):
        raise AssertionError(f"profile parser stopped at {end} of {len(lines)}")
    return profile


def _items_by_name(items: list[dict]) -> dict[str, dict]:
    return {str(item["name"]): item for item in items}


def _slurm_partition_fields() -> dict[str, dict[str, str]]:
    slurm_conf = (ROOT / "simulator/compose/slurm/slurm.conf").read_text(encoding="utf-8")
    partitions = {}
    for line in slurm_conf.splitlines():
        if not line.startswith("PartitionName="):
            continue
        fields = {}
        for token in line.split():
            key, value = token.split("=", 1)
            fields[key] = value
        partitions[fields["PartitionName"]] = fields
    return partitions


class SimulatorProfileConfigTests(unittest.TestCase):
    def test_profile_declares_behavior_contract(self) -> None:
        profile = _load_profile()

        self.assertEqual(profile["schema"], "pilot107.simulator_real107_behavior.v1")
        self.assertEqual(profile["slurm"]["target_version"], "25.11.2")
        self.assertEqual(profile["slurm"]["fallback_version"], "23.11.x")
        self.assertEqual(
            profile["slurm"]["accounting_storage_enforce"],
            ["associations", "qos", "limits"],
        )
        self.assertIn("/public", [item["path"] for item in profile["storage"]["shared_paths"]])
        self.assertIn("/tmp", [item["path"] for item in profile["storage"]["local_paths"]])
        self.assertTrue(profile["runtime_limitations"])
        self.assertIn(
            "REST API and JWT behavior are validated on the source-built Slurm "
            "25.11.2 target image.",
            profile["runtime_limitations"],
        )
        self.assertIn(
            "The retained Ubuntu 23.11 fallback must not be used for final parity claims.",
            profile["runtime_limitations"],
        )
        self.assertEqual(
            profile["behavior_matrix"]["limited_user_unauthorized_qos"]["expected"],
            "rejected",
        )

    def test_env_defaults_use_25_11_target(self) -> None:
        # .env.competition is a generated, gitignored file (created from
        # .env.competition.example by start-competition.sh). Audit the two
        # committed templates that carry the defaults.
        for relative in (
            "simulator/compose/.env.example",
            "simulator/compose/.env.competition.example",
        ):
            env_text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("SLURM_SIM_IMAGE=pilot107/slurm-sim:25.11-real107", env_text)
            self.assertIn("PILOT107_SLURM_API_VERSION=v0.0.41", env_text)
            self.assertNotIn("PILOT107_SLURM_API_VERSION=v0.0.40", env_text)

    def test_slurm_conf_matches_profile_partitions(self) -> None:
        profile = _load_profile()
        slurm_conf = (ROOT / "simulator/compose/slurm/slurm.conf").read_text(encoding="utf-8")
        partitions = _slurm_partition_fields()

        enforce = ",".join(profile["slurm"]["accounting_storage_enforce"])
        self.assertIn(f"AccountingStorageEnforce={enforce}", slurm_conf)
        self.assertIn("AccountingStorageTRES=gres/gpu", slurm_conf)
        self.assertIn("SelectTypeParameters=CR_CPU,CR_CORE_DEFAULT_DIST_BLOCK", slurm_conf)
        self.assertIn("SchedulerType=sched/backfill", slurm_conf)
        self.assertIn("SchedulerParameters=enable_user_top", slurm_conf)
        self.assertIn("PriorityType=priority/multifactor", slurm_conf)
        self.assertIn("SlurmdParameters=config_overrides", slurm_conf)

        for partition in profile["partitions"]:
            fields = partitions[partition["name"]]
            self.assertEqual(fields["Nodes"], partition["nodes"])
            self.assertEqual(fields["MaxTime"], partition["max_time"])
            self.assertEqual(fields["Default"], "YES" if partition["default"] else "NO")
            self.assertEqual(fields["AllowAccounts"].split(","), partition["allow_accounts"])
            self.assertEqual(fields["AllowQos"].split(","), partition["allow_qos"])

        students = partitions["Students"]
        profile_students = _items_by_name(profile["partitions"])["Students"]
        self.assertNotIn("MaxTime=01:00:00", " ".join(students.values()))
        self.assertEqual(
            students["DefMemPerCPU"],
            str(profile_students["default_memory_per_cpu_mb"]),
        )

    def test_worker_spool_is_not_shared_between_simulated_nodes(self) -> None:
        compose = (ROOT / "simulator/compose/compose.yml").read_text(encoding="utf-8")
        competition = (ROOT / "simulator/compose/compose.competition.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("slurm-spool:/var/spool/slurm", compose)
        self.assertNotIn("slurm-spool:", compose)
        self.assertNotIn("slurm-spool:/var/spool/slurm", competition)
        self.assertNotIn("/sys/fs/cgroup:/sys/fs/cgroup", compose)
        self.assertNotIn("cgroup: host", compose)
        self.assertNotIn("pilot107/slurm-sim:local", compose)
        self.assertNotIn("pilot107/slurm-sim:local", competition)
        self.assertIn("pilot107/slurm-sim:25.11-real107", compose)
        self.assertIn("pilot107/slurm-sim:25.11-real107", competition)
        self.assertIn("./slurm/cgroup.conf:/etc/slurm/cgroup.conf:ro", compose)
        self.assertIn("./slurm/cgroup.conf:/etc/slurm/cgroup.conf:ro", competition)
        self.assertIn('command: ["slurmctld", "-D", "-i", "-vvv"]', compose)
        self.assertNotIn("SLURMRESTD_SECURITY", compose)
        self.assertIn(
            'command: ["gosu", "pilot107", "slurmrestd", "-a", "rest_auth/jwt", '
            '"0.0.0.0:6820"]',
            compose,
        )
        self.assertIn(
            "exec gosu pilot107",
            (ROOT / "simulator/images/slurm/docker-entrypoint.sh").read_text(
                encoding="utf-8"
            ),
        )
        self.assertNotIn(
            "/sys/fs/cgroup/system.slice",
            (ROOT / "simulator/images/slurm/docker-entrypoint.sh").read_text(
                encoding="utf-8"
            ),
        )

    def test_gres_conf_matches_profile_gpu_nodes(self) -> None:
        profile = _load_profile()
        gres_conf = (ROOT / "simulator/compose/slurm/gres.conf").read_text(encoding="utf-8")

        for node in profile["nodes"]:
            for gres in node.get("gres", []):
                expected = (
                    f"NodeName={node['name']} Name={gres['name']} Type={gres['type']}"
                )
                self.assertIn(expected, gres_conf)
                self.assertIn(f"[0-{gres['count'] - 1}]", gres_conf)

    def test_apply_profile_mirrors_profile_accounts_qos_and_users(self) -> None:
        profile = _load_profile()
        script = (ROOT / "scripts/apply-sim-real107-profile.sh").read_text(encoding="utf-8")

        self.assertIn(str(PROFILE_PATH.relative_to(ROOT)), script)
        self.assertIn('cluster_name="pilot107-sim"', script)
        self.assertIn('run_sacctmgr add cluster "$cluster_name"', script)
        self.assertIn("AccountingStorageEnforce=associations,qos,limits", (
            ROOT / "simulator/compose/slurm/slurm.conf"
        ).read_text(encoding="utf-8"))

        for account in profile["accounts"]:
            self.assertIn(f"run_sacctmgr add account {account['name']}", script)
            self.assertIn('Cluster="$cluster_name"', script)
        for qos in _items_by_name(profile["qos"]):
            self.assertIn(qos, script)
        self.assertIn('require_qos "$qos"', script)

        users = _items_by_name(profile["users"])
        for username in ("alice", "bob"):
            user = users[username]
            qos_csv = ",".join(user["submit_qos"])
            self.assertIn(f"run_sacctmgr add user {username} Account={user['account']}", script)
            self.assertIn(
                f"modify user where user={username} account={user['account']} "
                f'cluster="$cluster_name" set QOS={qos_csv}',
                script,
            )
            self.assertIn(
                f"modify user where user={username} account={user['account']} "
                f'cluster="$cluster_name" set DefaultQOS={user["default_qos"]}',
                script,
            )

        self.assertIn("require_assoc_has_qos alice qos_stu_medium_2gpu", script)
        self.assertIn("require_assoc_excludes_qos bob qos_stu_medium_2gpu", script)

    def test_real107_profile_smoke_exercises_profile_behavior_matrix(self) -> None:
        profile = _load_profile()
        script = (ROOT / "scripts/smoke-sim-real107-profile.sh").read_text(encoding="utf-8")

        for case in profile["behavior_matrix"].values():
            self.assertIn(case["user"], script)
            self.assertIn(case["partition"], script)
            self.assertIn(case["qos"], script)
        self.assertIn("expect_submit_rejected", script)
        self.assertIn("submit_and_expect_completed", script)
        self.assertIn("bob limited association unexpected", script)

    def test_compose_readme_points_to_profile_and_scripts(self) -> None:
        readme = (ROOT / "simulator/compose/README.md").read_text(encoding="utf-8")

        self.assertIn("../../config/platform_profiles/simulator-real107-behavior.yaml", readme)
        self.assertIn("../../scripts/apply-sim-real107-profile.sh", readme)
        self.assertIn("../../scripts/report-sim-behavior-fidelity.sh", readme)
        self.assertTrue((ROOT / "scripts/apply-sim-real107-profile.sh").is_file())
        self.assertTrue((ROOT / "scripts/smoke-sim-real107-profile.sh").is_file())
        self.assertTrue((ROOT / "scripts/report-sim-behavior-fidelity.sh").is_file())

    def test_slurm_image_manifest_keeps_25_11_entrypoint(self) -> None:
        profile = _load_profile()
        manifest_path = ROOT / "simulator/images/slurm/version-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_25 = json.loads(
            (ROOT / "simulator/images/slurm/version-manifest.25.11.json").read_text(
                encoding="utf-8"
            )
        )
        dockerfile = (ROOT / "simulator/images/slurm/Dockerfile").read_text(encoding="utf-8")
        dockerfile_25 = (ROOT / "simulator/images/slurm/Dockerfile.25.11").read_text(
            encoding="utf-8"
        )
        check_script = (ROOT / "scripts/check-slurm-sim-image.sh").read_text(encoding="utf-8")
        build_25_script = (ROOT / "scripts/build-slurm-sim-25-image.sh").read_text(
            encoding="utf-8"
        )
        check_25_script = (ROOT / "scripts/check-slurm-sim-25-image.sh").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            manifest["target"]["slurm_version"],
            profile["slurm"]["target_version"],
        )
        self.assertEqual(
            manifest["fallback"]["slurm_version"],
            profile["slurm"]["fallback_version"],
        )
        self.assertIn("slurm-sim-version-manifest.json", dockerfile)
        self.assertIn("slurm-sim-version-manifest.json", dockerfile_25)
        self.assertIn("slurm-sim-version-manifest.json", check_script)
        self.assertIn("ARG SLURM_VERSION=25.11.2", dockerfile_25)
        self.assertIn('slurmctld -V | grep -F "$SLURM_VERSION"', dockerfile_25)
        self.assertIn(
            "https://download.schedmd.com/slurm/slurm-${SLURM_VERSION}.tar.bz2",
            dockerfile_25,
        )
        self.assertIn("pilot107/slurm-sim:25.11-real107", build_25_script)
        self.assertIn("slurmctld -V", check_25_script)
        self.assertIn('grep -F "25.11.2"', check_25_script)
        self.assertEqual(manifest["runtime_fidelity"]["real_gpu_devices"], "unavailable")
        self.assertEqual(manifest_25["target"]["status"], "current")


if __name__ == "__main__":
    unittest.main()
