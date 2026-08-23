"""Bounded, owner-scoped implementations of the seven A1 read tools."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pilot107.agent.tool_gateway import (
    AgentReadHandler,
    AgentReadResult,
    AgentToolGatewayError,
)
from pilot107.api.evidence_query import EvidencePreviewUnavailable, EvidenceQueryService
from pilot107.core.code_context import CodeContextError, WorkspaceReader
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.run_store import RunRecord, RunStore

_OWNER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_WORKSPACE_PATHS = 500
_MAX_SEARCH_MATCHES = 100
_MAX_SEARCH_SNIPPET_CHARACTERS = 200
_MAX_SEARCH_RESULT_BYTES = 256 * 1024
_MAX_FILE_BYTES = 64 * 1024
_MAX_PLATFORM_BYTES = 128 * 1024
_MAX_RUN_BYTES = 64 * 1024


class ObservabilityReadService(Protocol):
    def latest_platform(self, connection_id: str) -> dict[str, object]: ...
    def latest_account(
        self, connection_id: str, *, owner: str
    ) -> dict[str, object]: ...
    def run_resources(self, run_id: str, *, owner: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class AgentReadContext:
    platform_snapshot_store: PlatformSnapshotStore | None
    run_store: RunStore
    evidence_query: EvidenceQueryService
    workspace_reader: WorkspaceReader | None
    workspace_root_templates: tuple[str, ...] = ()
    observability_service: ObservabilityReadService | None = None


def build_a1_read_handlers(context: AgentReadContext) -> dict[str, AgentReadHandler]:
    return {
        "platform_get_snapshot": lambda owner, arguments: _platform_snapshot(
            context, owner, arguments
        ),
        "workspace_list": lambda owner, arguments: _workspace_list(
            context, owner, arguments
        ),
        "workspace_search": lambda owner, arguments: _workspace_search(
            context, owner, arguments
        ),
        "workspace_read": lambda owner, arguments: _workspace_read(
            context, owner, arguments
        ),
        "run_get": lambda owner, arguments: _run_get(context, owner, arguments),
        "run_log_read": lambda owner, arguments: _run_log_read(context, owner, arguments),
        "evidence_read": lambda owner, arguments: _evidence_read(
            context, owner, arguments
        ),
        "platform_observation_get": lambda owner, arguments: _platform_observation(
            context, owner, arguments
        ),
        "account_observation_get": lambda owner, arguments: _account_observation(
            context, owner, arguments
        ),
        "run_resources_get": lambda owner, arguments: _run_resources(
            context, owner, arguments
        ),
    }


def _platform_observation(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    del owner
    _closed_arguments(arguments, {"connection_id"})
    service = _observability(context)
    try:
        payload = service.latest_platform(_required_string(arguments, "connection_id"))
    except KeyError:
        raise _error("AGENT.TOOL.NOT_FOUND", "Platform observation was not found") from None
    return _observation_result(payload, prefix="observation")


def _account_observation(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"connection_id"})
    service = _observability(context)
    try:
        payload = service.latest_account(
            _required_string(arguments, "connection_id"), owner=owner
        )
    except KeyError:
        raise _error("AGENT.TOOL.NOT_FOUND", "Account observation was not found") from None
    return _observation_result(payload, prefix="observation")


def _run_resources(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"run_id"})
    service = _observability(context)
    try:
        payload = service.run_resources(_required_string(arguments, "run_id"), owner=owner)
    except KeyError:
        raise _error("AGENT.TOOL.NOT_FOUND", "Run resources were not found") from None
    prefix = (
        "resource-summary"
        if payload.get("kind") == "run_resource_summary"
        else "resource-sample"
    )
    return _observation_result(payload, prefix=prefix)


def _observation_result(payload: dict[str, object], *, prefix: str) -> AgentReadResult:
    observation_id = payload.get("observation_id")
    if not isinstance(observation_id, str):
        raise _error("AGENT.TOOL.INVALID_RESULT", "Observation result is invalid")
    _require_serialized_bound(payload, _MAX_PLATFORM_BYTES)
    return AgentReadResult(
        result=payload,
        evidence_refs=(f"{prefix}:{observation_id}",),
    )


def _observability(context: AgentReadContext) -> ObservabilityReadService:
    if context.observability_service is None:
        raise _error("AGENT.TOOL.UNAVAILABLE", "Resource observation reader is unavailable")
    return context.observability_service


def _platform_snapshot(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, set())
    store = context.platform_snapshot_store
    if store is None:
        raise _error("AGENT.TOOL.UNAVAILABLE", "Platform snapshot reader is unavailable")
    record = store.latest(owner=owner)
    if record is None:
        raise _error("AGENT.TOOL.NOT_FOUND", "Platform snapshot was not found")
    payload = record.safe_payload()
    _require_serialized_bound(payload, _MAX_PLATFORM_BYTES)
    return AgentReadResult(
        result=payload,
        evidence_refs=(f"platform-snapshot:{record.snapshot_id}",),
    )


def _workspace_list(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"workspace"})
    workspace = _workspace(context, owner, _required_string(arguments, "workspace"))
    paths, truncated = _workspace_paths(context, workspace)
    return AgentReadResult(
        result={
            "workspace": workspace,
            "items": [{"path": path, "kind": "tracked"} for path in paths],
            "limit": _MAX_WORKSPACE_PATHS,
            "truncated": truncated,
        },
        evidence_refs=(f"workspace:{workspace}:index",),
    )


def _workspace_read(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"workspace", "path"})
    workspace = _workspace(context, owner, _required_string(arguments, "workspace"))
    relative = _relative_path(_required_string(arguments, "path"))
    reader = _reader(context)
    try:
        text = reader.read_text(workspace, relative, max_bytes=_MAX_FILE_BYTES + 1)
    except (CodeContextError, OSError, UnicodeError):
        raise _error("AGENT.TOOL.PATH_FORBIDDEN", "Workspace file cannot be read") from None
    encoded = text.encode("utf-8")
    truncated = len(encoded) > _MAX_FILE_BYTES
    visible = encoded[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return AgentReadResult(
        result={
            "workspace": workspace,
            "path": relative,
            "content": visible,
            "encoding": "utf-8",
            "truncated": truncated,
        },
        evidence_refs=(f"workspace:{workspace}:{relative}",),
    )


def _workspace_search(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"workspace", "query"})
    workspace = _workspace(context, owner, _required_string(arguments, "workspace"))
    query = _required_string(arguments, "query")
    if len(query) > 256 or "\0" in query:
        raise _error("AGENT.TOOL.INVALID", "Workspace search query is invalid")
    reader = _reader(context)
    matches: list[dict[str, object]] = []
    refs: list[str] = []
    paths, _ = _workspace_paths(context, workspace)
    for relative in paths:
        try:
            text = reader.read_text(workspace, relative, max_bytes=_MAX_FILE_BYTES)
        except (CodeContextError, OSError, UnicodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if query not in line:
                continue
            snippet = line.strip()[:_MAX_SEARCH_SNIPPET_CHARACTERS]
            matches.append({"path": relative, "line": number, "snippet": snippet})
            refs.append(f"workspace:{workspace}:{relative}:{number}")
            if len(matches) >= _MAX_SEARCH_MATCHES:
                break
        if len(matches) >= _MAX_SEARCH_MATCHES:
            break
    payload = {
        "workspace": workspace,
        "query": query,
        "matches": matches,
        "limit": _MAX_SEARCH_MATCHES,
        "truncated": len(matches) == _MAX_SEARCH_MATCHES,
    }
    _require_serialized_bound(payload, _MAX_SEARCH_RESULT_BYTES)
    return AgentReadResult(result=payload, evidence_refs=tuple(refs))


def _run_get(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"run_id"})
    run = _owned_run(context.run_store, owner, _required_string(arguments, "run_id"))
    payload = _safe_run_payload(run)
    _require_serialized_bound(payload, _MAX_RUN_BYTES)
    return AgentReadResult(result=payload, evidence_refs=(f"run:{run.run_id}",))


def _run_log_read(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"run_id", "stream", "cursor"})
    run_id = _required_string(arguments, "run_id")
    _owned_run(context.run_store, owner, run_id)
    stream = _required_string(arguments, "stream")
    if stream not in {"stdout", "stderr"}:
        raise _error("AGENT.TOOL.INVALID", "Log stream must be stdout or stderr")
    cursor = _required_integer(arguments, "cursor", minimum=0, maximum=2**31 - 1)
    candidates = [
        item
        for item in context.run_store.list_evidence_objects(run_id)
        if item.logical_path.endswith(f"/{stream}.txt")
        or item.logical_path.endswith(f"/{stream}.log")
    ]
    if not candidates:
        raise _error("AGENT.TOOL.NOT_FOUND", "Run log was not found")
    selected = candidates[0]
    path = _bound_evidence_path(context.evidence_query, run_id, selected.store_path)
    with path.open("rb") as handle:
        handle.seek(cursor)
        data = handle.read(_MAX_FILE_BYTES + 1)
    visible = data[:_MAX_FILE_BYTES]
    return AgentReadResult(
        result={
            "run_id": run_id,
            "stream": stream,
            "cursor": cursor,
            "next_cursor": cursor + len(visible),
            "content": visible.decode("utf-8", errors="replace"),
            "truncated": len(data) > _MAX_FILE_BYTES,
        },
        evidence_refs=(f"evidence:{run_id}:{selected.object_id}",),
    )


def _evidence_read(
    context: AgentReadContext, owner: str, arguments: Mapping[str, object]
) -> AgentReadResult:
    _closed_arguments(arguments, {"run_id", "object_id"})
    run_id = _required_string(arguments, "run_id")
    object_id = _required_string(arguments, "object_id")
    _owned_run(context.run_store, owner, run_id)
    try:
        preview = context.evidence_query.get_object_preview(
            run_id, object_id, max_bytes=56 * 1024
        )
    except (KeyError, EvidencePreviewUnavailable, OSError, ValueError):
        raise _error("AGENT.TOOL.NOT_FOUND", "Evidence object was not found") from None
    preview.pop("source_uri", None)
    _require_serialized_bound(preview, _MAX_FILE_BYTES)
    return AgentReadResult(
        result=preview,
        evidence_refs=(f"evidence:{run_id}:{object_id}",),
    )


def _workspace(context: AgentReadContext, owner: str, requested: str) -> str:
    if _OWNER.fullmatch(owner) is None:
        raise _error("AGENT.TOOL.UNAUTHORIZED", "Workspace owner is invalid")
    reader = _reader(context)
    try:
        resolved = reader.resolve_workspace(requested)
    except (CodeContextError, OSError, ValueError):
        raise _error("AGENT.TOOL.PATH_FORBIDDEN", "Workspace is not authorized") from None
    path = Path(resolved)
    allowed = False
    for template in context.workspace_root_templates:
        try:
            root = Path(template.format(user=owner)).expanduser().resolve(strict=False)
            candidate = path.resolve(strict=False)
        except (KeyError, OSError, ValueError):
            continue
        if candidate == root or candidate.is_relative_to(root):
            allowed = True
            break
    if not allowed:
        raise _error("AGENT.TOOL.PATH_FORBIDDEN", "Workspace is not authorized")
    return resolved


def _workspace_paths(
    context: AgentReadContext, workspace: str
) -> tuple[list[str], bool]:
    reader = _reader(context)
    try:
        raw = reader.git(workspace, ("ls-files", "-z"))
    except (CodeContextError, OSError, UnicodeError):
        raise _error("AGENT.TOOL.READ_FAILED", "Workspace index cannot be read") from None
    paths: list[str] = []
    for value in raw.split("\0"):
        if not value:
            continue
        try:
            paths.append(_relative_path(value))
        except AgentToolGatewayError:
            continue
        if len(paths) > _MAX_WORKSPACE_PATHS:
            return paths[:_MAX_WORKSPACE_PATHS], True
    return paths, False


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or "\0" in value
    ):
        raise _error("AGENT.TOOL.PATH_FORBIDDEN", "Workspace path is not authorized")
    return path.as_posix()


def _owned_run(store: RunStore, owner: str, run_id: str) -> RunRecord:
    try:
        run = store.get_run(run_id)
    except KeyError:
        raise _error("AGENT.TOOL.NOT_FOUND", "Run was not found") from None
    if run.owner != owner:
        raise _error("AGENT.TOOL.NOT_FOUND", "Run was not found")
    return run


def _safe_run_payload(run: RunRecord) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "state": run.state.value,
        "collection_state": run.collection_state.value,
        "diagnosis_state": run.diagnosis_state.value,
        "capsule_state": run.capsule_state.value,
        "result_status": run.result_status.value,
        "job_id": run.job_id,
        "job_name": run.job_name,
        "exit_code": run.exit_code,
        "terminal_state": run.terminal_state,
        "resource_plan": run.resource_plan,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _bound_evidence_path(
    query: EvidenceQueryService, run_id: str, store_path: str
) -> Path:
    root = query.evidence_store.run_root(run_id).resolve()
    path = Path(store_path)
    if path.is_symlink():
        raise _error("AGENT.TOOL.PATH_FORBIDDEN", "Evidence path is not authorized")
    resolved = path.resolve(strict=True)
    if resolved == root or not resolved.is_relative_to(root) or not resolved.is_file():
        raise _error("AGENT.TOOL.PATH_FORBIDDEN", "Evidence path is not authorized")
    return resolved


def _reader(context: AgentReadContext) -> WorkspaceReader:
    if context.workspace_reader is None or not context.workspace_root_templates:
        raise _error("AGENT.TOOL.UNAVAILABLE", "Workspace reader is unavailable")
    return context.workspace_reader


def _closed_arguments(arguments: Mapping[str, object], required: set[str]) -> None:
    if not isinstance(arguments, Mapping) or set(arguments) != required:
        raise _error("AGENT.TOOL.INVALID", "Agent tool arguments are invalid")


def _required_string(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value or len(value) > 4_096 or "\0" in value:
        raise _error("AGENT.TOOL.INVALID", "Agent tool arguments are invalid")
    return value


def _required_integer(
    arguments: Mapping[str, object], key: str, *, minimum: int, maximum: int
) -> int:
    value = arguments.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _error("AGENT.TOOL.INVALID", "Agent tool arguments are invalid")
    return value


def _require_serialized_bound(payload: object, maximum: int) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise _error("AGENT.TOOL.RESULT_TOO_LARGE", "Agent tool result exceeds its bound")


def _error(code: str, message: str) -> AgentToolGatewayError:
    return AgentToolGatewayError(message, code=code)
