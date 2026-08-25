import { useQuery } from "@tanstack/react-query";
import { File, Folder, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { formatStorageBytes } from "../resource-summary";
import type { FileSearchEntry } from "../types";
import { parentPath } from "./selection";

export interface FileSearchFilters {
  kind: "file" | "directory" | "all";
  sizeMin: string;
  sizeMax: string;
  modifiedFrom: string;
  modifiedTo: string;
}

type SearchRequest = Parameters<typeof api.fileSearch>[1];

function optionalSize(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : undefined;
}

function optionalTimestamp(value: string): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}

/** Build the exact request boundary; null keeps TanStack Query disabled. */
export function buildFileSearchRequest(
  root: string,
  query: string,
  filters: FileSearchFilters,
  cursor: string | null,
): SearchRequest | null {
  const q = query.trim();
  if (!q) return null;
  const sizeMin = optionalSize(filters.sizeMin);
  const sizeMax = optionalSize(filters.sizeMax);
  const modifiedFrom = optionalTimestamp(filters.modifiedFrom);
  const modifiedTo = optionalTimestamp(filters.modifiedTo);
  return {
    root,
    q,
    kind: filters.kind,
    ...(sizeMin === undefined ? {} : { size_min: sizeMin }),
    ...(sizeMax === undefined ? {} : { size_max: sizeMax }),
    ...(modifiedFrom === undefined ? {} : { mtime_from: modifiedFrom }),
    ...(modifiedTo === undefined ? {} : { mtime_to: modifiedTo }),
    limit: 100,
    ...(cursor === null ? {} : { cursor }),
  };
}

/** Replace initial results and append only when an opaque continuation is used. */
export function mergeFileSearchItems(
  current: FileSearchEntry[],
  incoming: FileSearchEntry[],
  cursor: string | null,
): FileSearchEntry[] {
  return cursor === null ? incoming : [...current, ...incoming];
}

export function fileSearchOpenTarget(entry: FileSearchEntry): {
  path: string;
  selectedPath: string | undefined;
} {
  return entry.type === "directory"
    ? { path: entry.path, selectedPath: undefined }
    : { path: parentPath(entry.path), selectedPath: entry.path };
}

