"""Adapters from paged file operations to bounded Workspace source reads."""

from __future__ import annotations

from pilot107.adapters.slurm import (
    FileEntry,
    FileOpsExecutor,
    FileStat,
    SlurmSubmissionRejected,
    SlurmTransportError,
)


class PagedWorkspaceSourceReader:
    """Expose the legacy WorkspaceSourceReader shape over paged file listings.

    Workspace import intentionally remains a bounded snapshot operation.  The
    visual filesystem, however, must never require an unbounded directory
    response.  This adapter consumes stable cursor pages and applies a hard
    per-directory cap before returning the list expected by WorkspaceImporter.
    """

    def __init__(
        self,
        executor: FileOpsExecutor,
        *,
        page_size: int = 500,
        max_directory_entries: int = 10_000,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be within 1..1000")
        if max_directory_entries < 1:
            raise ValueError("max_directory_entries must be positive")
        self.executor = executor
        self.page_size = page_size
        self.max_directory_entries = max_directory_entries

    def list_dir(
        self,
        *,
        path: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> list[FileEntry]:
        entries: list[FileEntry] = []
        cursor: str | None = None
        revision: str | None = None
        while True:
            remaining = self.max_directory_entries - len(entries)
            if remaining <= 0:
                raise SlurmSubmissionRejected("workspace source directory exceeds maximum entries")
            page = self.executor.list_dir(
                path=path,
                owner=owner,
                limit=min(self.page_size, remaining),
                cursor=cursor,
                timeout_seconds=timeout_seconds,
            )
            if revision is None:
                revision = page.directory_revision
            elif page.directory_revision != revision:
                raise SlurmTransportError("workspace source directory changed during paged listing")
            entries.extend(page.entries)
            if not page.has_more:
                return entries
            if not page.next_cursor:
                raise SlurmTransportError("paged directory listing omitted the continuation cursor")
            cursor = page.next_cursor

    def stat_path(
        self,
        *,
        path: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> FileStat:
        return self.executor.stat_path(
            path=path,
            owner=owner,
            timeout_seconds=timeout_seconds,
        )

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        return self.executor.read_bytes_chunk(
            path=path,
            offset=offset,
            length=length,
            owner=owner,
            timeout_seconds=timeout_seconds,
        )

    def file_sha256(
        self,
        *,
        path: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        return self.executor.file_sha256(
            path=path,
            owner=owner,
            timeout_seconds=timeout_seconds,
        )
