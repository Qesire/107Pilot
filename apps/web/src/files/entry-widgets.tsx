// Shared per-entry UI widgets for every file view (Miller columns, grid,
// list): the pane-level action surface, the hover action button group and the
// inline rename input. Views stay dumb — all logic lives in FilePane.

import { useEffect, useRef } from "react";
import { Download, FolderInput, PackageOpen, Pencil, Trash2 } from "lucide-react";
import type { FileEntry } from "../types";
import { isArchiveName } from "./selection";
import type { UseFilePaneResult } from "./useFilePane";

/** Every operation the context menu, selection bar and per-row action
 * buttons can trigger. Implemented once in FilePane and passed down. */
export interface PaneActions {
  onOpen: (entry: FileEntry) => void;
  onDownload: (entry: FileEntry) => void;
  onRename: (entry: FileEntry) => void;
  onMove: (paths: string[]) => void;
  onCopy: (paths: string[]) => void;
  onExtract: (entry: FileEntry) => void;
  onDelete: (paths: string[]) => void;
  onArchive: (paths: string[]) => void;
  onMkdir: () => void;
  onCreateFile: () => void;
  onUpload: () => void;
  onRefresh: () => void;
  onSelectAll: () => void;
  onInvertSelection: () => void;
  /** Show the properties/details dialog for one entry. */
  onProperties: (entry: FileEntry) => void;
  /** Open the pane-level context menu. `entry === null` means empty space. */
  onOpenContext: (entry: FileEntry | null, x: number, y: number) => void;
  /** Begin a native HTML5 drag on an entry (move payload shared via the
   * FilesManager so the drop may land in a different pane/view). */
  onDragStartEntry: (entry: FileEntry, e: React.DragEvent) => void;
  /** Drop an internal file payload directly into a known directory entry. */
  onDropIntoDirectory: (entry: FileEntry, e: React.DragEvent) => void;
  /** Drag gesture finished — clears the shared drag payload/highlight. */
  onDragEndEntry: () => void;
}

/** Inline rename input: focuses on mount with the basename (extension
 * excluded) pre-selected. Enter/blur commits, Escape cancels. */
export function InlineRenameInput({
  initialName,
  busy,
  onCommit,
  onCancel,
}: {
  initialName: string;
  busy: boolean;
  onCommit: (newName: string) => void;
  onCancel: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    const dot = initialName.lastIndexOf(".");
    el.setSelectionRange(0, dot > 0 ? dot : initialName.length);
  }, [initialName]);

  const commit = (value: string) => {
    if (doneRef.current) return;
    doneRef.current = true;
    const name = value.trim();
    if (name && name !== initialName) onCommit(name);
    else onCancel();
  };

  return (
    <input
      ref={inputRef}
      className="inline-rename"
      defaultValue={initialName}
      disabled={busy}
      aria-label="重命名"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation(); // keep pane-level Escape/Delete shortcuts out
        if (e.key === "Enter") commit(e.currentTarget.value);
        else if (e.key === "Escape") {
          doneRef.current = true;
          onCancel();
        }
      }}
      onBlur={(e) => commit(e.currentTarget.value)}
    />
  );
}

/** Hover action button group shown on column rows, list rows and grid tiles.
 * Move/delete act on the whole selection when the entry is part of it. */
export function EntryActionButtons({
  entry,
  pane,
  actions,
}: {
  entry: FileEntry;
  pane: UseFilePaneResult;
  actions: PaneActions;
}) {
  const paths = pane.selected.includes(entry.path) ? [...pane.selected] : [entry.path];
  const run = (fn: () => void) => (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    fn();
  };
  return (
    <span className="entry-row-actions" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className="icon-button"
        title="下载"
        onClick={run(() => actions.onDownload(entry))}
      >
        <Download size={13} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="icon-button"
        title="重命名"
        onClick={run(() => actions.onRename(entry))}
      >
        <Pencil size={13} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="icon-button"
        title="移动到…"
        onClick={run(() => actions.onMove(paths))}
      >
        <FolderInput size={13} aria-hidden="true" />
      </button>
      {entry.kind === "file" && isArchiveName(entry.name) && (
        <button
          type="button"
          className="icon-button"
          title="解压到当前目录"
          onClick={run(() => actions.onExtract(entry))}
        >
          <PackageOpen size={13} aria-hidden="true" />
        </button>
      )}
      <button
        type="button"
        className="icon-button danger"
        title="删除"
        onClick={run(() => actions.onDelete(paths))}
      >
        <Trash2 size={13} aria-hidden="true" />
      </button>
    </span>
  );
}
