from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, got {count}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "apps/web/src/types.ts",
    '''export interface FileListResponse {\n  path: string;\n  entries: FileEntry[];\n}\n''',
    '''export interface FileListResponse {\n  path: string;\n  entries: FileEntry[];\n  page: PageInfo;\n  directory_revision: string;\n}\n''',
)

replace_once(
    "apps/web/src/api.ts",
    '''  fileList: async (user: string, path: string, signal?: AbortSignal) => {\n    const raw = await getJson<{\n      path: string;\n      entries: Array<{ name: string; type: string; size: number; mtime: number }>;\n    }>(queryPath("/api/v1/files", { path }), user, signal);\n    const base = raw.path.replace(/\\/+$/, "");\n    return {\n      path: raw.path,\n      entries: raw.entries.map((e) => ({\n        name: e.name,\n        path: `${base}/${e.name}`,\n        kind: (e.type === "dir" ? "directory" : e.type) as FileEntry["kind"],\n        size: e.size,\n        modified: e.mtime > 0 ? new Date(e.mtime * 1000).toISOString() : "",\n      })),\n    } satisfies FileListResponse;\n  },\n''',
    '''  fileList: async (\n    user: string,\n    path: string,\n    input: { limit?: number; cursor?: string | null } = {},\n    signal?: AbortSignal,\n  ) => {\n    const raw = await getJson<{\n      path: string;\n      entries: Array<{ name: string; type: string; size: number; mtime: number }>;\n      page: PageInfo;\n      directory_revision: string;\n    }>(queryPath("/api/v1/files", {\n      path,\n      limit: String(input.limit ?? 500),\n      cursor: input.cursor ?? undefined,\n    }), user, signal);\n    const base = raw.path.replace(/\\/+$/, "");\n    return {\n      path: raw.path,\n      entries: raw.entries.map((e) => ({\n        name: e.name,\n        path: `${base}/${e.name}`,\n        kind: (e.type === "dir" ? "directory" : e.type) as FileEntry["kind"],\n        size: e.size,\n        modified: e.mtime > 0 ? new Date(e.mtime * 1000).toISOString() : "",\n      })),\n      page: raw.page,\n      directory_revision: raw.directory_revision,\n    } satisfies FileListResponse;\n  },\n''',
)

p = Path("apps/web/src/api.ts")
text = p.read_text(encoding="utf-8")
if "  PageInfo,\n" not in text.split('} from "./types";', 1)[0]:
    marker = "  PagePayload,\n"
    if marker not in text:
        raise RuntimeError("PagePayload import marker missing")
    p.write_text(text.replace(marker, marker + "  PageInfo,\n", 1), encoding="utf-8")

