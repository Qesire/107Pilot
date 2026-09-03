from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1))


Path("apps/web/src/files/useFileDirectoryListing.ts").write_text(r'''import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "../api";
import type { FileEntry } from "../types";

export const FILE_DIRECTORY_PAGE_SIZE = 500;

export function fileDirectoryQueryKey(user: string, cwd: string) {
  return ["files-list", user, cwd] as const;
}

export function useFileDirectoryListing(
  user: string,
  cwd: string,
  pageSize = FILE_DIRECTORY_PAGE_SIZE,
) {
  const listing = useInfiniteQuery({
    queryKey: fileDirectoryQueryKey(user, cwd),
    queryFn: ({ signal, pageParam }) => api.fileList(
      user,
      cwd,
      { limit: pageSize, cursor: pageParam },
      signal,
    ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
    retry: false,
  });

  const entries = useMemo<FileEntry[]>(
    () => listing.data?.pages.flatMap((page) => page.entries) ?? [],
    [listing.data],
  );

  return {
    ...listing,
    entries,
    loadedCount: entries.length,
    error: listing.error as Error | null,
  };
}
''')

replace_once(
    "apps/web/src/files/useFilePane.ts",
    'import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";\n',
    'import { useMutation, useQueryClient } from "@tanstack/react-query";\n',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    'import { useFilesManager, type PaneViewMode } from "./FilesManagerContext";\n',
    'import { useFilesManager, type PaneViewMode } from "./FilesManagerContext";\nimport { fileDirectoryQueryKey, useFileDirectoryListing } from "./useFileDirectoryListing";\n',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''  const listing = useInfiniteQuery({
    queryKey: ["files-list", manager.user, cwd],
    queryFn: ({ signal, pageParam }) => api.fileList(
      manager.user,
      cwd,
      { limit: 500, cursor: pageParam },
      signal,
    ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
    retry: false,
  });

''',
    '''  const listing = useFileDirectoryListing(manager.user, cwd);

''',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''      void queryClient.invalidateQueries({
        queryKey: ["files-list", manager.user, path],
        exact: true,
      });
''',
    '''      void queryClient.invalidateQueries({
        queryKey: fileDirectoryQueryKey(manager.user, path),
        exact: true,
      });
''',
)
replace_once(
    "apps/web/src/files/useFilePane.ts",
    '''  const entries = useMemo(
    () => listing.data?.pages.flatMap((page) => page.entries) ?? [],
    [listing.data],
  );

''',
    '''  const entries = listing.entries;

''',
)

Path("apps/web/src/files/FilePickerDialog.tsx").write_text(r'''import { ArrowUp, Check, File, Folder, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { clampToHome, normalizeDir, parentPath } from "./selection";
import { PathBar } from "./PathBar";
import { useFileDirectoryListing } from "./useFileDirectoryListing";
import { useVirtualWindow } from "./virtualization";

export type FilePickerSelectionMode = "directory" | "file" | "path";

interface FilePickerDialogProps {
  user: string;
  homePath: string;
  initialPath?: string;
  title: string;
  selectionMode?: FilePickerSelectionMode;
  onSelect: (path: string) => void;
  onClose: () => void;
}

const PICKER_ROW_HEIGHT = 44;

export function FilePickerDialog({
  user,
  homePath,
  initialPath,
  title,
  selectionMode = "directory",
  onSelect,
  onClose,
}: FilePickerDialogProps) {
  const home = useMemo(() => normalizeDir(homePath), [homePath]);
  const [cwd, setCwd] = useState(() => clampToHome(initialPath || home, home));
  const [filter, setFilter] = useState("");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  const listing = useFileDirectoryListing(user, cwd);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    setSelectedPath(null);
    setFilter("");
    if (scrollElement) scrollElement.scrollTop = 0;
  }, [cwd, scrollElement]);

  useEffect(() => {
    if (scrollElement) scrollElement.scrollTop = 0;
  }, [filter, scrollElement]);

  const entries = useMemo(() => {
    const normalizedFilter = filter.trim().toLocaleLowerCase("zh-CN");
    return listing.entries
      .filter((entry) => entry.kind === "directory" || selectionMode !== "directory")
      .filter((entry) => !normalizedFilter || entry.name.toLocaleLowerCase("zh-CN").includes(normalizedFilter));
  }, [filter, listing.entries, selectionMode]);
  const range = useVirtualWindow(scrollElement, entries.length, PICKER_ROW_HEIGHT, 8);
  const visibleEntries = entries.slice(range.start, range.end);

  const navigate = (path: string) => setCwd(clampToHome(path, home));
  const targetPath = selectedPath ?? (selectionMode === "file" ? null : cwd);
  const targetLabel = selectedPath
    ? "选择此文件"
    : selectionMode === "file"
      ? "请选择一个文件"
      : "选择此目录";

  return (
    <div className="file-picker-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section className="file-picker-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <header className="file-picker-header">
          <div>
            <span>集群文件系统 · {selectionMode === "directory" ? "目录" : selectionMode === "file" ? "文件" : "文件或目录"}</span>
            <h2>{title}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="关闭文件选择器" onClick={onClose}><X aria-hidden="true" /></button>
        </header>
        <div className="file-picker-pathbar">
          <button type="button" className="icon-button" aria-label="返回上级目录" disabled={cwd === home} onClick={() => navigate(parentPath(cwd))}>
            <ArrowUp aria-hidden="true" />
          </button>
          <PathBar
            cwd={cwd}
            home={home}
            isPending={listing.isPending}
            isError={listing.isError}
            onNavigate={navigate}
          />
        </div>
        <div className="file-picker-filter">
          <Search aria-hidden="true" />
          <input
            type="search"
            aria-label="筛选当前目录"
            value={filter}
            placeholder="筛选当前目录中的文件或文件夹"
            onChange={(event) => setFilter(event.target.value)}
          />
          {listing.hasNextPage ? <small>{filter ? `筛选已加载的 ${listing.loadedCount} 项；目录仍有更多内容` : `已加载 ${listing.loadedCount} 项；目录仍有更多内容`}</small> : null}
        </div>
        <div className="file-picker-body" ref={setScrollElement}>
          {listing.isPending ? <div className="file-picker-state">正在读取服务器目录…</div> : null}
          {listing.isError ? <div className="file-picker-state error" role="alert">无法读取目录：{listing.error?.message ?? "未知错误"}</div> : null}
          {!listing.isPending && !listing.isError && entries.length === 0 ? (
            <div className="file-picker-state">{filter ? "当前筛选没有匹配项。" : selectionMode === "directory" ? "当前目录没有子目录。" : "当前目录没有可选择对象。"}</div>
          ) : null}
          {entries.length > 0 ? (
            <ul className="file-picker-list virtual-picker-list" aria-setsize={entries.length}>
              {range.paddingBefore > 0 ? <li className="virtual-picker-spacer" aria-hidden="true" style={{ height: range.paddingBefore }} /> : null}
              {visibleEntries.map((entry, localIndex) => {
                const isDirectory = entry.kind === "directory";
                const selected = !isDirectory && selectedPath === entry.path;
                return (
                  <li
                    key={entry.path}
                    className="virtual-picker-item"
                    aria-posinset={range.start + localIndex + 1}
                    aria-setsize={entries.length}
                  >
                    <button
                      type="button"
                      className={`file-picker-entry${selected ? " is-selected" : ""}`}
                      aria-pressed={!isDirectory ? selected : undefined}
                      onClick={() => {
                        if (isDirectory) navigate(entry.path);
                        else setSelectedPath(entry.path);
                      }}
                    >
                      {isDirectory ? <Folder aria-hidden="true" /> : <File aria-hidden="true" />}
                      <span>{entry.name}</span>
                      <small>{isDirectory ? "打开目录" : selected ? "已选择" : "选择文件"}</small>
                    </button>
                  </li>
                );
              })}
              {range.paddingAfter > 0 ? <li className="virtual-picker-spacer" aria-hidden="true" style={{ height: range.paddingAfter }} /> : null}
            </ul>
          ) : null}
          {listing.hasNextPage ? (
            <button type="button" className="button secondary file-picker-load-more" disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>
              {listing.isFetchingNextPage ? "正在加载更多…" : "加载更多目录内容"}
            </button>
          ) : null}
        </div>
        <footer className="file-picker-footer">
          <div>
            {selectedPath ? <File aria-hidden="true" /> : <Folder aria-hidden="true" />}
            <code title={targetPath ?? cwd}>{targetPath ?? "尚未选择文件"}</code>
          </div>
          <div className="file-picker-actions">
            <button type="button" className="button secondary" onClick={onClose}>取消</button>
            <button type="button" className="button primary" disabled={!targetPath} onClick={() => targetPath && onSelect(targetPath)}>
              <Check aria-hidden="true" size={15} />{targetLabel}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
''')

css_path = Path("apps/web/src/styles/file-picker-v2.css")
css = css_path.read_text()
css_marker = '''.file-picker-list {
  display: grid;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
}
'''
if css.count(css_marker) != 1:
    raise RuntimeError("file-picker-v2.css: picker list marker mismatch")
css = css.replace(css_marker, '''.file-picker-list {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.virtual-picker-item {
  height: 44px;
  min-height: 44px;
}

.virtual-picker-spacer {
  display: block;
  min-height: 0;
  pointer-events: none;
}
''', 1)
css = css.replace('''  min-height: 42px;
''', '''  height: 44px;
  min-height: 44px;
''', 1)
css += '''\n.file-picker-pathbar .filepane-breadcrumb { min-width: 0; }\n.file-picker-pathbar .path-bar-form { min-width: 0; }\n'''
css_path.write_text(css)

visual = Path("tests/ui/visual.spec.js")
text = visual.read_text()
old_api = '''    if (url.pathname === "/api/v1/files") {
      const currentPath = url.searchParams.get("path") || "/public/home/alice";
      return json(route, {
        path: currentPath,
        entries: currentPath === "/public/home/alice"
          ? [
              { name: "project-a", type: "directory", size: 0, mtime: 1788408000 },
              { name: "dataset.tar.gz", type: "file", size: 100, mtime: 1788408000 },
            ]
          : [],
        page: { limit: Number(url.searchParams.get("limit") || 500), has_more: false, next_cursor: null },
        directory_revision: "visual-fixture-v1",
      });
    }
'''
new_api = '''    if (url.pathname === "/api/v1/files") {
      const currentPath = url.searchParams.get("path") || "/public/home/alice";
      const limit = Number(url.searchParams.get("limit") || 500);
      const offset = Number(url.searchParams.get("cursor") || 0);
      const allEntries = currentPath === "/public/home/alice"
        ? [
            { name: "project-a", type: "directory", size: 0, mtime: 1788408000 },
            { name: "picker-large", type: "directory", size: 0, mtime: 1788408000 },
            { name: "dataset.tar.gz", type: "file", size: 100, mtime: 1788408000 },
          ]
        : currentPath === "/public/home/alice/picker-large"
          ? Array.from({ length: 1200 }, (_, index) => ({
              name: `dir-${String(index).padStart(4, "0")}`,
              type: "directory",
              size: 0,
              mtime: 1788408000 + index,
            }))
          : [];
      const entries = allEntries.slice(offset, offset + limit);
      const nextOffset = offset + entries.length;
      const hasMore = nextOffset < allEntries.length;
      return json(route, {
        path: currentPath,
        entries,
        page: { limit, has_more: hasMore, next_cursor: hasMore ? String(nextOffset) : null },
        directory_revision: "visual-fixture-v2",
      });
    }
'''
if text.count(old_api) != 1:
    raise RuntimeError("visual.spec.js: file API fixture mismatch")
text = text.replace(old_api, new_api, 1)
anchor = '''test("recipe shared_path browses existing backend files and writes canonical field", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览 runtime.environment.DATA_ROOT" }).click();
  await expect(page.getByRole("dialog", { name: /选择共享路径/ })).toBeVisible();
  await page.getByRole("button", { name: /dataset.tar.gz/ }).click();
  await page.getByRole("button", { name: "选择此文件" }).click();
  await expect(page.getByRole("textbox", { name: /^runtime\\.environment\\.DATA_ROOT/ })).toHaveValue("/public/home/alice/dataset.tar.gz");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
});
'''
extra = anchor + '''\ntest("studio picker keeps a bounded DOM while paging a large directory", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await page.getByRole("button", { name: "浏览工作目录" }).click();
  const dialog = page.getByRole("dialog", { name: "选择实验工作目录" });
  await dialog.getByRole("button", { name: /picker-large/ }).click();
  await expect(dialog.getByText(/已加载 500 项；目录仍有更多内容/)).toBeVisible();
  expect(await dialog.locator(".file-picker-entry").count()).toBeLessThan(80);

  await dialog.getByRole("button", { name: "加载更多目录内容" }).click();
  await expect(dialog.getByText(/已加载 1000 项；目录仍有更多内容/)).toBeVisible();
  expect(await dialog.locator(".file-picker-entry").count()).toBeLessThan(80);
});
'''
if text.count(anchor) != 1:
    raise RuntimeError("visual.spec.js: shared_path test anchor mismatch")
text = text.replace(anchor, extra, 1)
visual.write_text(text)

print("A6 shared picker core staged")
