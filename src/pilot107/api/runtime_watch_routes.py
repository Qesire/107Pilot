"""Owner-scoped, persistence-only Runtime Watch read routes."""

from __future__ import annotations

import base64
import codecs
import json
from collections.abc import Mapping
from typing import cast

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.runtime_watch.model import RuntimeLogStream
from pilot107.runtime_watch.store import RuntimeWatchStore


class RuntimeWatchRoutes:
    def __init__(self, store: RuntimeWatchStore) -> None:
        self.store = store

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if len(parts) < 3 or parts[0] != "runs" or parts[2] != "runtime-watch":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        run_id = parts[1]
        owner = identity.username
        try:
            watch = self.store.get_watch_for_run(run_id, owner=owner)
            if len(parts) == 3:
                if params:
                    raise ValueError("query parameters are not supported")
                return ApiResponse(
                    status=200,
                    payload={
                        "watch_id": watch.watch_id,
                        "run_id": watch.run_id,
                        "state": watch.state.value,
                        "next_poll_at": watch.next_poll_at,
                        "updated_at": watch.updated_at,
                        "streams": {
                            cursor.stream: {
                                "generation": cursor.generation,
                                "offset": cursor.offset,
                                "last_data_at": cursor.last_data_at,
                                "last_checked_at": cursor.last_checked_at,
                                "quiet_polls": cursor.quiet_polls,
                            }
                            for cursor in watch.cursors
                        },
                        "alert_count": len(
                            self.store.list_alerts(run_id, owner=owner, limit=1000)
                        ),
                    },
                )
            if len(parts) == 4 and parts[3] == "alerts":
                if params:
                    raise ValueError("query parameters are not supported")
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [
                            {
                                "alert_id": item.alert_id,
                                "code": item.code,
                                "severity": item.severity,
                                "summary": item.summary,
                                "generation": item.generation,
                                "offset": item.offset,
                                "created_at": item.created_at,
                            }
                            for item in self.store.list_alerts(
                                run_id, owner=owner, limit=1000
                            )
                        ]
                    },
                )
            if len(parts) == 4 and parts[3] == "logs":
                return self._logs(run_id, owner=owner, params=params)
        except KeyError:
            return _error(404, "RUNTIME_WATCH.NOT_FOUND", "Runtime Watch not found")
        except ValueError as exc:
            return _error(400, "RUNTIME_WATCH.INVALID_REQUEST", str(exc))
        return None

    def _logs(
        self,
        run_id: str,
        *,
        owner: str,
        params: Mapping[str, list[str]],
    ) -> ApiResponse:
        allowed = {"stream", "cursor", "max_bytes"}
        if set(params) - allowed:
            raise ValueError("query parameters are invalid")
        stream_value = _one(params, "stream")
        if stream_value not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        stream = cast(RuntimeLogStream, stream_value)
        max_bytes = int(_one(params, "max_bytes", default="65536"))
        if not 1 <= max_bytes <= 256 * 1024:
            raise ValueError("max_bytes must be between 1 and 262144")
        generation, offset = _decode_cursor(_one(params, "cursor", default=""))
        segments = self.store.list_segments_from(
            run_id,
            owner=owner,
            stream=stream,
            generation=generation,
            offset=offset,
            limit=1000,
        )
        selected = bytearray()
        next_generation, next_offset = generation, offset
        for segment in segments:
            if segment.generation < generation or (
                segment.generation == generation and segment.end_offset <= offset
            ):
                continue
            start = max(segment.start_offset, offset if segment.generation == generation else 0)
            content = self.store.read_segment_content(segment.segment_id, owner=owner)
            relative = start - segment.start_offset
            take = content[relative : relative + max_bytes - len(selected)]
            selected.extend(take)
            next_generation = segment.generation
            next_offset = start + len(take)
            if len(selected) >= max_bytes:
                break
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(bytes(selected), final=False)
        remainder, _ = decoder.getstate()
        if remainder:
            del selected[-len(remainder) :]
            next_offset -= len(remainder)
        return ApiResponse(
            status=200,
            payload={
                "run_id": run_id,
                "stream": stream,
                "content": text,
                "bytes": len(selected),
                "next_cursor": _encode_cursor(next_generation, next_offset),
            },
        )


def _one(params: Mapping[str, list[str]], key: str, *, default: str | None = None) -> str:
    values = params.get(key)
    if values is None:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    if len(values) != 1 or not values[0]:
        raise ValueError(f"{key} is invalid")
    return values[0]


def _encode_cursor(generation: int, offset: int) -> str:
    raw = json.dumps({"v": 1, "g": generation, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[int, int]:
    if not value:
        return 0, 0
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        generation, offset = payload["g"], payload["o"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if (
        payload.get("v") != 1
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise ValueError("cursor is invalid")
    return generation, offset


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
