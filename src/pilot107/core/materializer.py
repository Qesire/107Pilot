"""ContractV2 to executable Slurm script materialization."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Protocol

from jinja2 import StrictUndefined, meta
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from pilot107.core.contract_v2 import normalize_contract
from pilot107.core.resources import PreflightFinding, PreflightSeverity

SUPPORTED_MATERIALIZERS = frozenset({"generic_command", "sbatch_template_v1"})

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_TEMPLATE_SCALAR = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:TOKEN|API_KEY|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY)(?:$|_)",
    re.IGNORECASE,
)


class RecipeMaterializationSpec(Protocol):
    @property
    def recipe_id(self) -> str: ...

    @property
    def recipe_version_id(self) -> str: ...

    @property
    def materializer(self) -> str: ...

    @property
    def sbatch_template(self) -> str | None: ...


@dataclass(frozen=True)
class MaterializationResult:
    script: str | None
    findings: tuple[PreflightFinding, ...]
    materializer: str


def materialize_contract(
    payload: dict[str, Any],
    recipe: RecipeMaterializationSpec,
) -> MaterializationResult:
    canonical = normalize_contract(payload)
    findings = [*_runtime_findings(canonical), *_materializer_findings(recipe)]
    if any(item.severity == PreflightSeverity.BLOCK for item in findings):
        return MaterializationResult(None, tuple(findings), recipe.materializer)

    runtime_lines = _runtime_lines(canonical)
    script: str | None
    if recipe.materializer == "generic_command":
        script = _generic_script(canonical, runtime_lines)
    else:
        script, template_findings = _template_script(canonical, recipe, runtime_lines)
        findings.extend(template_findings)
    if any(item.severity == PreflightSeverity.BLOCK for item in findings):
        script = None
    return MaterializationResult(script, tuple(findings), recipe.materializer)


def _generic_script(payload: dict[str, Any], runtime_lines: list[str]) -> str:
    workdir = str(payload["project"]["workdir"])
    command = str(payload["entry"]["command"]).strip()
    return "\n".join(
        [
            "#!/bin/bash",
            "set -Eeuo pipefail",
            f"# pilot107 contract-schema: {payload['schema_version']}",
            f"# pilot107 recipe: {payload['recipe_version_id']}",
            *runtime_lines,
            f"cd {shlex.quote(workdir)}",
            command,
            "",
        ]
    )


def _template_script(
    payload: dict[str, Any],
    recipe: RecipeMaterializationSpec,
    runtime_lines: list[str],
) -> tuple[str | None, list[PreflightFinding]]:
    assert recipe.sbatch_template is not None
    environment = payload["runtime"]["environment"]
    resources = payload["resources"]
    array = resources.get("array") or {}
    context: dict[str, Any] = {
        "job_name": _job_name(payload, recipe.recipe_id),
        "account": environment.get("SLURM_ACCOUNT"),
        "partition": resources.get("partition"),
        "qos": resources.get("qos"),
        "nodes": resources.get("nodes", 1),
        "ntasks": resources.get("ntasks", 1),
        "cpus_per_task": resources.get("cpus_per_task", 1),
        "memory": resources.get("memory"),
        "time_limit": resources.get("time_limit"),
        "workdir": payload["project"].get("workdir"),
        "entry_command": payload["entry"].get("command"),
        "kit_root": environment.get("KIT_ROOT"),
        "data_root": environment.get("DATA_ROOT"),
        "gpus_per_task": resources.get("gpus_per_node") or resources.get("gpus_total"),
        "gpu_type": resources.get("gpu_type"),
        "array_expression": array.get("expression"),
        "array_max_concurrency": array.get("max_concurrency") or 1,
    }
    jinja = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    try:
        parsed = jinja.parse(recipe.sbatch_template)
        required = meta.find_undeclared_variables(parsed)
    except TemplateError as exc:
        return None, [_block("MATERIALIZER.TEMPLATE_ERROR", str(exc))]
    findings: list[PreflightFinding] = []
    for name in sorted(required):
        value = context.get(name)
        if value is None or value == "":
            findings.append(
                _block(
                    "MATERIALIZER.VALUE_REQUIRED",
                    f"template value is required: {name}",
                )
            )
        elif name != "entry_command" and not _safe_template_scalar(value):
            findings.append(
                _block(
                    "MATERIALIZER.UNSAFE_TEMPLATE_VALUE",
                    f"template value contains unsupported characters: {name}",
                )
            )
    if findings:
        return None, findings
    try:
        rendered = jinja.from_string(recipe.sbatch_template).render(context)
    except TemplateError as exc:
        return None, [_block("MATERIALIZER.TEMPLATE_ERROR", str(exc))]
    return _inject_runtime_lines(rendered, runtime_lines), []


def _runtime_findings(payload: dict[str, Any]) -> list[PreflightFinding]:
    runtime = payload["runtime"]
    findings: list[PreflightFinding] = []
    if runtime.get("container_image"):
        findings.append(
            _block(
                "MATERIALIZER.CONTAINER_CAPABILITY_REQUIRED",
                "runtime.container_image requires a platform-specific OCI capability profile",
            )
        )
    conda_env = runtime.get("conda_env")
    if conda_env and ("\n" in conda_env or "\x00" in conda_env):
        findings.append(
            _block("MATERIALIZER.CONDA_ENV_UNSAFE", "runtime.conda_env contains unsafe bytes")
        )
    for module in runtime.get("modules", []):
        if not module or "\n" in module or "\x00" in module:
            findings.append(
                _block("MATERIALIZER.MODULE_UNSAFE", "runtime.modules contains an unsafe value")
            )
    for name, value in runtime.get("environment", {}).items():
        if not _ENV_NAME.fullmatch(name):
            findings.append(
                _block("MATERIALIZER.ENV_NAME_UNSAFE", f"invalid environment name: {name}")
            )
        if "\x00" in value:
            findings.append(
                _block("MATERIALIZER.ENV_VALUE_UNSAFE", f"environment value contains NUL: {name}")
            )
        if _SENSITIVE_ENV_NAME.search(name):
            findings.append(
                _block(
                    "MATERIALIZER.SECRET_LITERAL_FORBIDDEN",
                    f"literal secret-like environment value is forbidden: {name}",
                )
            )
    return findings


def _materializer_findings(
    recipe: RecipeMaterializationSpec,
) -> list[PreflightFinding]:
    if recipe.materializer not in SUPPORTED_MATERIALIZERS:
        return [
            _block(
                "RECIPE.MATERIALIZER_UNAVAILABLE",
                f"unsupported recipe materializer: {recipe.materializer}",
            )
        ]
    if recipe.materializer == "sbatch_template_v1" and not recipe.sbatch_template:
        return [_block("MATERIALIZER.TEMPLATE_MISSING", "recipe sbatch_template is missing")]
    return []


def _runtime_lines(payload: dict[str, Any]) -> list[str]:
    runtime = payload["runtime"]
    lines: list[str] = []
    for module in runtime.get("modules", []):
        lines.append(f"module load {shlex.quote(module)}")
    for name, value in sorted(runtime.get("environment", {}).items()):
        lines.append(f"export {name}={shlex.quote(value)}")
    conda_env = runtime.get("conda_env")
    if conda_env:
        lines.extend(
            [
                "command -v conda >/dev/null 2>&1 || { echo 'conda not found' >&2; exit 127; }",
                'eval "$(conda shell.bash hook)"',
                f"conda activate {shlex.quote(conda_env)}",
            ]
        )
    return lines


def _inject_runtime_lines(script: str, runtime_lines: list[str]) -> str:
    if not runtime_lines:
        return script if script.endswith("\n") else script + "\n"
    lines = script.splitlines()
    index = next(
        (offset + 1 for offset, line in enumerate(lines) if line.startswith("set -")),
        1,
    )
    lines[index:index] = ["", *runtime_lines, ""]
    return "\n".join(lines) + "\n"


def _job_name(payload: dict[str, Any], recipe_id: str) -> str:
    raw = str(payload["project"].get("name") or recipe_id)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-")
    return (slug or "pilot107-run")[:128]


def _safe_template_scalar(value: object) -> bool:
    return bool(_SAFE_TEMPLATE_SCALAR.fullmatch(str(value)))


def _block(code: str, message: str) -> PreflightFinding:
    return PreflightFinding(
        severity=PreflightSeverity.BLOCK,
        code=code,
        message=message,
        source_authority="contract_materializer",
    )
