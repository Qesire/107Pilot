// Right-click context menu for a file pane. Rendered through a portal so it is
// never clipped by pane scroll containers; flips near viewport edges; closes
// on outside mousedown, Escape, scroll (capture) and resize.
//
// Item sets:
//   multi-selection → 打包 / 移动到… / 复制到… / 删除
//   directory       → 打开 / 下载（打包下载）/ 重命名 / 移动到… / 复制到… / 删除 / 属性
//   file            → 下载 / [解压到当前目录] / 重命名 / 移动到… / 复制到… / 删除 / 属性
//   empty space     → 新建目录 / 新建文件 / 上传 / 刷新 / 全选 / 反选

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  CheckCheck,
  Copy,
  Download,
  FileArchive,
  FilePlus2,
  FolderInput,
  FolderOpen,
  FolderPlus,
  Info,
  PackageOpen,
  Pencil,
  RefreshCw,
  Shuffle,
  Trash2,
  Upload,
} from "lucide-react";
import type { FileEntry } from "../types";
import type { PaneActions } from "./entry-widgets";
import { isArchiveName } from "./selection";
import type { UseFilePaneResult } from "./useFilePane";

export interface ContextMenuState {
  x: number;
  y: number;
  /** Right-clicked entry; null means the menu was opened on empty space. */
  target: FileEntry | null;
}

interface MenuItem {
  key: string;
  label: string;
  icon: React.ReactNode;
  danger?: boolean;
  run: () => void;
}

export function FileContextMenu({
  state,
  pane,
  actions,
  onClose,
}: {
  state: ContextMenuState;
  pane: UseFilePaneResult;
  actions: PaneActions;
  onClose: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: state.x, top: state.y });

  // Flip the menu when it would overflow the viewport.
  useLayoutEffect(() => {
    const el = menuRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    let left = state.x;
    let top = state.y;
    if (left + rect.width > window.innerWidth - 4) {
      left = Math.max(4, window.innerWidth - rect.width - 4);
    }
    if (top + rect.height > window.innerHeight - 4) {
      top = Math.max(4, window.innerHeight - rect.height - 4);
    }
    setPos({ left, top });
  }, [state]);

  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onScroll = () => onClose();
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  const { target } = state;
  const multi =
    target !== null && pane.selected.length > 1 && pane.selected.includes(target.path);
  const paths = multi ? [...pane.selected] : target ? [target.path] : [];

  const items: MenuItem[] = [];
  if (target === null) {
    items.push(
      { key: "mkdir", label: "新建目录", icon: <FolderPlus size={14} aria-hidden="true" />, run: actions.onMkdir },
      { key: "newfile", label: "新建文件", icon: <FilePlus2 size={14} aria-hidden="true" />, run: actions.onCreateFile },
      { key: "upload", label: "上传文件", icon: <Upload size={14} aria-hidden="true" />, run: actions.onUpload },
      { key: "refresh", label: "刷新", icon: <RefreshCw size={14} aria-hidden="true" />, run: actions.onRefresh },
      { key: "select-all", label: "全选", icon: <CheckCheck size={14} aria-hidden="true" />, run: actions.onSelectAll },
      { key: "invert", label: "反选", icon: <Shuffle size={14} aria-hidden="true" />, run: actions.onInvertSelection },
    );
  } else if (multi) {
    items.push(
      { key: "archive", label: `打包 ${paths.length} 项`, icon: <FileArchive size={14} aria-hidden="true" />, run: () => actions.onArchive(paths) },
      { key: "move", label: "移动到…", icon: <FolderInput size={14} aria-hidden="true" />, run: () => actions.onMove(paths) },
      { key: "copy", label: "复制到…", icon: <Copy size={14} aria-hidden="true" />, run: () => actions.onCopy(paths) },
      { key: "delete", label: `删除 ${paths.length} 项`, icon: <Trash2 size={14} aria-hidden="true" />, danger: true, run: () => actions.onDelete(paths) },
    );
  } else if (target.kind === "directory") {
    items.push(
      { key: "open", label: "打开", icon: <FolderOpen size={14} aria-hidden="true" />, run: () => actions.onOpen(target) },
      { key: "download", label: "下载（打包下载）", icon: <Download size={14} aria-hidden="true" />, run: () => actions.onDownload(target) },
      { key: "rename", label: "重命名", icon: <Pencil size={14} aria-hidden="true" />, run: () => actions.onRename(target) },
      { key: "move", label: "移动到…", icon: <FolderInput size={14} aria-hidden="true" />, run: () => actions.onMove(paths) },
      { key: "copy", label: "复制到…", icon: <Copy size={14} aria-hidden="true" />, run: () => actions.onCopy(paths) },
      { key: "delete", label: "删除", icon: <Trash2 size={14} aria-hidden="true" />, danger: true, run: () => actions.onDelete(paths) },
      { key: "properties", label: "属性", icon: <Info size={14} aria-hidden="true" />, run: () => actions.onProperties(target) },
    );
  } else {
    items.push(
      { key: "download", label: "下载", icon: <Download size={14} aria-hidden="true" />, run: () => actions.onDownload(target) },
    );
    if (target.kind === "file" && isArchiveName(target.name)) {
      items.push({
        key: "extract",
        label: "解压到当前目录",
        icon: <PackageOpen size={14} aria-hidden="true" />,
        run: () => actions.onExtract(target),
      });
    }
    items.push(
      { key: "rename", label: "重命名", icon: <Pencil size={14} aria-hidden="true" />, run: () => actions.onRename(target) },
      { key: "move", label: "移动到…", icon: <FolderInput size={14} aria-hidden="true" />, run: () => actions.onMove(paths) },
      { key: "copy", label: "复制到…", icon: <Copy size={14} aria-hidden="true" />, run: () => actions.onCopy(paths) },
      { key: "delete", label: "删除", icon: <Trash2 size={14} aria-hidden="true" />, danger: true, run: () => actions.onDelete(paths) },
      { key: "properties", label: "属性", icon: <Info size={14} aria-hidden="true" />, run: () => actions.onProperties(target) },
    );
  }

  return createPortal(
    <div
      ref={menuRef}
      className="file-context-menu"
      role="menu"
      style={{ left: pos.left, top: pos.top }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="menuitem"
          className={`file-context-item${item.danger ? " danger" : ""}`}
          onClick={() => {
            onClose();
            item.run();
          }}
        >
          {item.icon}
          <span>{item.label}</span>
        </button>
      ))}
    </div>,
    document.body,
  );
}
