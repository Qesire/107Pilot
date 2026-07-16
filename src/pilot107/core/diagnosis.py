"""Rule-based diagnosis for failed or degraded runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from pilot107.core.run_store import DiagnosisRecord, EvidenceObjectRecord, RunRecord, RunStore

DIAGNOSIS_SNIPPET_PATHS = (
    "submission/submit.stderr",
    "submission/submit.stderr.txt",
    "logs/stderr.tail.txt",
    "logs/stderr.tail.json",
    "slurm/runtime_status.json",
    "slurm/job_detail.json",
    "slurm/accounting.json",
    "environment/summary.json",
    "run/environment/gpu.json",
    "outputs/inventory.json",
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_KNOWN_ERRORS_DIR = _PROJECT_ROOT / "data" / "known_errors"

_LEGACY_SUMMARIES = {
    "SLURM.INVALID_QOS": "提交使用了当前分区或账号不允许的 QoS。",
    "SLURM.INVALID_PARTITION": "提交使用了不存在或当前账号无权访问的分区。",
    "RUNTIME.COMMAND_NOT_FOUND": "运行环境中找不到脚本调用的命令或路径。",
    "RUNTIME.PYTHON_PACKAGE_MISSING": "Python 运行环境缺少作业需要的包。",
    "RUNTIME.TIMEOUT": "作业达到 walltime 限制后被 Slurm 终止。",
    "RUNTIME.OOM": "作业疑似因内存不足被终止。",
}


@dataclass(frozen=True)
class KnownErrorRule:
    error_id: str
    category: str
    severity: str
    retryable: bool
    stage: str
    title: str
    symptoms: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    root_cause: str = ""
    fix_template: dict[str, Any] = field(default_factory=dict)
    fix_guide: dict[str, str] = field(default_factory=dict)
    confidence: str = "medium"
    kb_article: str | None = None
    terminal_state_match: str | None = None
    state_match: dict[str, Any] = field(default_factory=dict)

    @property
    def suggested_patch(self) -> dict[str, Any]:
        patch = self.fix_template.get("patch", {})
        return dict(patch) if isinstance(patch, dict) else {}


@dataclass(frozen=True)
class DiagnosisDraft:
    run_id: str
    rule_id: str
    severity: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    suggested_patch: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    confidence: str = "medium"
    category: str | None = None
    stage: str | None = None
    fix_guide: dict[str, str] = field(default_factory=dict)

    @property
    def diagnosis_id(self) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", self.rule_id).strip("_").lower()
        return f"diag_{self.run_id}_{slug}"

    def to_record_payload(self) -> dict[str, Any]:
        return {
            "diagnosis_id": self.diagnosis_id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "suggested_patch": self.suggested_patch,
            "retryable": self.retryable,
            "confidence": self.confidence,
            "category": self.category,
            "stage": self.stage,
            "fix_guide": self.fix_guide,
        }


@dataclass(frozen=True)
class DiagnosisContext:
    run: RunRecord
    evidence_text: dict[str, str]
    missing_logical_paths: tuple[str, ...]


class DiagnosisContextBuilder:
    def __init__(
        self,
        *,
        store: RunStore,
        max_snippet_bytes: int = 8192,
        logical_paths: tuple[str, ...] | None = None,
    ) -> None:
        if max_snippet_bytes <= 0:
            raise ValueError("max_snippet_bytes must be positive")
        self.store = store
        self.max_snippet_bytes = max_snippet_bytes
        self.logical_paths = tuple(dict.fromkeys(logical_paths or known_error_evidence_paths()))

    def build(self, run_id: str) -> DiagnosisContext:
        run = self.store.get_run(run_id)
        objects_by_path = {
            obj.logical_path: obj for obj in self.store.list_evidence_objects(run_id)
        }
        snippets: dict[str, str] = {}
        missing: list[str] = []
        for logical_path in self.logical_paths:
            obj = objects_by_path.get(logical_path)
            if obj is None:
                missing.append(logical_path)
                continue
            text = self._read_snippet(obj)
            if text:
                snippets[logical_path] = text
        return DiagnosisContext(
            run=run,
            evidence_text=snippets,
            missing_logical_paths=tuple(missing),
        )

    def _read_snippet(self, obj: EvidenceObjectRecord) -> str:
        path = Path(obj.store_path)
        if not path.is_file():
            return ""
        data = path.read_bytes()[: self.max_snippet_bytes]
        decoded = data.decode("utf-8", errors="replace")
        if obj.logical_path in {"logs/stdout.tail.json", "logs/stderr.tail.json"}:
            try:
                payload = json.loads(decoded)
            except json.JSONDecodeError:
                return ""
            tail = payload.get("tail") if isinstance(payload, dict) else None
            return tail if isinstance(tail, str) else ""
        return decoded


class DiagnosisService:
    def __init__(
        self,
        *,
        store: RunStore,
        context_builder: DiagnosisContextBuilder | None = None,
    ) -> None:
        self.store = store
        self.context_builder = context_builder or DiagnosisContextBuilder(store=store)

    def diagnose(self, run_id: str) -> list[DiagnosisRecord]:
        context = self.context_builder.build(run_id)
        drafts = diagnose_run(context.run, evidence_text=context.evidence_text)
        return self.store.replace_diagnoses(
            run_id,
            [draft.to_record_payload() for draft in drafts],
        )


def diagnose_run(
    run: RunRecord,
    *,
    evidence_text: dict[str, str] | None = None,
    known_error_rules: tuple[KnownErrorRule, ...] | None = None,
) -> list[DiagnosisDraft]:
    """Return deterministic rule diagnoses from run metadata and evidence snippets."""

    snippets = evidence_text or {}
    diagnoses: list[DiagnosisDraft] = []
    for rule in known_error_rules or load_known_error_rules():
        draft = match_known_error_rule(rule, run, snippets)
        if draft is not None:
            diagnoses.append(draft)

    return _deduplicate(diagnoses)


def load_known_error_rules(
    directory: Path | None = None,
) -> tuple[KnownErrorRule, ...]:
    """Load known error rules from data/known_errors YAML files."""

    rules_dir = directory or _default_known_errors_dir()
    if not rules_dir.is_dir():
        return _fallback_known_error_rules()
    rules: list[KnownErrorRule] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        if path.name == "INDEX.yaml":
            continue
        payload = _load_rule_payload(path)
        rules.append(_known_error_rule_from_payload(payload, source=path))
    return tuple(rules) if rules else _fallback_known_error_rules()


def _default_known_errors_dir() -> Path:
    if _SOURCE_KNOWN_ERRORS_DIR.is_dir():
        return _SOURCE_KNOWN_ERRORS_DIR
    try:
        installed = distribution("pilot107")
    except PackageNotFoundError:
        return _SOURCE_KNOWN_ERRORS_DIR
    for item in installed.files or ():
        normalized = str(item).replace("\\", "/")
        if normalized.endswith("share/pilot107/known_errors/INDEX.yaml"):
            return Path(str(installed.locate_file(item))).resolve().parent
    return _SOURCE_KNOWN_ERRORS_DIR


def known_error_evidence_paths(rules: tuple[KnownErrorRule, ...] | None = None) -> tuple[str, ...]:
    paths = list(DIAGNOSIS_SNIPPET_PATHS)
    for rule in rules or load_known_error_rules():
        paths.extend(rule.evidence_paths)
    return tuple(dict.fromkeys(paths))


def match_known_error_rule(
    rule: KnownErrorRule,
    run: RunRecord,
    snippets: dict[str, str],
) -> DiagnosisDraft | None:
    combined = "\n".join([run.terminal_state or "", run.exit_code or "", *snippets.values()])
    lowered = combined.lower()
    text_matched = any(_matches_symptom(symptom, lowered) for symptom in rule.symptoms)
    terminal_matched = (
        rule.terminal_state_match is not None
        and (run.terminal_state or "").lower() == rule.terminal_state_match.lower()
    )
    state_matched = _matches_state(rule.state_match, run)
    if not (text_matched or terminal_matched or state_matched):
        return None
    return DiagnosisDraft(
        run_id=run.run_id,
        rule_id=rule.error_id,
        severity=rule.severity,
        summary=_summary_for_rule(rule, run),
        evidence_refs=_refs(run.run_id, snippets),
        suggested_patch=rule.suggested_patch,
        retryable=rule.retryable,
        confidence=rule.confidence or ("high" if snippets else "medium"),
        category=rule.category,
        stage=rule.stage,
        fix_guide=rule.fix_guide,
    )


def _draft(
    run: RunRecord,
    rule_id: str,
    summary: str,
    snippets: dict[str, str],
    *,
    suggested_patch: dict[str, Any] | None = None,
    retryable: bool = True,
) -> DiagnosisDraft:
    return DiagnosisDraft(
        run_id=run.run_id,
        rule_id=rule_id,
        severity="error",
        summary=summary,
        evidence_refs=_refs(run.run_id, snippets),
        suggested_patch=suggested_patch or {},
        retryable=retryable,
        confidence="high" if snippets else "medium",
    )


def _matches_symptom(symptom: str, lowered_text: str) -> bool:
    if symptom.startswith("regex:"):
        return re.search(symptom.removeprefix("regex:"), lowered_text) is not None
    return symptom in lowered_text


def _matches_state(state_match: dict[str, Any], run: RunRecord) -> bool:
    if not state_match:
        return False
    expected_state = state_match.get("state")
    if expected_state is not None and run.state.value.lower() != str(expected_state).lower():
        return False
    if "exit_code_not_in" in state_match:
        disallowed = set(state_match["exit_code_not_in"])
        if run.exit_code in disallowed:
            return False
    return True


def _summary_for_rule(rule: KnownErrorRule, run: RunRecord) -> str:
    if rule.error_id == "RUNTIME.NONZERO_EXIT":
        return f"作业以非零退出码结束：{run.exit_code}。"
    return _LEGACY_SUMMARIES.get(rule.error_id, rule.title or rule.root_cause or rule.error_id)


def _known_error_rule_from_payload(payload: dict[str, Any], *, source: Path) -> KnownErrorRule:
    return KnownErrorRule(
        error_id=str(payload["error_id"]),
        category=str(payload.get("category") or "runtime"),
        severity=str(payload.get("severity") or "error"),
        retryable=bool(payload.get("retryable", False)),
        stage=str(payload.get("stage") or "runtime"),
        title=str(payload.get("title") or payload["error_id"]),
        symptoms=tuple(str(item).lower() for item in payload.get("symptoms", [])),
        evidence_paths=tuple(str(item) for item in payload.get("evidence_paths", [])),
        root_cause=str(payload.get("root_cause") or ""),
        fix_template=_dict_value(payload.get("fix_template"), source=source, key="fix_template"),
        fix_guide={
            str(key): str(value)
            for key, value in _dict_value(
                payload.get("fix_guide"),
                source=source,
                key="fix_guide",
            ).items()
        },
        confidence=str(payload.get("confidence") or "medium"),
        kb_article=None if payload.get("kb_article") is None else str(payload["kb_article"]),
        terminal_state_match=(
            None
            if payload.get("terminal_state_match") is None
            else str(payload["terminal_state_match"])
        ),
        state_match=_dict_value(payload.get("state_match"), source=source, key="state_match"),
    )


def _dict_value(value: Any, *, source: Path, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{source}: {key} must be a mapping")
    return value


def _load_rule_payload(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError:
        loaded = _parse_simple_yaml(path.read_text(encoding="utf-8"))
    else:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected YAML mapping")
    return loaded


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    lines = [
        (indent, content)
        for indent, content in (_normalized_yaml_line(line) for line in text.splitlines())
        if content
    ]
    parsed, next_index = _parse_yaml_block(lines, 0, 0)
    if next_index != len(lines) or not isinstance(parsed, dict):
        raise ValueError("unsupported YAML structure")
    return parsed


def _normalized_yaml_line(line: str) -> tuple[int, str]:
    without_comments = _strip_yaml_comment(line.rstrip())
    if not without_comments.strip():
        return 0, ""
    return len(without_comments) - len(without_comments.lstrip(" ")), without_comments.strip()


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index].rstrip()
    return line


def _parse_yaml_block(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    if lines[index][1].startswith("- "):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_mapping(lines, index, indent)


def _parse_yaml_mapping(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(f"unexpected indentation: {content}")
        if content.startswith("- "):
            break
        key, sep, raw_value = content.partition(":")
        if not sep:
            raise ValueError(f"expected mapping entry: {content}")
        key = key.strip()
        raw_value = raw_value.strip()
        index += 1
        if raw_value:
            result[key] = _parse_yaml_scalar(raw_value)
        elif index < len(lines) and lines[index][0] > line_indent:
            result[key], index = _parse_yaml_block(lines, index, lines[index][0])
        else:
            result[key] = None
    return result, index


def _parse_yaml_list(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent != indent or not content.startswith("- "):
            break
        item = content[2:].strip()
        index += 1
        if item:
            result.append(_parse_yaml_scalar(item))
        elif index < len(lines) and lines[index][0] > line_indent:
            value, index = _parse_yaml_block(lines, index, lines[index][0])
            result.append(value)
        else:
            result.append(None)
    return result, index


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value == "{}":
        return {}
    if value == "[]":
        return []
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _fallback_known_error_rules() -> tuple[KnownErrorRule, ...]:
    return (
        KnownErrorRule(
            error_id="SLURM.INVALID_QOS",
            category="resource_policy",
            severity="error",
            retryable=True,
            stage="submit",
            title=_LEGACY_SUMMARIES["SLURM.INVALID_QOS"],
            symptoms=("invalid qos", "invalid qos specification"),
            fix_template={"patch": {"resources.qos": None}},
            confidence="high",
        ),
        KnownErrorRule(
            error_id="SLURM.INVALID_PARTITION",
            category="resource_policy",
            severity="error",
            retryable=True,
            stage="submit",
            title=_LEGACY_SUMMARIES["SLURM.INVALID_PARTITION"],
            symptoms=("invalid partition", "unknown partition"),
            fix_template={"patch": {"resources.partition": None}},
            confidence="high",
        ),
        KnownErrorRule(
            error_id="RUNTIME.COMMAND_NOT_FOUND",
            category="runtime",
            severity="error",
            retryable=True,
            stage="runtime",
            title=_LEGACY_SUMMARIES["RUNTIME.COMMAND_NOT_FOUND"],
            symptoms=("command not found", "no such file or directory"),
            fix_template={"patch": {"entry.command": None}},
            confidence="high",
        ),
        KnownErrorRule(
            error_id="RUNTIME.PYTHON_PACKAGE_MISSING",
            category="runtime",
            severity="error",
            retryable=True,
            stage="runtime",
            title=_LEGACY_SUMMARIES["RUNTIME.PYTHON_PACKAGE_MISSING"],
            symptoms=("modulenotfounderror", "no module named"),
            fix_template={"patch": {"runtime.conda_env": None}},
            confidence="high",
        ),
        KnownErrorRule(
            error_id="RUNTIME.TIMEOUT",
            category="runtime",
            severity="error",
            retryable=True,
            stage="runtime",
            title=_LEGACY_SUMMARIES["RUNTIME.TIMEOUT"],
            symptoms=("timeout",),
            terminal_state_match="TIMEOUT",
            fix_template={"patch": {"resources.time_limit": None}},
            confidence="high",
        ),
        KnownErrorRule(
            error_id="RUNTIME.OOM",
            category="runtime",
            severity="error",
            retryable=True,
            stage="runtime",
            title=_LEGACY_SUMMARIES["RUNTIME.OOM"],
            symptoms=("out_of_memory", "oom", "oom-kill"),
            fix_template={"patch": {"resources.memory": None}},
            confidence="high",
        ),
        KnownErrorRule(
            error_id="RUNTIME.NONZERO_EXIT",
            category="runtime",
            severity="error",
            retryable=True,
            stage="runtime",
            title="作业以非零退出码结束",
            state_match={"state": "failed", "exit_code_not_in": [None, "0:0"]},
            confidence="high",
        ),
    )


def _refs(run_id: str, snippets: dict[str, str]) -> tuple[str, ...]:
    return tuple(f"evidence://runs/{run_id}/{path}" for path in sorted(snippets))


def _deduplicate(diagnoses: list[DiagnosisDraft]) -> list[DiagnosisDraft]:
    by_rule: dict[str, DiagnosisDraft] = {}
    for diagnosis in diagnoses:
        by_rule.setdefault(diagnosis.rule_id, diagnosis)
    if "RUNTIME.CONDA_BATCH_NOT_INITIALIZED" in by_rule:
        by_rule.pop("RUNTIME.COMMAND_NOT_FOUND", None)
    return list(by_rule.values())
