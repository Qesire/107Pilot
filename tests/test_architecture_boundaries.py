import ast
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _PROJECT_ROOT / "src" / "pilot107"
_DOCKER_ADAPTER = _SRC_ROOT / "adapters" / "slurm.py"


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_business_layers_do_not_call_docker_exec_directly(self) -> None:
        offenders: list[str] = []
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            if path == _DOCKER_ADAPTER:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _is_subprocess_run(node) and _contains_docker_exec(node):
                    offenders.append(str(path.relative_to(_PROJECT_ROOT)))

        self.assertEqual(offenders, [])

    def test_python_production_code_does_not_call_llm_chat_completions(self) -> None:
        offenders: list[str] = []
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "/chat/completions" in text or "PILOT107_LLM_API_KEY" in text:
                offenders.append(str(path.relative_to(_PROJECT_ROOT)))

        self.assertEqual(offenders, [])

    def test_agentd_has_no_cluster_or_workspace_mount_contract(self) -> None:
        dockerfile = (
            _PROJECT_ROOT / "services" / "pilot-agentd" / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertNotIn("openssh", dockerfile.lower())
        self.assertNotIn("slurm", dockerfile.lower())


def _is_subprocess_run(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _contains_docker_exec(node: ast.Call) -> bool:
    if not node.args:
        return False
    argv = node.args[0]
    if not isinstance(argv, ast.List):
        return False
    values = [item.value for item in argv.elts if isinstance(item, ast.Constant)]
    return "docker" in values and "exec" in values


if __name__ == "__main__":
    unittest.main()
