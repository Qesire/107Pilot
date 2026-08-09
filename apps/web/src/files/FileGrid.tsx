import { useCallback, useMemo, useRef, useState } from "react";
import Selecto from "react-selecto";
import { File, FileArchive, FileCode, FileImage, FileText, Folder, Link2 } from "lucide-react";
import type { FileEntry } from "../types";
import { EntryActionButtons, InlineRenameInput, type PaneActions } from "./entry-widgets";
import { useFilesManager } from "./FilesManagerContext";
import type { UseFilePaneResult } from "./useFilePane";

type FileCategory = "code" | "archive" | "image" | "text" | "generic";

function categorize(name: string): FileCategory {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["py", "js", "ts", "tsx", "jsx", "sh", "bash", "c", "cpp", "h", "java", "go", "rs", "rb", "sql", "json", "yaml", "yml", "toml", "xml", "html", "css"].includes(ext)) return "code";
  if (["tar", "gz", "tgz", "zip", "bz2", "xz", "7z", "rar"].includes(ext)) return "archive";
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"].includes(ext)) return "image";
  if (["txt", "md", "log", "csv", "rst", "tex"].includes(ext)) return "text";
  return "generic";
}

function TileIcon({ entry, size = 30 }: { entry: FileEntry; size?: number }) {
  if (entry.kind === "directory") return <Folder size={size} aria-hidden="true" className="tile-icon icon-folder" />;
  if (entry.kind === "symlink") return <Link2 size={size} aria-hidden="true" className="tile-icon icon-link" />;
  switch (categorize(entry.name)) {
    case "code": return <FileCode size={size} aria-hidden="true" className="tile-icon icon-code" />;
    case "archive": return <FileArchive size={size} aria-hidden="true" className="tile-icon icon-archive" />;
    case "image": return <FileImage size={size} aria-hidden="true" className="tile-icon icon-image" />;
    case "text": return <FileText size={size} aria-hidden="true" className="tile-icon icon-text" />;
    default: return <File size={size} aria-hidden="true" className="tile-icon icon-file" />;
  }
}

export { categorize, TileIcon };

export function FileGrid({
  pane,
  actions,
  dropTarget,
}: {
  pane: UseFilePaneResult;
  actions: PaneActions;
  /** Directory path highlighted as a drop destination (owned by FilePane). */
  dropTarget: string | null;
}) {
  const manager = useFilesManager();
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  // The element a pointer gesture started on (mousedown origin). Selecto's
  // onSelectEnd only exposes the mouseup event, so we track the origin here to
  // tell a tile drag (native move) from an empty-area drag (marquee select).
  const downOnTile = useRef(false);

  const selectedSet = useMemo(() => new Set(pane.selected), [pane.selected]);

  const openEntry = useCallback(
    (entry: FileEntry) => {
      if (entry.kind === "directory") pane.navigateTo(entry.path);
    },
    [pane],
  );

  return (
    <div
      className="filegrid"
      ref={setContainer}
      onPointerDownCapture={(e) => {
        downOnTile.current = Boolean((e.target as Element | null)?.closest?.(".file-tile"));
      }}
    >
      <div className="filegrid-tiles">
        {pane.entries.map((entry) => {
          const activate = (e: React.MouseEvent | React.KeyboardEvent) => {
            manager.setActivePane(pane.paneId);
            if (e.shiftKey || e.metaKey || e.ctrlKey) {
              pane.toggleSelect(entry.path);
            } else if (entry.kind === "directory") {
              openEntry(entry);
            } else {
              pane.setSelection([entry.path]);
            }
          };
          return (
            <div
              key={entry.path}
              role="button"
              tabIndex={0}
              draggable
              onDragStart={(e) => actions.onDragStartEntry(entry, e)}
              onDragEnd={actions.onDragEndEntry}
              className={`file-tile${selectedSet.has(entry.path) ? " selected" : ""}${
                dropTarget === entry.path ? " drop-target" : ""
              }`}
              data-path={entry.path}
              data-kind={entry.kind}
              title={entry.path}
              onClick={activate}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  activate(e);
                }
              }}
              onDoubleClick={() => openEntry(entry)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!selectedSet.has(entry.path)) pane.setSelection([entry.path]);
                actions.onOpenContext(entry, e.clientX, e.clientY);
              }}
            >
              <TileIcon entry={entry} />
              {pane.renamingPath === entry.path ? (
                <InlineRenameInput
                  initialName={entry.name}
                  busy={pane.busy}
                  onCommit={(name) => void pane.renameEntry(entry.path, name)}
                  onCancel={() => pane.setRenamingPath(null)}
                />
              ) : (
                <span className="tile-name">{entry.name}</span>
              )}
              <EntryActionButtons entry={entry} pane={pane} actions={actions} />
            </div>
          );
        })}
      </div>

      <Selecto
        dragContainer={container}
        selectableTargets={[".file-tile"]}
        hitRate={0}
        selectByClick={false}
        selectFromInside
        continueSelect
        toggleContinueSelect="shift"
        ratio={0}
        // Do NOT preventDefault on mousedown — doing so would suppress the
        // native HTML5 dragstart on tiles. Marquee selection is driven by
        // mousemove, so it still works.
        preventDefault={false}
        onSelectEnd={(e) => {
          const rect = e.rect;
          // A gesture that began on a tile is a native drag-and-drop move; let
          // the tile onClick / native drag handle it, not marquee selection.
          if (downOnTile.current) return;
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
