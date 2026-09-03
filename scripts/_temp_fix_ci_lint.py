from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, got {count}: {old!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/pilot107/adapters/slurm.py",
    "import hmac\nimport heapq\n",
    "import heapq\nimport hmac\n",
)
replace_once(
    "src/pilot107/adapters/slurm.py",
    'raw = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}".encode("utf-8")',
    'raw = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}".encode()',
)
replace_once(
    "src/pilot107/adapters/slurm.py",
    '''                if isinstance(state.get("binding"), dict) and state["binding"].get("path") == str(target):\n''',
    '''                if (\n                    isinstance(state.get("binding"), dict)\n                    and state["binding"].get("path") == str(target)\n                ):\n''',
)
replace_once(
    "src/pilot107/api/agent_tool_routes.py",
    '''    elif code in {"AGENT.TOOL.INVALID", "AGENT.TOOL.INVALID_RESULT"}:\n        status = 400\n    elif code == "AGENT.BUILDER.VALIDATIONS_INVALID":\n        status = 400\n''',
    '''    elif code in {\n        "AGENT.TOOL.INVALID",\n        "AGENT.TOOL.INVALID_RESULT",\n        "AGENT.BUILDER.VALIDATIONS_INVALID",\n    }:\n        status = 400\n''',
)
replace_once(
    "src/pilot107/core/run_service.py",
    '''                failure_reason="SlurmTransportError: submission transport failed without reconciliation",\n''',
    '''                failure_reason=(\n                    "SlurmTransportError: submission transport failed without reconciliation"\n                ),\n''',
)

print("CI lint cleanup applied")
