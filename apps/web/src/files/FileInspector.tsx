import { Copy, File, Folder, Link2 } from "lucide-react";
import type { FileEntry } from "../types";
import { useFilesManager } from "./FilesManagerContext";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function kindLabel(kind: FileEntry["kind"]): string {
  if (kind === "directory") return "目录";
  if (kind === "symlink") return "符号链接";
  return "文件";
}

function EntryIcon({ entry }: { entry: FileEntry }) {
  if (entry.kind === "directory") return <Folder size={18} aria-hidden="true" />;
  if (entry.kind === "symlink") return <Link2 size={18} aria-hidden="true" />;
  return <File size={18} aria-hidden="true" />;
}

function CopyPathButton({ path }: { path: string }) {
  return (
    <button
      type="button"
      className="file-inspector-copy"
      title="复制路径"
      aria-label="复制路径"
      onClick={() => { void navigator.clipboard?.writeText(path); }}
    >
      <Copy size={14} aria-hidden="true" />
    </button>
  );
}

export function FileInspector() {
  const manager = useFilesManager();
  const selected = manager.activeSelection;

  if (selected.length === 0) {
    return (
      <aside className="file-inspector" aria-label="文件属性">
        <div className="file-rail-heading">
          <span>属性</span>
          <small>当前目录</small>
        </div>
        <dl className="file-inspector-list">
          <div>
            <dt>位置</dt>
            <dd className="file-inspector-path">
              <span title={manager.activePath}>{manager.activePath}</span>
              <CopyPathButton path={manager.activePath} />
            </dd>
          </div>
        </dl>
        <p className="file-inspector-hint">选择文件或目录以查看属性。运行来源与实验关联将在资产链接阶段接入。</p>
      </aside>
    );
  }

  if (selected.length > 1) {
    const fileBytes = selected.reduce((total, entry) => total + (entry.kind === "file" ? entry.size : 0), 0);
    return (
      <aside className="file-inspector" aria-label="文件属性">
        <div className="file-rail-heading">
          <span>属性</span>
          <small>多选</small>
        </div>
        <div className="file-inspector-summary">
          <strong>{selected.length} 项已选</strong>
          <span>文件合计 {formatSize(fileBytes)}</span>
        </div>
        <p className="file-inspector-hint">批量操作仍从当前窗格的选择操作栏执行。</p>
      </aside>
    );
  }

  const entry = selected[0]!;
  return (
    <aside className="file-inspector" aria-label="文件属性">
      <div className="file-rail-heading">
        <span>属性</span>
        <small>{kindLabel(entry.kind)}</small>
      </div>
      <div className="file-inspector-entry">
        <EntryIcon entry={entry} />
        <strong title={entry.name}>{entry.name}</strong>
      </div>
      <dl className="file-inspector-list">
        <div><dt>类型</dt><dd>{kindLabel(entry.kind)}</dd></div>
        <div><dt>大小</dt><dd>{entry.kind === "file" ? formatSize(entry.size) : "—"}</dd></div>
        <div><dt>修改时间</dt><dd>{entry.modified || "—"}</dd></div>
        <div>
          <dt>路径</dt>
          <dd className="file-inspector-path">
            <span title={entry.path}>{entry.path}</span>
            <CopyPathButton path={entry.path} />
          </dd>
        </div>
      </dl>
    </aside>
  );
}
