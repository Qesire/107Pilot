import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Check, Folder, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { clampToHome, normalizeDir, parentPath } from "./selection";

interface FilePickerDialogProps {
  user: string;
  homePath: string;
  initialPath?: string;
  title: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}

export function FilePickerDialog({ user, homePath, initialPath, title, onSelect, onClose }: FilePickerDialogProps) {
  const home = useMemo(() => normalizeDir(homePath), [homePath]);
  const [cwd, setCwd] = useState(() => clampToHome(initialPath || home, home));
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

  const directories = useMemo(
    () => (listing.data?.entries ?? [])
      .filter((entry) => entry.kind === "directory")
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN", { numeric: true })),
    [listing.data],
  );

  const navigate = (path: string) => setCwd(clampToHome(path, home));

  return (
    <div className="file-picker-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section className="file-picker-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <header className="file-picker-header">
          <div><span>集群文件系统</span><h2>{title}</h2></div>
          <button type="button" className="icon-button" aria-label="关闭文件选择器" onClick={onClose}><X aria-hidden="true" /></button>
        </header>
        <div className="file-picker-pathbar">
          <button type="button" className="icon-button" aria-label="返回上级目录" disabled={cwd === home} onClick={() => navigate(parentPath(cwd))}>
            <ArrowUp aria-hidden="true" />
          </button>
          <code title={cwd}>{cwd}</code>
        </div>
        <div className="file-picker-body">
          {listing.isPending ? <div className="file-picker-state">正在读取服务器目录…</div> : null}
          {listing.isError ? <div className="file-picker-state error" role="alert">无法读取目录：{listing.error instanceof Error ? listing.error.message : "未知错误"}</div> : null}
          {!listing.isPending && !listing.isError && directories.length === 0 ? <div className="file-picker-state">当前目录没有子目录。</div> : null}
          {directories.length > 0 ? <ul className="file-picker-list">{directories.map((entry) => (
            <li key={entry.path}>
              <button type="button" className="file-picker-entry" onClick={() => navigate(entry.path)}>
                <Folder aria-hidden="true" /><span>{entry.name}</span><small>打开目录</small>
              </button>
            </li>
          ))}</ul> : null}
        </div>
        <footer className="file-picker-footer">
          <div><Folder aria-hidden="true" /><code>{cwd}</code></div>
          <div className="file-picker-actions">
            <button type="button" className="button secondary" onClick={onClose}>取消</button>
            <button type="button" className="button primary" onClick={() => onSelect(cwd)}><Check aria-hidden="true" size={15} />选择此目录</button>
          </div>
        </footer>
      </section>
    </div>
  );
}
