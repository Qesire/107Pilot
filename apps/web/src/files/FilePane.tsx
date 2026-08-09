import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Selecto from "react-selecto";
import {
  ArrowLeft,
  ArrowRight,
  ChevronRight,
  Columns3,
  Copy,
  Download,
  FolderInput,
  FolderPlus,
  FilePlus2,
  Home,
  LayoutGrid,
  List,
  CornerLeftUp,
  PackageOpen,
  Pencil,
  Trash2,
} from "lucide-react";
import { QueryBoundary } from "../components";
import { formatTimestamp } from "../components";
import { api } from "../api";
import { formatStorageBytes } from "../resource-summary";
import type { FileEntry } from "../types";
import { EntryActionButtons, InlineRenameInput, type PaneActions } from "./entry-widgets";
import { EntryInfoDialog } from "./EntryInfoDialog";
import { FileContextMenu, type ContextMenuState } from "./FileContextMenu";
import { FileGrid } from "./FileGrid";
import { useFilesManager } from "./FilesManagerContext";
import { MillerColumns } from "./MillerColumns";
import { MoveDialog } from "./MoveDialog";
import { isArchiveName, clampToHome, pathSegments } from "./selection";
import { useFilePane } from "./useFilePane";

function FileListView({
  pane,
  actions,
  dropTarget,
}: {
  pane: ReturnType<typeof useFilePane>;
  actions: PaneActions;
  dropTarget: string | null;
}) {
  const manager = useFilesManager();
  const selectedSet = new Set(pane.selected);
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  // The element a pointer gesture started on (mousedown origin). Selecto's
  // onSelectEnd only exposes the mouseup event, so we track the origin here to
  // tell a row drag (native move) from an empty-area drag (marquee select).
  const downOnRow = useRef(false);
  return (
    <div
      className="filelist"
      ref={setContainer}
      onPointerDownCapture={(e) => {
        downOnRow.current = Boolean((e.target as Element | null)?.closest?.(".file-row"));
      }}
    >
    <table className="files-table filepane-table">
      <thead>
        <tr>
          <th>名称</th>
          <th className="col-size">大小</th>
          <th className="col-time">修改时间</th>
          <th className="col-ops" aria-label="操作" />
        </tr>
      </thead>
      <tbody>
        {pane.entries.map((entry: FileEntry) => (
          <tr
            key={entry.path}
            className={`file-row${selectedSet.has(entry.path) ? " selected" : ""}${
              dropTarget === entry.path ? " drop-target" : ""
            }`}
            data-path={entry.path}
            data-kind={entry.kind}
            draggable
            onDragStart={(e) => actions.onDragStartEntry(entry, e)}
            onDragEnd={actions.onDragEndEntry}
            onClick={(e) => {
              manager.setActivePane(pane.paneId);
              if (e.shiftKey || e.metaKey || e.ctrlKey) pane.toggleSelect(entry.path);
              else if (entry.kind === "directory") pane.navigateTo(entry.path);
              else pane.setSelection([entry.path]);
            }}
            onContextMenu={(e) => {
              e.preventDefault();
              e.stopPropagation();
              if (!selectedSet.has(entry.path)) pane.setSelection([entry.path]);
              actions.onOpenContext(entry, e.clientX, e.clientY);
            }}
          >
            <td className="col-name">
              {pane.renamingPath === entry.path ? (
                <InlineRenameInput
                  initialName={entry.name}
                  busy={pane.busy}
                  onCommit={(name) => void pane.renameEntry(entry.path, name)}
                  onCancel={() => pane.setRenamingPath(null)}
                />
              ) : (
                <span className="entry-name" title={entry.path}>{entry.name}</span>
              )}
            </td>
            <td className="col-size">
              {entry.kind === "directory" ? "—" : formatStorageBytes(entry.size)}
            </td>
            <td className="col-time">{formatTimestamp(entry.modified)}</td>
            <td className="col-ops">
              <EntryActionButtons entry={entry} pane={pane} actions={actions} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>

      <Selecto
        dragContainer={container}
        selectableTargets={[".file-row"]}
        hitRate={0}
        selectByClick={false}
        selectFromInside
        continueSelect
        toggleContinueSelect="shift"
        ratio={0}
        // Do NOT preventDefault on mousedown — doing so would suppress the
        // native HTML5 dragstart on rows. Marquee selection is driven by
        // mousemove, so it still works.
        preventDefault={false}
        onSelectEnd={(e) => {
          const rect = e.rect;
          // A gesture that began on a row is a native drag-and-drop move; let
          // the row onClick / native drag handle it, not marquee selection.
          if (downOnRow.current) return;
          // Only a gesture with a real selection rectangle is a marquee; plain
          // clicks on empty space are ignored here.
          if (!rect || (rect.width < 4 && rect.height < 4)) return;
          manager.setActivePane(pane.paneId);
          const paths = e.selected
            .map((el) => el.getAttribute("data-path"))
            .filter((p): p is string => Boolean(p));
          pane.setSelection(paths);
        }}
      />
    </div>
  );
}

export function FilePane({
  paneId,
  homePath,
  onClose,
  onSplitHorizontal,
  onSplitVertical,
}: {
  paneId: string;
  homePath: string;
  onClose: () => void;
  onSplitHorizontal: () => void;
  onSplitVertical: () => void;
}) {
  const manager = useFilesManager();
  const pane = useFilePane(paneId, homePath);
  const isActive = manager.activePaneId === paneId;

  const [menu, setMenu] = useState<ContextMenuState | null>(null);
  const [destDialog, setDestDialog] = useState<{
    mode: "move" | "copy";
    paths: string[];
  } | null>(null);
  const [infoEntry, setInfoEntry] = useState<FileEntry | null>(null);
  const [notice, setNotice] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  // Directory path (or pane cwd) currently highlighted as a drop destination.
  // Lifted out of FileGrid so column/grid/list views all support drag-move.
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const selectedSet = useMemo(() => new Set(pane.selected), [pane.selected]);

  // Transient action feedback (poor man's toast) auto-dismisses.
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        pane.clearSelection();
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") {
        e.preventDefault();
        pane.selectAll();
      } else if (e.key === "Delete" && pane.selected.length > 0) {
        e.preventDefault();
        if (window.confirm(`确认删除选中的 ${pane.selected.length} 个项目？`)) {
          void pane.deleteEntries([...pane.selected]);
        }
      } else if (e.key === "F5") {
        e.preventDefault();
        pane.refresh();
      } else if (e.altKey && e.key === "ArrowUp") {
        e.preventDefault();
        pane.goUp();
      }
    },
    [pane],
  );

  const downloadEntry = useCallback(
    async (entry: FileEntry) => {
      if (entry.kind === "file") {
        try {
          await downloadFile(manager.user, entry);
        } catch (err) {
          setNotice({ kind: "error", text: `下载失败：${errorMessage(err)}` });
        }
        return;
      }
      // Directories: pack into a temporary tar.gz next to the pane cwd,
      // download it, then remove the temporary archive (best effort).
      const archiveName = `${entry.name}.tar.gz`;
      try {
        const created = await api.fileArchive(manager.user, [entry.path], pane.cwd, archiveName);
        await downloadFile(manager.user, {
          name: archiveName,
          path: created.path,
          kind: "file",
          size: created.size,
          modified: "",
        });
        await api.fileDelete(manager.user, created.path).catch(() => undefined);
        pane.refresh();
      } catch (err) {
        setNotice({ kind: "error", text: `打包下载失败：${errorMessage(err)}` });
      }
    },
    [manager.user, pane],
  );

  // -- Entry drag & drop (move), view-agnostic. The drop destination is
  // resolved from the pointer position, so drops may land in another pane
  // (cross-pane move) regardless of the source/target view mode. --
  const dropTargetAt = useCallback(
    (x: number, y: number): string | null => {
      const stack = document.elementsFromPoint(x, y);
      let paneCwd: string | null = null;
      for (const node of stack) {
        if (!(node instanceof Element)) continue;
        const tile = node.closest("[data-path]");
        if (
          tile &&
          tile.getAttribute("data-kind") === "directory" &&
          !selectedSet.has(tile.getAttribute("data-path") ?? "")
        ) {
          return tile.getAttribute("data-path");
        }
        if (!paneCwd) {
          const paneRoot = node.closest("[data-pane-cwd]");
          if (paneRoot) paneCwd = paneRoot.getAttribute("data-pane-cwd");
        }
      }
      return paneCwd;
    },
    [selectedSet],
  );

  const handleEntryDragStart = useCallback(
    (entry: FileEntry, e: React.DragEvent) => {
      manager.setActivePane(paneId);
      // Dragging a selected entry moves the whole selection; dragging an
      // unselected one moves just that entry. Do NOT setSelection here: the
      // resulting re-render scrolls the drag source into view (MillerColumn's
      // selected-row effect), and scrolling an element mid-dragstart makes
      // the browser cancel the whole drag (immediate dragend, no drop).
      const paths = selectedSet.has(entry.path) ? [...pane.selected] : [entry.path];
      const entries = pane.entries.filter((en) => paths.includes(en.path));
      // Shared across panes: the drop may land in a different pane.
      manager.setDragPayload({ sourcePaneId: paneId, entries });
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", paths.join("\n"));
    },
    [manager, pane, paneId, selectedSet],
  );

  const handleEntryDragEnd = useCallback(() => {
    manager.setDragPayload(null);
    setDropTarget(null);
  }, [manager]);

  const handleBodyDragOver = useCallback(
    (e: React.DragEvent) => {
      // External file drags bubble up to the page-level upload overlay.
      if (e.dataTransfer.types.includes("Files")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      setDropTarget(dropTargetAt(e.clientX, e.clientY));
    },
    [dropTargetAt],
  );

  const handleBodyDrop = useCallback(
    (e: React.DragEvent) => {
      if (e.dataTransfer.types.includes("Files")) return; // page handles upload
      e.preventDefault();
      const dest = dropTargetAt(e.clientX, e.clientY);
      setDropTarget(null);
      const payload = manager.getDragPayload();
      manager.setDragPayload(null);
      if (!dest || !payload || payload.entries.length === 0) return;
      const sourceController = manager.getController(payload.sourcePaneId);
      void manager.moveEntries(payload.entries, dest).then((moved) => {
        if (moved > 0) {
          // Refresh both the drop pane and the source pane (cross-pane move).
          sourceController?.refresh();
          if (payload.sourcePaneId !== paneId) pane.refresh();
          pane.clearSelection();
        }
      });
    },
    [dropTargetAt, manager, pane, paneId],
  );

  const handleBodyDragLeave = useCallback((e: React.DragEvent) => {
    if (!(e.currentTarget as Element).contains(e.relatedTarget as Node | null)) {
      setDropTarget(null);
    }
  }, []);

  const actions = useMemo<PaneActions>(
    () => ({
      onOpen: (entry) => {
        if (entry.kind === "directory") pane.navigateTo(entry.path);
      },
      onDownload: (entry) => {
        setMenu(null);
        void downloadEntry(entry);
      },
      onRename: (entry) => {
        setMenu(null);
        pane.setRenamingPath(entry.path);
      },
      onMove: (paths) => {
        setMenu(null);
        setDestDialog({ mode: "move", paths });
      },
      onCopy: (paths) => {
        setMenu(null);
        setDestDialog({ mode: "copy", paths });
      },
      onExtract: (entry) => {
        setMenu(null);
        void pane
          .extractEntry(entry.path)
          .then(() => setNotice({ kind: "success", text: `已解压 ${entry.name}` }))
          .catch((err) => setNotice({ kind: "error", text: `解压失败：${errorMessage(err)}` }));
      },
      onDelete: (paths) => {
        setMenu(null);
        if (!window.confirm(`确认删除选中的 ${paths.length} 个项目？`)) return;
        void pane
          .deleteEntries(paths)
          .then(() => setNotice({ kind: "success", text: `已删除 ${paths.length} 项` }))
          .catch((err) => setNotice({ kind: "error", text: `删除失败：${errorMessage(err)}` }));
      },
      onArchive: (paths) => {
        setMenu(null);
        void pane
          .archiveEntries(paths)
          .then(() => setNotice({ kind: "success", text: "已打包为 archive.tar.gz" }))
          .catch((err) => setNotice({ kind: "error", text: `打包失败：${errorMessage(err)}` }));
      },
      onMkdir: () => {
        setMenu(null);
        pane.setInlineForm("mkdir");
      },
      onCreateFile: () => {
        setMenu(null);
        pane.setInlineForm("newfile");
      },
      onUpload: () => {
        setMenu(null);
        manager.requestUpload();
      },
      onRefresh: () => {
        setMenu(null);
        pane.refresh();
      },
      onSelectAll: () => {
        setMenu(null);
        pane.selectAll();
      },
      onInvertSelection: () => {
        setMenu(null);
        pane.invertSelect();
      },
      onProperties: (entry) => {
        setMenu(null);
        setInfoEntry(entry);
      },
      onOpenContext: (entry, x, y) => {
        manager.setActivePane(paneId);
        setMenu({ x, y, target: entry });
      },
      onDragStartEntry: (entry, e) => handleEntryDragStart(entry, e),
      onDragEndEntry: () => handleEntryDragEnd(),
    }),
    [pane, downloadEntry, manager, paneId, handleEntryDragStart, handleEntryDragEnd],
  );

  const singleFile = pane.selectedEntries.length === 1 ? pane.selectedEntries[0] : undefined;

  return (
    <section
      className={`file-pane${isActive ? " active" : ""}`}
      data-pane-cwd={pane.cwd}
      tabIndex={0}
      onFocus={() => manager.setActivePane(paneId)}
      onMouseDown={() => manager.setActivePane(paneId)}
      onKeyDown={handleKeyDown}
      aria-label={`文件窗格 ${pane.cwd}`}
    >
      <header className="filepane-bar">
        <div className="filepane-nav">
          <button type="button" className="icon-button" title="后退" disabled={!pane.canBack} onClick={pane.goBack}>
            <ArrowLeft size={15} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" title="前进" disabled={!pane.canForward} onClick={pane.goForward}>
            <ArrowRight size={15} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" title="上级目录" disabled={!pane.canUp} onClick={pane.goUp}>
            <CornerLeftUp size={15} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" title="主目录" onClick={() => pane.navigateTo(homePath)}>
            <Home size={15} aria-hidden="true" />
          </button>
        </div>

        <nav className="filepane-breadcrumb" aria-label="路径">
          {pathSegments(pane.cwd)
            .filter((seg) => clampToHome(seg.path, homePath) === seg.path)
            .map((seg, idx) => (
              <span key={seg.path} className="crumb">
                {idx > 0 && <ChevronRight size={11} aria-hidden="true" />}
                <button type="button" onClick={() => pane.navigateTo(seg.path)}>{seg.label}</button>
              </span>
            ))}
        </nav>

        <div className="filepane-tools">
          <button
            type="button"
            className={`icon-button${pane.viewMode === "column" ? " active" : ""}`}
            title="分栏视图"
            onClick={() => pane.setViewMode("column")}
          >
            <Columns3 size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={`icon-button${pane.viewMode === "grid" ? " active" : ""}`}
            title="网格视图"
            onClick={() => pane.setViewMode("grid")}
          >
            <LayoutGrid size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={`icon-button${pane.viewMode === "list" ? " active" : ""}`}
            title="列表视图"
            onClick={() => pane.setViewMode("list")}
          >
            <List size={15} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" title="新建目录" onClick={() => pane.setInlineForm("mkdir")}>
            <FolderPlus size={15} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" title="新建文件" onClick={() => pane.setInlineForm("newfile")}>
            <FilePlus2 size={15} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" title="横向拆分" onClick={onSplitHorizontal}>
            <span aria-hidden="true" className="split-glyph">▤</span>
          </button>
          <button type="button" className="icon-button" title="纵向拆分" onClick={onSplitVertical}>
            <span aria-hidden="true" className="split-glyph">▥</span>
          </button>
          <button type="button" className="icon-button danger" title="关闭窗格" onClick={onClose}>
            ✕
          </button>
        </div>
      </header>

      {pane.inlineForm && (
        <form
          className="filepane-mkdir"
          onSubmit={(e) => {
            e.preventDefault();
            const input = e.currentTarget.elements.namedItem("name") as HTMLInputElement;
            const name = input.value.trim();
            if (!name) return;
            if (pane.inlineForm === "newfile") pane.createFile(name);
            else pane.createDir(name);
          }}
        >
          <input
            name="name"
            autoFocus
            placeholder={pane.inlineForm === "newfile" ? "新文件名称…" : "新目录名称…"}
            aria-label={pane.inlineForm === "newfile" ? "新文件名称" : "新目录名称"}
          />
          <button type="submit" className="button primary">创建</button>
          <button type="button" className="button ghost" onClick={() => pane.setInlineForm(null)}>取消</button>
        </form>
      )}

      {pane.selected.length > 0 && (
        <div className="filepane-selectionbar">
          <span>{pane.selected.length} 项已选</span>
          {singleFile && (
            <button type="button" className="button secondary" onClick={() => actions.onDownload(singleFile)}>
              <Download size={13} aria-hidden="true" /> 下载
            </button>
          )}
          {singleFile && (
            <button type="button" className="button secondary" onClick={() => actions.onRename(singleFile)}>
              <Pencil size={13} aria-hidden="true" /> 重命名
            </button>
          )}
          <button
            type="button"
            className="button secondary"
            onClick={() => actions.onMove([...pane.selected])}
          >
            <FolderInput size={13} aria-hidden="true" /> 移动
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={() => actions.onCopy([...pane.selected])}
          >
            <Copy size={13} aria-hidden="true" /> 复制
          </button>
          {singleFile && singleFile.kind === "file" && isArchiveName(singleFile.name) && (
            <button type="button" className="button secondary" onClick={() => actions.onExtract(singleFile)}>
              <PackageOpen size={13} aria-hidden="true" /> 解压
            </button>
          )}
          <button
            type="button"
            className="button secondary"
            onClick={() => actions.onArchive([...pane.selected])}
          >
            打包
          </button>
          <button
            type="button"
            className="button danger"
            onClick={() => actions.onDelete([...pane.selected])}
          >
            <Trash2 size={13} aria-hidden="true" /> 删除
          </button>
          <button type="button" className="button ghost" onClick={pane.selectAll}>全选</button>
          <button type="button" className="button ghost" onClick={pane.invertSelect}>反选</button>
          <button type="button" className="button ghost" onClick={pane.clearSelection}>取消</button>
        </div>
      )}

      <div
        className="filepane-body"
        onContextMenu={(e) => {
          // Rows/tiles stopPropagation; only empty space bubbles here.
          e.preventDefault();
          actions.onOpenContext(null, e.clientX, e.clientY);
        }}
        onDragOver={handleBodyDragOver}
        onDragLeave={handleBodyDragLeave}
        onDrop={handleBodyDrop}
      >
        {pane.viewMode === "column" ? (
          <MillerColumns pane={pane} homePath={homePath} actions={actions} dropTarget={dropTarget} />
        ) : (
          <QueryBoundary
            pending={pane.isPending}
            error={pane.error}
            empty={pane.entries.length === 0}
            emptyTitle="空目录"
            emptyDetail="此目录下没有文件或子目录。"
          >
            {pane.viewMode === "grid" ? (
              <FileGrid pane={pane} actions={actions} dropTarget={dropTarget} />
            ) : (
              <FileListView pane={pane} actions={actions} dropTarget={dropTarget} />
            )}
          </QueryBoundary>
        )}
      </div>

      <footer className="filepane-status">
        <span>{pane.entries.length} 项</span>
        {singleFile && singleFile.kind === "file" && (
          <button
            type="button"
            className="text-link"
            onClick={() => void downloadEntry(singleFile)}
          >
            <Download size={12} aria-hidden="true" /> 下载
          </button>
        )}
      </footer>

      {notice && (
        <div className={`filepane-notice notice-${notice.kind}`} role="status">
          {notice.text}
        </div>
      )}

      {menu && (
        <FileContextMenu state={menu} pane={pane} actions={actions} onClose={() => setMenu(null)} />
      )}

      {destDialog && (
        <MoveDialog
          user={manager.user}
          homePath={homePath}
          initialDir={pane.cwd}
          count={destDialog.paths.length}
          busy={pane.busy}
          headerText={
            destDialog.mode === "copy"
              ? `复制 ${destDialog.paths.length} 个项目到…`
              : undefined
          }
          confirmText={destDialog.mode === "copy" ? "复制到此" : undefined}
          onClose={() => setDestDialog(null)}
          onConfirm={(destDir) => {
            const { mode, paths } = destDialog;
            setDestDialog(null);
            if (mode === "copy") {
              void pane
                .copyEntriesTo(paths, destDir)
                .then(() => setNotice({ kind: "success", text: `已复制 ${paths.length} 项` }))
                .catch((err) => setNotice({ kind: "error", text: `复制失败：${errorMessage(err)}` }));
            } else {
              void pane
                .moveEntriesTo(paths, destDir)
                .then((moved) => setNotice({ kind: "success", text: `已移动 ${moved} 项` }))
                .catch((err) => setNotice({ kind: "error", text: `移动失败：${errorMessage(err)}` }));
            }
          }}
        />
      )}

      {infoEntry && (
        <EntryInfoDialog
          user={manager.user}
          entry={infoEntry}
          onClose={() => setInfoEntry(null)}
        />
      )}
    </section>
  );
}

// Chunked base64 download reused from the legacy single-pane page.
function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function downloadFile(user: string, entry: FileEntry) {
  const chunks: Uint8Array[] = [];
  let offset = 0;
  const READ_LEN = 1024 * 1024;
  let totalSize = entry.size;
  while (offset < totalSize || chunks.length === 0) {
    const resp = await api.fileContent(user, entry.path, offset, READ_LEN);
    totalSize = resp.size;
    const raw = atob(resp.data_b64);
    const arr = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    chunks.push(arr);
    offset += arr.length;
    if (arr.length === 0) break;
  }
  const blob = new Blob(chunks);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = entry.name;
  a.click();
  URL.revokeObjectURL(url);
}
