"""Authorization and deterministic publication gates for template releases."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pilot107.core.contracts import ContractError, ContractService
from pilot107.core.resources import PreflightSeverity

_SPDX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|access[_-]?key|password|passwd|secret|token|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{20,}"),
)
_PLACEHOLDERS = {"", "none", "n/a", "not-applicable", "redacted", "example", "placeholder"}
_POLICY_VERSION = "template-publication/v1"
_RAW_SBATCH_SAFE_DIRECTIVES = frozenset({"--exclusive"})


class TemplateReviewerRole(StrEnum):
    REVIEWER = "reviewer"
    COURSE_INSTRUCTOR = "course_instructor"
    COURSE_TA = "course_ta"
    ADMIN = "admin"


@dataclass(frozen=True)
class TemplateReviewerPrincipal:
    actor: str
    roles: frozenset[TemplateReviewerRole]
    course_scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TemplateRoleDirectory:
    """Server-controlled template roles and course memberships."""

    reviewers: frozenset[str] = frozenset()
    admins: frozenset[str] = frozenset()
    course_instructors: Mapping[str, frozenset[str]] = field(default_factory=dict)
    course_tas: Mapping[str, frozenset[str]] = field(default_factory=dict)
    course_members: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def reviewer_principal(self, actor: str) -> TemplateReviewerPrincipal:
        roles: set[TemplateReviewerRole] = set()
        if actor in self.reviewers:
            roles.add(TemplateReviewerRole.REVIEWER)
        if actor in self.admins:
            roles.add(TemplateReviewerRole.ADMIN)
        instructor_scopes = _scopes_for_actor(self.course_instructors, actor)
        ta_scopes = _scopes_for_actor(self.course_tas, actor)
        if instructor_scopes:
            roles.add(TemplateReviewerRole.COURSE_INSTRUCTOR)
        if ta_scopes:
            roles.add(TemplateReviewerRole.COURSE_TA)
        return TemplateReviewerPrincipal(
            actor=actor,
            roles=frozenset(roles),
            course_scopes=instructor_scopes | ta_scopes,
        )

    def system_reviewer_principal(self) -> TemplateReviewerPrincipal:
        """Return a system principal for seed/bootstrap publishing.

        The system reviewer is distinct from any draft owner to bypass the
        self-review prohibition (system seed is not user behavior).
        """
        return TemplateReviewerPrincipal(
            actor="pilot107-system-reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER, TemplateReviewerRole.ADMIN}),
        )

    def visible_course_scopes(self, actor: str) -> frozenset[str]:
        scopes = (
            _scopes_for_actor(self.course_members, actor)
            | _scopes_for_actor(self.course_instructors, actor)
            | _scopes_for_actor(self.course_tas, actor)
        )
        if actor in self.admins:
            scopes |= frozenset(
                {
                    *self.course_members,
                    *self.course_instructors,
                    *self.course_tas,
                }
            )
        return scopes


@dataclass(frozen=True)
class TemplateGateFinding:
    severity: str
    code: str
    message: str
    source: str

    @property
    def blocking(self) -> bool:
        return self.severity == "block"

    def as_payload(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class TemplateGateResult:
    status: str
    findings: tuple[TemplateGateFinding, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "policy_version": _POLICY_VERSION,
            "status": self.status,
            "findings": [finding.as_payload() for finding in self.findings],
        }


class TemplatePublicationGate:
    """Validate the canonical Contract and market-specific publication metadata."""

    def __init__(
        self,
        contract_service: ContractService,
        *,
        verified_container_digests: frozenset[str] = frozenset(),
    ) -> None:
        self.contract_service = contract_service
        self.verified_container_digests = verified_container_digests

    def validate(
        self,
        *,
        payload: dict[str, Any],
        compatibility: dict[str, Any],
        publication: dict[str, Any],
    ) -> TemplateGateResult:
        findings: list[TemplateGateFinding] = []
        canonical: dict[str, Any] | None = None
        try:
            validation = self.contract_service.validate(payload)
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, ContractError) else "CONTRACT.INVALID"
            findings.append(_block(code, str(exc), source="contract"))
        else:
            effective = validation.effective_request.get("contract")
            if isinstance(effective, dict):
                canonical = effective
            findings.extend(
                TemplateGateFinding(
                    severity=("block" if finding.severity == PreflightSeverity.BLOCK else "warn"),
                    code=finding.code,
                    message=finding.message,
                    source="contract",
                )
                for finding in validation.findings
            )
            findings.extend(
                _block(
                    str(item.get("rule_id", "RISK.UNSPECIFIED")),
                    str(item.get("message", "contract risk lint failed")),
                    source="risk_lint",
                )
                for item in validation.risk_lint
                if item.get("severity") == "high_risk" or item.get("blocking") is True
            )

        findings.extend(_publication_findings(publication))
        findings.extend(_secret_findings((payload, compatibility, publication)))
        if canonical is not None:
            findings.extend(_path_findings(canonical))
            findings.extend(
                _compatibility_findings(
                    canonical,
                    compatibility,
                    verified_container_digests=self.verified_container_digests,
                )
            )
            findings.extend(_dangerous_shell_findings(canonical))
            findings.extend(_raw_sbatch_findings(canonical))
        ordered = tuple(_deduplicate(findings))
        return TemplateGateResult(
            status="BLOCK" if any(finding.blocking for finding in ordered) else "OK",
            findings=ordered,
        )


def authorize_template_review(
    *,
    principal: TemplateReviewerPrincipal,
    requester: str,
    visibility: str,
    scope_key: str | None,
) -> tuple[TemplateReviewerRole, str | None]:
    if principal.actor == requester:
        raise PermissionError("template authors cannot review their own draft")
    if TemplateReviewerRole.ADMIN in principal.roles:
        return TemplateReviewerRole.ADMIN, scope_key
    if visibility == "course":
        course_roles = {
            TemplateReviewerRole.COURSE_INSTRUCTOR,
            TemplateReviewerRole.COURSE_TA,
        }
        if (
            scope_key is not None
            and principal.roles & course_roles
            and scope_key in principal.course_scopes
        ):
            role = (
                TemplateReviewerRole.COURSE_INSTRUCTOR
                if TemplateReviewerRole.COURSE_INSTRUCTOR in principal.roles
                else TemplateReviewerRole.COURSE_TA
            )
            return role, scope_key
        raise PermissionError("course review requires an instructor or TA in the same course")
    if TemplateReviewerRole.REVIEWER in principal.roles:
        return TemplateReviewerRole.REVIEWER, None
    raise PermissionError("template review requires reviewer or admin authority")


def _scopes_for_actor(memberships: Mapping[str, frozenset[str]], actor: str) -> frozenset[str]:
    return frozenset(scope for scope, actors in memberships.items() if actor in actors)


def _publication_findings(publication: dict[str, Any]) -> list[TemplateGateFinding]:
    findings: list[TemplateGateFinding] = []
    license_id = publication.get("license")
    if not isinstance(license_id, str) or not _SPDX_ID.fullmatch(license_id):
        findings.append(
            _block(
                "TEMPLATE.LICENSE_REQUIRED",
                "publication.license must be an SPDX license identifier",
                source="publication",
            )
        )
    for key, code in (
        ("attribution", "TEMPLATE.ATTRIBUTION_REQUIRED"),
        ("dataset_access", "TEMPLATE.DATASET_ACCESS_REQUIRED"),
        ("risk_statement", "TEMPLATE.RISK_STATEMENT_REQUIRED"),
    ):
        value = publication.get(key)
        if not isinstance(value, str) or not value.strip():
            findings.append(_block(code, f"publication.{key} is required", source="publication"))
    return findings


def _secret_findings(values: Iterable[object]) -> list[TemplateGateFinding]:
    findings: list[TemplateGateFinding] = []
    for path, value in _walk(values):
        key = path.rsplit(".", 1)[-1]
        text = value if isinstance(value, str) else ""
        if _SECRET_KEY.search(key) and text.strip().lower() not in _PLACEHOLDERS:
            findings.append(
                _block(
                    "TEMPLATE.SECRET_DETECTED",
                    f"secret-like value is not allowed at {path}",
                    source="secret_scan",
                )
            )
            continue
        if any(pattern.search(text) for pattern in _SECRET_VALUE_PATTERNS):
            findings.append(
                _block(
                    "TEMPLATE.SECRET_DETECTED",
                    f"credential material is not allowed at {path}",
                    source="secret_scan",
                )
            )
    return findings


def _walk(values: Iterable[object]) -> Iterable[tuple[str, object]]:
    def visit(value: object, path: str) -> Iterable[tuple[str, object]]:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield from visit(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                yield from visit(child, f"{path}[{index}]")
        else:
            yield path, value

    for index, value in enumerate(values):
        yield from visit(value, f"document[{index}]")


def _path_findings(payload: dict[str, Any]) -> list[TemplateGateFinding]:
    project = payload.get("project")
    workdir = project.get("workdir") if isinstance(project, dict) else None
    if not isinstance(workdir, str):
        return []
    path = PurePosixPath(workdir)
    if not path.is_absolute() or ".." in path.parts or not workdir.startswith("/public/"):
        return [
            _block(
                "TEMPLATE.WORKDIR_UNSAFE",
                "template workdir must be an absolute path under /public",
                source="path_policy",
            )
        ]
    return []


def _compatibility_findings(
    payload: dict[str, Any],
    compatibility: dict[str, Any],
    *,
    verified_container_digests: frozenset[str],
) -> list[TemplateGateFinding]:
    findings: list[TemplateGateFinding] = []
    resources = payload.get("resources")
    resources = resources if isinstance(resources, dict) else {}
    partition = resources.get("partition")
    partitions = compatibility.get("partitions")
    if (
        not isinstance(partitions, list)
        or not partitions
        or not all(isinstance(item, str) and item for item in partitions)
        or partition not in partitions
    ):
        findings.append(
            _block(
                "TEMPLATE.COMPATIBILITY_PARTITION",
                "compatibility.partitions must include the Contract partition",
                source="compatibility",
            )
        )
    requested_gpu = any(
        isinstance(resources.get(key), int) and resources[key] > 0
        for key in ("gpus_total", "gpus_per_node")
    )
    if compatibility.get("gpu") is not requested_gpu:
        findings.append(
            _block(
                "TEMPLATE.COMPATIBILITY_GPU",
                "compatibility.gpu must match the Contract GPU request",
                source="compatibility",
            )
        )
    runtime = payload.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    image = runtime.get("container_image")
    if isinstance(image, str) and image:
        container = compatibility.get("container")
        digest = container.get("image_digest") if isinstance(container, dict) else None
        if (
            not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or digest not in verified_container_digests
        ):
            findings.append(
                _block(
                    "TEMPLATE.CONTAINER_UNVERIFIED",
                    "container digest is not registered by a trusted verifier",
                    source="compatibility",
                )
            )
        elif "@sha256:" in image and not image.endswith(digest):
            findings.append(
                _block(
                    "TEMPLATE.CONTAINER_DIGEST_MISMATCH",
                    "container verification digest does not match runtime.container_image",
                    source="compatibility",
                )
            )
    return findings


def _dangerous_shell_findings(payload: dict[str, Any]) -> list[TemplateGateFinding]:
    entry = payload.get("entry")
    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str):
        return []
    normalized = " ".join(command.lower().split())
    findings: list[TemplateGateFinding] = []
    for pattern, code, label in (
        (r"(?:^|[;&|]\s*)sudo(?:\s|$)", "TEMPLATE.SHELL_PRIVILEGE_ESCALATION", "sudo"),
        (r"\bsrun\s+--pty\b", "TEMPLATE.SHELL_INTERACTIVE", "srun --pty"),
        (r"\bchmod\s+777\b", "TEMPLATE.SHELL_WORLD_WRITABLE", "chmod 777"),
        (r"\brm\s+-[a-z]*(?:r[a-z]*f|f[a-z]*r)[a-z]*\b", "RISK.RM_RF", "rm -rf"),
        (
            r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba)?sh\b",
            "RISK.CURL_BASH",
            "network pipe to shell",
        ),
    ):
        if re.search(pattern, normalized):
            findings.append(
                _block(
                    code,
                    f"dangerous shell construct detected: {label}",
                    source="shell_lint",
                )
            )
    return findings


def _raw_sbatch_findings(payload: dict[str, Any]) -> list[TemplateGateFinding]:
    extensions = payload.get("extensions")
    advanced = extensions.get("advanced") if isinstance(extensions, dict) else None
    raw = advanced.get("raw_sbatch") if isinstance(advanced, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, str) or "\x00" in raw or "`" in raw or "$(" in raw:
        return [
            _block(
                "TEMPLATE.RAW_SBATCH_UNSAFE",
                "advanced raw_sbatch must contain only supported literal directives",
                source="sbatch_lint",
            )
        ]
    findings: list[TemplateGateFinding] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#SBATCH "):
            findings.append(
                _block(
                    "TEMPLATE.RAW_SBATCH_UNSAFE",
                    "advanced raw_sbatch contains a non-directive line",
                    source="sbatch_lint",
                )
            )
            continue
        directive = stripped.removeprefix("#SBATCH ").split("=", 1)[0].split(None, 1)[0]
        if directive not in _RAW_SBATCH_SAFE_DIRECTIVES:
            findings.append(
                _block(
                    "TEMPLATE.RAW_SBATCH_UNSUPPORTED",
                    f"advanced raw_sbatch directive is not allowed: {directive}",
                    source="sbatch_lint",
                )
            )
        elif stripped != "#SBATCH --exclusive":
            findings.append(
                _block(
                    "TEMPLATE.RAW_SBATCH_UNSAFE",
                    "the --exclusive raw_sbatch directive does not accept a value",
                    source="sbatch_lint",
                )
            )
    return findings


def _block(code: str, message: str, *, source: str) -> TemplateGateFinding:
    return TemplateGateFinding(severity="block", code=code, message=message, source=source)


def _deduplicate(findings: Iterable[TemplateGateFinding]) -> list[TemplateGateFinding]:
    seen: set[tuple[str, str]] = set()
    unique: list[TemplateGateFinding] = []
    for finding in findings:
        key = (finding.code, finding.message)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique
