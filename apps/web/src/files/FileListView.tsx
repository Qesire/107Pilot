import { useCallback, useMemo, useState } from "react";
import { formatTimestamp } from "../components";
import { formatStorageBytes } from "../resource-summary";
import type { FileEntry } from "../types";
import { EntryActionButtons, InlineRenameInput, type PaneActions } from "./entry-widgets";
import { useFilesManager } from "./FilesManagerContext";
import { useMarqueeSelection } from "./useMarqueeSelection";
import type { UseFilePaneResult } from "./useFilePane";
import { LIST_ROW_HEIGHT, useVirtualWindow } from "./virtualization";

export function FileListView({
  pane,
  actions,
  dropTarget,
}: {
  pane: UseFilePaneResult;
  actions: PaneActions;
  dropTarget: string | null;
}) {
  const manager = useFilesManager();
  const selectedSet = useMemo(() => new Set(pane.selected), [pane.selected]);
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const scrollElement = container?.parentElement ?? null;
  const range = useVirtualWindow(scrollElement, pane.entries.length, LIST_ROW_HEIGHT);
  const entries = pane.entries.slice(range.start, range.end);

  const handleMarquee = useCallback((paths: string[], additive: boolean) => {
    manager.setActivePane(pane.paneId);
    pane.setSelection(additive
      ? Array.from(new Set([...pane.selected, ...paths]))
      : paths);
  }, [manager, pane]);
  const marquee = useMarqueeSelection({
    rootElement: container,
    surfaceElement: scrollElement,
    itemSelector: ".file-row[data-path]",
    blockedStartSelector: ".file-row",
    onSelect: handleMarquee,
  });

  return (
    <div
      className="filelist virtual-filelist"
      ref={setContainer}
    >
      <table className="files-table filepane-table" aria-rowcount={pane.entries.length}>
        <thead>
          <tr>
            <th>名称</th>
            <th className="col-size">大小</th>
            <th className="col-time">修改时间</th>
            <th className="col-ops" aria-label="操作" />
          </tr>
        </thead>
        <tbody>
          {range.paddingBefore > 0 ? (
            <tr className="virtual-spacer" aria-hidden="true">
              <td colSpan={4} style={{ height: range.paddingBefore }} />
            </tr>
          ) : null}
          {entries.map((entry: FileEntry, localIndex) => (
            <tr
              key={entry.path}
              className={`file-row${selectedSet.has(entry.path) ? " selected" : ""}${
                dropTarget === entry.path ? " drop-target" : ""
              }`}
              data-path={entry.path}
              data-kind={entry.kind}
              data-virtual-index={range.start + localIndex}
              draggable
              onDragStart={(e) => actions.onDragStartEntry(entry, e)}
              onDragEnd={actions.onDragEndEntry}
              onDragOver={(e) => {
                if (entry.kind !== "directory" || e.dataTransfer.types.includes("Files")) return;
                e.preventDefault();
                e.stopPropagation();
              }}
              onDrop={(e) => actions.onDropIntoDirectory(entry, e)}
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
          {range.paddingAfter > 0 ? (
            <tr className="virtual-spacer" aria-hidden="true">
              <td colSpan={4} style={{ height: range.paddingAfter }} />
            </tr>
          ) : null}
        </tbody>
      </table>
      {marquee.marqueeStyle ? <div className="file-marquee" style={marquee.marqueeStyle} /> : null}
    </div>
  );
}
