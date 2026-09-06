import { useCallback, useMemo, useState } from "react";
import { File, FileArchive, FileCode, FileImage, FileText, Folder, Link2 } from "lucide-react";
import type { FileEntry } from "../types";
import { EntryActionButtons, InlineRenameInput, type PaneActions } from "./entry-widgets";
import { useFilesManager } from "./FilesManagerContext";
import { useMarqueeSelection } from "./useMarqueeSelection";
import type { UseFilePaneResult } from "./useFilePane";
import {
  GRID_GAP,
  GRID_MIN_TILE_WIDTH,
  GRID_ROW_HEIGHT,
  useElementWidth,
  useVirtualWindow,
} from "./virtualization";

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
  dropTarget: string | null;
}) {
  const manager = useFilesManager();
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const width = useElementWidth(container);
  const columns = Math.max(1, Math.floor((width + GRID_GAP) / (GRID_MIN_TILE_WIDTH + GRID_GAP)));
  const rowCount = Math.ceil(pane.entries.length / columns);
  const scrollElement = container?.parentElement ?? null;
  const range = useVirtualWindow(scrollElement, rowCount, GRID_ROW_HEIGHT, 4);
  const startIndex = range.start * columns;
  const endIndex = Math.min(pane.entries.length, range.end * columns);
  const entries = pane.entries.slice(startIndex, endIndex);
  const selectedSet = useMemo(() => new Set(pane.selected), [pane.selected]);

  const openEntry = useCallback((entry: FileEntry) => {
    if (entry.kind === "directory") pane.navigateTo(entry.path);
  }, [pane]);

  const handleMarquee = useCallback((paths: string[], additive: boolean) => {
    manager.setActivePane(pane.paneId);
    pane.setSelection(additive
      ? Array.from(new Set([...pane.selected, ...paths]))
      : paths);
  }, [manager, pane]);
  const marquee = useMarqueeSelection({
    rootElement: container,
    surfaceElement: scrollElement,
    itemSelector: ".file-tile[data-path]",
    blockedStartSelector: ".file-tile",
    onSelect: handleMarquee,
  });

  return (
    <div
      className="filegrid virtual-filegrid"
      ref={setContainer}
    >
      <div
        className="filegrid-tiles virtual-filegrid-tiles"
        style={{ paddingTop: range.paddingBefore, paddingBottom: range.paddingAfter }}
        aria-setsize={pane.entries.length}
      >
        {entries.map((entry, localIndex) => {
          const activate = (e: React.MouseEvent | React.KeyboardEvent) => {
            manager.setActivePane(pane.paneId);
            if (e.shiftKey || e.metaKey || e.ctrlKey) pane.toggleSelect(entry.path);
            else if (entry.kind === "directory") openEntry(entry);
            else pane.setSelection([entry.path]);
          };
          return (
            <div
              key={entry.path}
              role="button"
              tabIndex={0}
              draggable
              onDragStart={(e) => actions.onDragStartEntry(entry, e)}
              onDragEnd={actions.onDragEndEntry}
              onDragOver={(e) => {
                if (entry.kind !== "directory" || e.dataTransfer.types.includes("Files")) return;
                e.preventDefault();
                e.stopPropagation();
              }}
              onDrop={(e) => actions.onDropIntoDirectory(entry, e)}
              className={`file-tile${selectedSet.has(entry.path) ? " selected" : ""}${
                dropTarget === entry.path ? " drop-target" : ""
              }`}
              data-path={entry.path}
              data-kind={entry.kind}
              data-virtual-index={startIndex + localIndex}
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
      {marquee.marqueeStyle ? <div className="file-marquee" style={marquee.marqueeStyle} /> : null}
    </div>
  );
}