function useDebouncedValue(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function modifiedLabel(mtime: number): string {
  if (mtime <= 0) return "—";
  return new Date(mtime * 1000).toLocaleString("zh-CN");
}

function uniqueWarnings(current: string[], incoming: string[]): string[] {
  return [...new Set([...current, ...incoming])];
}

export function FileSearchPanel({
  user,
  root,
  onOpen,
}: {
  user: string;
  root: string;
  onOpen: (entry: FileSearchEntry) => void;
}) {
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<FileSearchFilters>({
    kind: "all",
    sizeMin: "",
    sizeMax: "",
    modifiedFrom: "",
    modifiedTo: "",
  });
  const debouncedQuery = useDebouncedValue(query, 250);
  const signature = useMemo(
    () => JSON.stringify([root, debouncedQuery.trim(), filters]),
    [debouncedQuery, filters, root],
  );
  const [continuation, setContinuation] = useState<{
    signature: string;
    cursor: string;
  } | null>(null);
  const cursor = continuation?.signature === signature ? continuation.cursor : null;
  const request = useMemo(
    () => buildFileSearchRequest(root, debouncedQuery, filters, cursor),
    [cursor, debouncedQuery, filters, root],
  );
  const search = useQuery({
    queryKey: ["files-search", user, request],
    queryFn: ({ signal }) => {
      if (!request) throw new Error("search request is disabled");
      return api.fileSearch(user, request, signal);
    },
    enabled: request !== null,
    retry: false,
  });
  const [resultState, setResultState] = useState<{
    signature: string;
    items: FileSearchEntry[];
    warnings: string[];
  } | null>(null);

  useEffect(() => {
    if (!search.data) return;
    setResultState((current) => {
      const previous = current?.signature === signature ? current : null;
      return {
        signature,
        items: mergeFileSearchItems(previous?.items ?? [], search.data.items, cursor),
        warnings: cursor === null
          ? search.data.warnings
          : uniqueWarnings(previous?.warnings ?? [], search.data.warnings),
      };
    });
  }, [cursor, search.data, signature]);

  const items = resultState?.signature === signature ? resultState.items : [];
  const warnings = resultState?.signature === signature ? resultState.warnings : [];
  const updateFilter = <K extends keyof FileSearchFilters>(
    key: K,
    value: FileSearchFilters[K],
  ) => {
    setContinuation(null);
    setFilters((current) => ({ ...current, [key]: value }));
  };

  return (
    <section className="file-search-panel" aria-label="文件搜索">
      <div className="file-search-main">
        <Search size={16} aria-hidden="true" />
        <input
          type="search"
          value={query}
          aria-label="搜索文件名或路径"
          placeholder="搜索当前窗格中的名称或相对路径…"
          onChange={(event) => {
            setContinuation(null);
            setQuery(event.target.value);
          }}
        />
        <select
          value={filters.kind}
          aria-label="文件类型"
          onChange={(event) =>
            updateFilter("kind", event.target.value as FileSearchFilters["kind"])
          }
        >
          <option value="all">全部类型</option>
          <option value="file">文件</option>
          <option value="directory">目录</option>
        </select>
        <span className="file-search-root" title={root}>{root}</span>
      </div>

      <details className="file-search-filters">
        <summary>更多筛选</summary>
        <div>
          <label>
            最小大小（字节）
            <input
              type="number"
              min="0"
              value={filters.sizeMin}
              onChange={(event) => updateFilter("sizeMin", event.target.value)}
            />
          </label>
          <label>
            最大大小（字节）
            <input
              type="number"
              min="0"
              value={filters.sizeMax}
              onChange={(event) => updateFilter("sizeMax", event.target.value)}
            />
          </label>
          <label>
            修改时间从
            <input
              type="datetime-local"
              value={filters.modifiedFrom}
              onChange={(event) => updateFilter("modifiedFrom", event.target.value)}
            />
          </label>
          <label>
            修改时间至
            <input
              type="datetime-local"
              value={filters.modifiedTo}
              onChange={(event) => updateFilter("modifiedTo", event.target.value)}
            />
          </label>
        </div>
      </details>

      {search.isFetching ? <p className="file-search-state" role="status">正在搜索…</p> : null}
      {search.isError ? (
        <p className="file-search-state error" role="alert">
          搜索失败：{search.error instanceof Error ? search.error.message : "未知错误"}
        </p>
      ) : null}
      {warnings.length > 0 ? (
        <ul className="file-search-warnings" aria-label="搜索警告">
          {warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      ) : null}
      {request && search.isSuccess && !search.isFetching && items.length === 0 ? (
        <p className="file-search-state">没有匹配结果</p>
      ) : null}
      {items.length > 0 ? (
        <ul className="file-search-results" aria-label="搜索结果">
          {items.map((entry) => (
            <li key={entry.path}>
              <button type="button" onClick={() => onOpen(entry)} title={entry.path}>
                {entry.type === "directory"
                  ? <Folder size={16} aria-hidden="true" />
                  : <File size={16} aria-hidden="true" />}
                <span className="file-search-result-path">{entry.relative_path}</span>
                <span>{entry.type === "directory" ? "目录" : "文件"}</span>
                <span>{entry.type === "directory" ? "—" : formatStorageBytes(entry.size)}</span>
                <span>{modifiedLabel(entry.mtime)}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {search.data?.incomplete && search.data.next_cursor ? (
        <button
          type="button"
          className="button secondary file-search-continue"
          disabled={search.isFetching}
          onClick={() => setContinuation({
            signature,
            cursor: search.data.next_cursor as string,
          })}
        >
          继续搜索
        </button>
      ) : null}
    </section>
  );
}
