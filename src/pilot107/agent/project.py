"""Experiment Project and Blueprint domain records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

PROJECT_SCHEMA_VERSION = "pilot107.experiment-project-session/v1"
EXPERIMENT_BUILDER_PROFILE = "experiment_builder"
RUN_DIAGNOSIS_REPAIR_PROFILE = "run_diagnosis_repair"
MARKET_APPLICATION_PROFILE = "market_application"
TEMPLATE_PUBLICATION_PROFILE = "template_publication"
PROJECT_AGENT_PROFILES = frozenset(
    {
        EXPERIMENT_BUILDER_PROFILE,
        RUN_DIAGNOSIS_REPAIR_PROFILE,
        MARKET_APPLICATION_PROFILE,
        TEMPLATE_PUBLICATION_PROFILE,
    }
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSIONED_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,256}$")
_RESOURCE_HINTS = frozenset(
    {"partition", "qos", "cpus_per_task", "memory_mib", "gpus", "time_limit"}
)


class ProjectConflict(RuntimeError):
    """Raised when a request key or optimistic version no longer matches."""


def is_project_agent_profile(profile_id: str) -> bool:
    return profile_id in PROJECT_AGENT_PROFILES


class ExperimentProjectOrigin(StrEnum):
    BLANK = "blank"
    TEMPLATE = "template"
    EXISTING = "existing"
    FAILED_RUN = "failed_run"


class ExperimentProjectState(StrEnum):
    DRAFTING = "drafting"
    EDITING = "editing"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHING = "publishing"
    READY = "ready"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ProjectSource:
    kind: Literal["template", "existing", "failed_run"]
    ref_id: str
    cluster_path: str | None

    def __post_init__(self) -> None:
        if self.kind not in {"template", "existing", "failed_run"}:
            raise ValueError("Project source kind is invalid")
        _identifier(self.ref_id, "source ref_id")
        if self.cluster_path is not None:
            _relative_path(self.cluster_path)


@dataclass(frozen=True)
class ProjectFile:
    path: str
    purpose: str
    classification: Literal["editable", "read_only", "metadata_only", "excluded"]

    def __post_init__(self) -> None:
        _relative_path(self.path)
        _bounded_text(self.purpose, "file purpose", maximum=4096)
        if self.classification not in {
            "editable",
            "read_only",
            "metadata_only",
            "excluded",
        }:
            raise ValueError("file classification is invalid")


@dataclass(frozen=True)
class ProjectValidation:
    validation_id: str
    execution: Literal["sandbox", "slurm"]
    argv: tuple[str, ...]
    expected_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))
        _identifier(self.validation_id, "validation_id")
        if self.execution not in {"sandbox", "slurm"}:
            raise ValueError("validation execution is invalid")
        if not self.argv or len(self.argv) > 128:
            raise ValueError("validation argv must contain 1 to 128 items")
        for argument in self.argv:
            _bounded_text(argument, "validation argument", maximum=4096)
        if len(self.expected_outputs) > 256:
            raise ValueError("validation expected_outputs exceeds 256 items")
        for output in self.expected_outputs:
            _relative_path(output)


@dataclass(frozen=True)
class ProjectContractIntent:
    recipe_version_id: str | None
    resource_hints: Mapping[str, str | int]

    def __post_init__(self) -> None:
        if self.recipe_version_id is not None and not _VERSIONED_ID.fullmatch(
            self.recipe_version_id
        ):
            raise ValueError("recipe_version_id is invalid")
        hints = dict(self.resource_hints)
        unknown = sorted(set(hints) - _RESOURCE_HINTS)
        if unknown:
            raise ValueError(f"unsupported resource hints: {', '.join(unknown)}")
        for name in ("partition", "qos", "time_limit"):
            value = hints.get(name)
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError(f"resource hint {name} must be a string")
                maximum = 64 if name == "time_limit" else 128
                _bounded_text(value, f"resource hint {name}", maximum=maximum)
        for name in ("cpus_per_task", "memory_mib", "gpus"):
            value = hints.get(name)
            if value is not None:
                minimum = 0 if name == "gpus" else 1
                maximum = 1_125_899_906_842_624 if name == "memory_mib" else 1_048_576
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not minimum <= value <= maximum
                ):
                    raise ValueError(f"resource hint {name} is invalid")
        object.__setattr__(self, "resource_hints", MappingProxyType(hints))


@dataclass(frozen=True)
class ProjectExpectedOutput:
    path: str
    kind: Literal["file", "directory", "json", "table", "metric"]
    required: bool

    def __post_init__(self) -> None:
        _relative_path(self.path)
        if self.kind not in {"file", "directory", "json", "table", "metric"}:
            raise ValueError("expected output kind is invalid")
        if not isinstance(self.required, bool):
            raise TypeError("expected output required must be a boolean")


@dataclass(frozen=True)
class ProjectDependency:
    name: str
    version: str
    source: Literal["runtime", "module", "conda", "project", "system"]

    def __post_init__(self) -> None:
        _bounded_text(self.name, "dependency name", maximum=256)
        _bounded_text(self.version, "dependency version", maximum=256)
        if self.source not in {"runtime", "module", "conda", "project", "system"}:
            raise ValueError("dependency source is invalid")


@dataclass(frozen=True)
class ProjectBlueprint:
    goal: str
    entrypoints: tuple[str, ...]
    files: tuple[ProjectFile, ...]
    validations: tuple[ProjectValidation, ...]
    contract_intent: ProjectContractIntent
    expected_outputs: tuple[ProjectExpectedOutput, ...]
    dependencies: tuple[ProjectDependency, ...]
    open_questions: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "entrypoints",
            "files",
            "validations",
            "expected_outputs",
            "dependencies",
            "open_questions",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        _bounded_text(self.goal, "blueprint goal", maximum=64_000)
        _bounded_items(self.entrypoints, "entrypoints", maximum=64)
        for entrypoint in self.entrypoints:
            _relative_path(entrypoint)
        _typed_items(self.files, ProjectFile, "files", maximum=4096)
        _typed_items(self.validations, ProjectValidation, "validations", maximum=256)
        if not isinstance(self.contract_intent, ProjectContractIntent):
            raise TypeError("contract_intent must be ProjectContractIntent")
        _typed_items(
            self.expected_outputs,
            ProjectExpectedOutput,
            "expected_outputs",
            maximum=4096,
        )
        _typed_items(self.dependencies, ProjectDependency, "dependencies", maximum=4096)
        _bounded_items(self.open_questions, "open_questions", maximum=256)
        for question in self.open_questions:
            _bounded_text(question, "open question", maximum=4096)


@dataclass(frozen=True)
class ExperimentProjectSessionRecord:
    project_id: str
    owner: str
    origin: ExperimentProjectOrigin
    state: ExperimentProjectState
    version: int
    goal: str
    source: ProjectSource | None
    blueprint: ProjectBlueprint | None
    created_at: str
    updated_at: str
    schema_version: str = PROJECT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _identifier(self.owner, "owner")
        if not isinstance(self.origin, ExperimentProjectOrigin):
            raise TypeError("origin must be ExperimentProjectOrigin")
        if not isinstance(self.state, ExperimentProjectState):
            raise TypeError("state must be ExperimentProjectState")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("Project version is invalid")
        _bounded_text(self.goal, "Project goal", maximum=64_000)
        if self.source is not None and not isinstance(self.source, ProjectSource):
            raise TypeError("source must be ProjectSource")
        if self.blueprint is not None and not isinstance(self.blueprint, ProjectBlueprint):
            raise TypeError("blueprint must be ProjectBlueprint")
        _bounded_text(self.created_at, "created_at", maximum=64)
        _bounded_text(self.updated_at, "updated_at", maximum=64)
        if self.schema_version != PROJECT_SCHEMA_VERSION:
            raise ValueError("Project schema_version is invalid")


def blueprint_payload(blueprint: ProjectBlueprint) -> dict[str, Any]:
    return {
        "goal": blueprint.goal,
        "entrypoints": list(blueprint.entrypoints),
        "files": [
            {
                "path": item.path,
                "purpose": item.purpose,
                "classification": item.classification,
            }
            for item in blueprint.files
        ],
        "validations": [
            {
                "validation_id": item.validation_id,
                "execution": item.execution,
                "argv": list(item.argv),
                "expected_outputs": list(item.expected_outputs),
            }
            for item in blueprint.validations
        ],
        "contract_intent": {
            "recipe_version_id": blueprint.contract_intent.recipe_version_id,
            "resource_hints": dict(blueprint.contract_intent.resource_hints),
        },
        "expected_outputs": [
            {"path": item.path, "kind": item.kind, "required": item.required}
            for item in blueprint.expected_outputs
        ],
        "dependencies": [
            {"name": item.name, "version": item.version, "source": item.source}
            for item in blueprint.dependencies
        ],
        "open_questions": list(blueprint.open_questions),
    }


def blueprint_from_payload(value: Mapping[str, Any]) -> ProjectBlueprint:
    intent = _mapping(value.get("contract_intent"), "contract_intent")
    return ProjectBlueprint(
        goal=_string(value.get("goal"), "goal"),
        entrypoints=tuple(_string(item, "entrypoint") for item in _list(value, "entrypoints")),
        files=tuple(
            ProjectFile(
                path=_string(item.get("path"), "file path"),
                purpose=_string(item.get("purpose"), "file purpose"),
                classification=_string(item.get("classification"), "file classification"),  # type: ignore[arg-type]
            )
            for item in _mapping_list(value, "files")
        ),
        validations=tuple(
            ProjectValidation(
                validation_id=_string(item.get("validation_id"), "validation_id"),
                execution=_string(item.get("execution"), "validation execution"),  # type: ignore[arg-type]
                argv=tuple(
                    _string(argument, "validation argument") for argument in _list(item, "argv")
                ),
                expected_outputs=tuple(
                    _string(output, "validation output")
                    for output in _list(item, "expected_outputs")
                ),
            )
            for item in _mapping_list(value, "validations")
        ),
        contract_intent=ProjectContractIntent(
            recipe_version_id=_optional_string(intent.get("recipe_version_id")),
            resource_hints=_resource_hints(intent.get("resource_hints")),
        ),
        expected_outputs=tuple(
            ProjectExpectedOutput(
                path=_string(item.get("path"), "expected output path"),
                kind=_string(item.get("kind"), "expected output kind"),  # type: ignore[arg-type]
                required=_boolean(item.get("required"), "expected output required"),
            )
            for item in _mapping_list(value, "expected_outputs")
        ),
        dependencies=tuple(
            ProjectDependency(
                name=_string(item.get("name"), "dependency name"),
                version=_string(item.get("version"), "dependency version"),
                source=_string(item.get("source"), "dependency source"),  # type: ignore[arg-type]
            )
            for item in _mapping_list(value, "dependencies")
        ),
        open_questions=tuple(
            _string(item, "open question") for item in _list(value, "open_questions")
        ),
    )


def source_payload(source: ProjectSource | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {"kind": source.kind, "ref_id": source.ref_id, "cluster_path": source.cluster_path}


def source_from_payload(value: Mapping[str, Any] | None) -> ProjectSource | None:
    if value is None:
        return None
    return ProjectSource(
        kind=_string(value.get("kind"), "source kind"),  # type: ignore[arg-type]
        ref_id=_string(value.get("ref_id"), "source ref_id"),
        cluster_path=_optional_string(value.get("cluster_path")),
    )


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _relative_path(value: str) -> str:
    _bounded_text(value, "relative project path", maximum=4096)
    if value.startswith("/") or any(part == ".." for part in value.split("/")):
        raise ValueError("relative project path must stay inside the Workspace")
    return value


def _bounded_text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise ValueError(f"{label} is invalid")
    return value


def _bounded_items(value: tuple[Any, ...], label: str, *, maximum: int) -> None:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise ValueError(f"{label} must be a tuple with at most {maximum} items")


def _typed_items(value: tuple[Any, ...], expected: type[Any], label: str, *, maximum: int) -> None:
    _bounded_items(value, label, maximum=maximum)
    if any(not isinstance(item, expected) for item in value):
        raise TypeError(f"{label} contains an invalid item")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _list(container: Mapping[str, Any], key: str) -> list[Any]:
    value = container.get(key)
    if not isinstance(value, list):
        raise TypeError(f"{key} must be an array")
    return value


def _mapping_list(container: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(_mapping(item, key) for item in _list(container, key))


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value, "optional value")


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


def _resource_hints(value: object) -> dict[str, str | int]:
    hints = _mapping(value, "resource_hints")
    if any(not isinstance(item, (str, int)) or isinstance(item, bool) for item in hints.values()):
        raise TypeError("resource_hints contains an invalid value")
    return {key: item for key, item in hints.items() if isinstance(item, (str, int))}
