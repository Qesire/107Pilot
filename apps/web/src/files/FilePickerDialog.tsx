import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Check, File, Folder, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { clampToHome, normalizeDir, parentPath } from "./selection";

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

const MAX_RENDERED_ENTRIES = 300;

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
  const listing = useQuery({
    queryKey: ["files-list", user, cwd],
    queryFn: ({ signal }) => api.fileList(user, cwd, signal),
    retry: false,
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    setSelectedPath(null);
    setFilter("");
  }, [cwd]);

  const entries = useMemo(() => {
    const normalizedFilter = filter.trim().toLocaleLowerCase("zh-CN");
    return (listing.data?.entries ?? [])
      .filter((entry) => entry.kind === "directory" || selectionMode !== "directory")
      .filter((entry) => !normalizedFilter || entry.name.toLocaleLowerCase("zh-CN").includes(normalizedFilter))
      .sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
        return a.name.localeCompare(b.name, "zh-CN", { numeric: true });
      });
  }, [filter, listing.data, selectionMode]);

  const visibleEntries = entries.slice(0, MAX_RENDERED_ENTRIES);
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
          <code title={cwd}>{cwd}</code>
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
          {entries.length > MAX_RENDERED_ENTRIES ? <small>当前匹配 {entries.length} 项，仅渲染前 {MAX_RENDERED_ENTRIES} 项</small> : null}
        </div>
        <div className="file-picker-body">
          {listing.isPending ? <div className="file-picker-state">正在读取服务器目录…</div> : null}
          {listing.isError ? <div className="file-picker-state error" role="alert">无法读取目录：{listing.error instanceof Error ? listing.error.message : "未知错误"}</div> : null}
          {!listing.isPending && !listing.isError && entries.length === 0 ? (
            <div className="file-picker-state">{filter ? "当前筛选没有匹配项。" : selectionMode === "directory" ? "当前目录没有子目录。" : "当前目录没有可选择对象。"}</div>
          ) : null}
          {visibleEntries.length > 0 ? <ul className="file-picker-list">{visibleEntries.map((entry) => {
            const isDirectory = entry.kind === "directory";
            const selected = !isDirectory && selectedPath === entry.path;
            return (
              <li key={entry.path}>
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
          })}</ul> : null}
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