replace_once(
    "apps/web/src/files/useFilePane.ts",
    'import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";\n',
    'import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";\n',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''  isFetching: boolean;\n  error: Error | null;\n''',
    '''  isFetching: boolean;\n  hasNextPage: boolean;\n  isFetchingNextPage: boolean;\n  fetchNextPage: () => void;\n  error: Error | null;\n''',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''  sortEntries,\n  toggleSelection,\n''',
    '''  toggleSelection,\n''',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''  const listing = useQuery({\n    queryKey: ["files-list", manager.user, cwd],\n    queryFn: ({ signal }) => api.fileList(manager.user, cwd, signal),\n    retry: false,\n  });\n''',
    '''  const listing = useInfiniteQuery({\n    queryKey: ["files-list", manager.user, cwd],\n    queryFn: ({ signal, pageParam }) => api.fileList(\n      manager.user,\n      cwd,\n      { limit: 500, cursor: pageParam ?? undefined },\n      signal,\n    ),\n    initialPageParam: null as string | null,\n    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,\n    retry: false,\n  });\n''',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''  const entries = useMemo(\n    () => sortEntries(listing.data?.entries ?? []),\n    [listing.data],\n  );\n''',
    '''  const entries = useMemo(\n    () => listing.data?.pages.flatMap((page) => page.entries) ?? [],\n    [listing.data],\n  );\n''',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''    isFetching: listing.isFetching,\n    error: listing.error as Error | null,\n''',
    '''    isFetching: listing.isFetching,\n    hasNextPage: Boolean(listing.hasNextPage),\n    isFetchingNextPage: listing.isFetchingNextPage,\n    fetchNextPage: () => { void listing.fetchNextPage(); },\n    error: listing.error as Error | null,\n''',
)

replace_once(
    "apps/web/src/files/FilePane.tsx",
    '''      <footer className="filepane-status">\n        <span>{pane.entries.length} 项</span>\n''',
    '''      <footer className="filepane-status">\n        <span>{pane.entries.length} 项已加载{pane.hasNextPage ? " · 还有更多" : ""}</span>\n        {pane.viewMode !== "column" && pane.hasNextPage ? (\n          <button type="button" className="text-link" disabled={pane.isFetchingNextPage} onClick={pane.fetchNextPage}>\n            {pane.isFetchingNextPage ? "加载中…" : "加载更多"}\n          </button>\n        ) : null}\n''',
)

replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    'import { useQuery } from "@tanstack/react-query";\n',
    'import { useInfiniteQuery } from "@tanstack/react-query";\n',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    'const MAX_RENDERED_ENTRIES = 300;\n',
    'const PICKER_PAGE_SIZE = 500;\n',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    '''  const listing = useQuery({\n    queryKey: ["files-list", user, cwd],\n    queryFn: ({ signal }) => api.fileList(user, cwd, signal),\n    retry: false,\n  });\n''',
    '''  const listing = useInfiniteQuery({\n    queryKey: ["files-list", user, cwd],\n    queryFn: ({ signal, pageParam }) => api.fileList(\n      user, cwd, { limit: PICKER_PAGE_SIZE, cursor: pageParam ?? undefined }, signal\n    ),\n    initialPageParam: null as string | null,\n    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,\n    retry: false,\n  });\n''',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    '''    return (listing.data?.entries ?? [])\n      .filter((entry) => entry.kind === "directory" || selectionMode !== "directory")\n      .filter((entry) => !normalizedFilter || entry.name.toLocaleLowerCase("zh-CN").includes(normalizedFilter))\n      .sort((a, b) => {\n        if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;\n        return a.name.localeCompare(b.name, "zh-CN", { numeric: true });\n      });\n''',
    '''    return (listing.data?.pages.flatMap((page) => page.entries) ?? [])\n      .filter((entry) => entry.kind === "directory" || selectionMode !== "directory")\n      .filter((entry) => !normalizedFilter || entry.name.toLocaleLowerCase("zh-CN").includes(normalizedFilter));\n''',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    '  const visibleEntries = entries.slice(0, MAX_RENDERED_ENTRIES);\n',
    '  const loadedCount = listing.data?.pages.reduce((count, page) => count + page.entries.length, 0) ?? 0;\n',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    '''          {entries.length > MAX_RENDERED_ENTRIES ? <small>当前匹配 {entries.length} 项，仅渲染前 {MAX_RENDERED_ENTRIES} 项</small> : null}\n''',
    '''          {listing.hasNextPage ? <small>{filter ? `筛选已加载的 ${loadedCount} 项；目录仍有更多内容` : `已加载 ${loadedCount} 项；目录仍有更多内容`}</small> : null}\n''',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    '          {visibleEntries.length > 0 ? <ul className="file-picker-list">{visibleEntries.map((entry) => {\n',
    '          {entries.length > 0 ? <ul className="file-picker-list">{entries.map((entry) => {\n',
)
replace_once(
    "apps/web/src/files/FilePickerDialog.tsx",
    '''          })}</ul> : null}\n        </div>\n''',
    '''          })}</ul> : null}\n          {listing.hasNextPage ? (\n            <button type="button" className="button secondary file-picker-load-more" disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>\n              {listing.isFetchingNextPage ? "正在加载更多…" : "加载更多目录内容"}\n            </button>\n          ) : null}\n        </div>\n''',
)

replace_once(
    "apps/web/src/files/MillerColumns.tsx",
    'import { useQuery } from "@tanstack/react-query";\n',
    'import { useInfiniteQuery } from "@tanstack/react-query";\n',
)
replace_once(
    "apps/web/src/files/MillerColumns.tsx",
    'import { columnDirsFor, sortEntries } from "./selection";\n',
    'import { columnDirsFor } from "./selection";\n',
)
replace_once(
    "apps/web/src/files/MillerColumns.tsx",
    '''  const listing = useQuery({\n    queryKey: ["files-list", user, dir],\n    queryFn: ({ signal }) => api.fileList(user, dir, signal),\n    retry: false,\n  });\n  const entries = useMemo(\n    () => sortEntries(listing.data?.entries ?? []),\n    [listing.data],\n  );\n''',
    '''  const listing = useInfiniteQuery({\n    queryKey: ["files-list", user, dir],\n    queryFn: ({ signal, pageParam }) => api.fileList(user, dir, { limit: 500, cursor: pageParam ?? undefined }, signal),\n    initialPageParam: null as string | null,\n    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,\n    retry: false,\n  });\n  const entries = useMemo(\n    () => listing.data?.pages.flatMap((page) => page.entries) ?? [],\n    [listing.data],\n  );\n''',
)
replace_once(
    "apps/web/src/files/MillerColumns.tsx",
    '''        })}\n      </div>\n''',
    '''        })}\n        {listing.hasNextPage ? (\n          <button type="button" className="miller-load-more" disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>\n            {listing.isFetchingNextPage ? "加载中…" : "加载更多"}\n          </button>\n        ) : null}\n      </div>\n''',
)

replace_once(
    "apps/web/src/files/MoveDialog.tsx",
    'import { useQuery } from "@tanstack/react-query";\n',
    'import { useInfiniteQuery } from "@tanstack/react-query";\n',
)
replace_once(
    "apps/web/src/files/MoveDialog.tsx",
    'import { sortEntries } from "./selection";\n',
    '',
)
replace_once(
    "apps/web/src/files/MoveDialog.tsx",
    '''  const listing = useQuery({\n    queryKey: ["files-list", user, dir],\n    queryFn: ({ signal }) => api.fileList(user, dir, signal),\n    retry: false,\n    enabled: isExpanded,\n  });\n  const subdirs = useMemo(\n    () =>\n      sortEntries(listing.data?.entries ?? []).filter(\n        (entry) => entry.kind === "directory",\n      ),\n    [listing.data],\n  );\n''',
    '''  const listing = useInfiniteQuery({\n    queryKey: ["files-list", user, dir],\n    queryFn: ({ signal, pageParam }) => api.fileList(user, dir, { limit: 500, cursor: pageParam ?? undefined }, signal),\n    initialPageParam: null as string | null,\n    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,\n    retry: false,\n    enabled: isExpanded,\n  });\n  const subdirs = useMemo(\n    () => (listing.data?.pages.flatMap((page) => page.entries) ?? []).filter((entry) => entry.kind === "directory"),\n    [listing.data],\n  );\n''',
)
replace_once(
    "apps/web/src/files/MoveDialog.tsx",
    '''          subdirs.map((entry) => (\n            <MoveTreeNode\n              key={entry.path}\n              user={user}\n              dir={entry.path}\n              depth={depth + 1}\n              dest={dest}\n              expanded={expanded}\n              onToggle={onToggle}\n              onSelect={onSelect}\n            />\n          ))\n        ))}\n''',
    '''          <>\n            {subdirs.map((entry) => (\n              <MoveTreeNode key={entry.path} user={user} dir={entry.path} depth={depth + 1} dest={dest} expanded={expanded} onToggle={onToggle} onSelect={onSelect} />\n            ))}\n            {listing.hasNextPage ? (\n              <button type="button" className="move-tree-loading" style={{ paddingLeft: `${24 + depth * 16}px` }} disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>\n                {listing.isFetchingNextPage ? "加载中…" : "加载更多目录"}\n              </button>\n            ) : null}\n          </>\n        ))}\n''',
)

# Upgrade Playwright file mocks to the paged API contract.
for path in ("tests/ui/files.spec.js", "tests/ui/visual.spec.js"):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    marker = '        entries: '
    # Add fields immediately before the close of each /api/v1/files response.
    if path.endswith("files.spec.js"):
        old = '      return json({ path, entries: fs[path] ?? [] });\n'
        new = '      return json({ path, entries: fs[path] ?? [], page: { limit: Number(url.searchParams.get("limit") || 500), has_more: false, next_cursor: null }, directory_revision: "ui-fixture-v1" });\n'
        if old not in text:
            raise RuntimeError("files.spec file-list fixture marker missing")
        text = text.replace(old, new, 1)
    else:
        old = '''          : [],\n      });\n    }\n    if (url.pathname === "/api/v1/recipes/recipe_python_cpu/versions/1.0.0") {\n'''
        new = '''          : [],\n        page: { limit: Number(url.searchParams.get("limit") || 500), has_more: false, next_cursor: null },\n        directory_revision: "visual-fixture-v1",\n      });\n    }\n    if (url.pathname === "/api/v1/recipes/recipe_python_cpu/versions/1.0.0") {\n'''
        if old not in text:
            raise RuntimeError("visual.spec file-list fixture marker missing")
        text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")

print("paged files source migration applied")
