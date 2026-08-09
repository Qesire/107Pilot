// Properties/details dialog for a single entry (右键菜单“属性”). Shows name,
// path, type, size and modification time; directories additionally report
// their entry count via the shared files-list query. Overlay/panel styling is
// reused from the MoveDialog language (modal-overlay + modal-card).

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Folder, File as FileIcon } from "lucide-react";
import { api } from "../api";
import { formatTimestamp } from "../components";
import { formatStorageBytes } from "../resource-summary";
import type { FileEntry } from "../types";

export function EntryInfoDialog({
  user,
  entry,
  onClose,
}: {
  user: string;
  entry: FileEntry;
  onClose: () => void;
}) {
  const isDir = entry.kind === "directory";
  const listing = useQuery({
    queryKey: ["files-list", user, entry.path],
    queryFn: ({ signal }) => api.fileList(user, entry.path, signal),
    retry: false,
    enabled: isDir,
  });

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const rows: Array<[string, string]> = [
    ["名称", entry.name],
    ["路径", entry.path],
    ["类型", isDir ? "目录" : "文件"],
  ];
  if (isDir) {
    const count = listing.isPending
      ? "统计中…"
      : listing.isError
        ? "不可用"
        : `${listing.data?.entries.length ?? 0} 项`;
    rows.push(["包含", count]);
  } else {
    rows.push(["大小", formatStorageBytes(entry.size)]);
  }
  rows.push(["修改时间", formatTimestamp(entry.modified)]);

  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal-card entry-info-dialog" role="dialog" aria-modal="true" aria-label="属性">
        <header className="move-dialog-header">
          {isDir ? (
            <Folder size={15} aria-hidden="true" className="icon-folder" />
          ) : (
            <FileIcon size={15} aria-hidden="true" />
          )}
          <span>{entry.name} 的属性</span>
        </header>
        <dl className="entry-info-rows">
          {rows.map(([label, value]) => (
            <div className="entry-info-row" key={label}>
              <dt>{label}</dt>
              <dd title={value}>{value}</dd>
            </div>
          ))}
        </dl>
        <footer className="move-dialog-footer">
          <span className="move-dialog-actions">
            <button type="button" className="button primary" autoFocus onClick={onClose}>
              关闭
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
