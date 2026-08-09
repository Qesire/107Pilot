// Miller-column ("flowing") navigation: the pane's cwd is the deepest column;
// every ancestor directory back to the home root renders as its own column to
// the left. Clicking a directory makes it the cwd (columns truncate/extend
// automatically); clicking a file single-selects it in the deepest column.
//
// Each column owns its own ["files-list", user, dir] query, so the whole
// chain shares the react-query cache with the pane listing and refreshes on
// the same invalidations.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import Selecto from "react-selecto";
import { ChevronRight } from "lucide-react";
import { api } from "../api";
import type { FileEntry } from "../types";
import { EntryActionButtons, InlineRenameInput, type PaneActions } from "./entry-widgets";
import { useFilesManager, type FilesManager } from "./FilesManagerContext";
import { TileIcon } from "./FileGrid";
import { columnDirsFor, sortEntries } from "./selection";
import type { UseFilePaneResult } from "./useFilePane";

function MillerColumn({
  user,
  dir,
  nextDir,
  isLast,
  pane,
  actions,
  manager,
  dropTarget,
}: {
  user: string;
  dir: string;
  /** The directory shown in the following column (this column's active row). */
  nextDir: string | null;
  isLast: boolean;
  pane: UseFilePaneResult;
  actions: PaneActions;
  manager: FilesManager;
  /** Directory path highlighted as a drop destination (owned by FilePane). */
  dropTarget: string | null;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const listing = useQuery({
    queryKey: ["files-list", user, dir],
    queryFn: ({ signal }) => api.fileList(user, dir, signal),
    retry: false,
  });
  const entries = useMemo(
    () => sortEntries(listing.data?.entries ?? []),
    [listing.data],
  );
  const selectedSet = useMemo(() => new Set(pane.selected), [pane.selected]);

  // Keep the active row visible when the chain grows beyond the viewport.
  useEffect(() => {
    const active = bodyRef.current?.querySelector(".miller-row.active, .miller-row.selected");
    active?.scrollIntoView({ block: "nearest" });
  }, [nextDir, pane.selected]);

  const label = dir === "/" ? "/" : (dir.split("/").filter(Boolean).pop() ?? dir);

  return (
    <div className="miller-column" data-miller-dir={dir}>
      <div className="miller-column-header" title={dir}>{label}</div>
      <div className="miller-column-body" ref={bodyRef}>
        {listing.isPending && <div className="miller-empty">加载中…</div>}
        {listing.isError && <div className="miller-empty miller-error">加载失败</div>}
        {!listing.isPending && !listing.isError && entries.length === 0 && (
          <div className="miller-empty">空目录</div>
        )}
        {entries.map((entry: FileEntry) => {
          const isActive = nextDir === entry.path;
          const isSelected = isLast && selectedSet.has(entry.path);
          const renaming = pane.renamingPath === entry.path;
          return (
            <div
              key={entry.path}
              className={`miller-row${isActive ? " active" : ""}${isSelected ? " selected" : ""}${
                dropTarget === entry.path ? " drop-target" : ""
              }`}
              data-path={entry.path}
              data-kind={entry.kind}
              title={entry.path}
              draggable
              onDragStart={(e) => actions.onDragStartEntry(entry, e)}
              onDragEnd={actions.onDragEndEntry}
              onClick={(e) => {
                e.stopPropagation();
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
              <TileIcon entry={entry} size={15} />
              {renaming ? (
                <InlineRenameInput
                  initialName={entry.name}
                  busy={pane.busy}
                  onCommit={(name) => void pane.renameEntry(entry.path, name)}
                  onCancel={() => pane.setRenamingPath(null)}
                />
              ) : (
                <span className="miller-row-name">{entry.name}</span>
              )}
              {entry.kind === "directory" && (
                <ChevronRight size={12} aria-hidden="true" className="miller-row-chevron" />
              )}
              <EntryActionButtons entry={entry} pane={pane} actions={actions} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function MillerColumns({
  pane,
  homePath,
  actions,
  dropTarget,
}: {
  pane: UseFilePaneResult;
  homePath: string;
  actions: PaneActions;
  dropTarget: string | null;
}) {
  const filesManager = useFilesManager();
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  // Mousedown origin: a gesture that began on a row is a native drag (move)
  // or a click, not a marquee selection (Selecto only exposes mouseup).
  const downOnRow = useRef(false);
  const dirs = useMemo(() => columnDirsFor(pane.cwd, homePath), [pane.cwd, homePath]);

  // Flow to the deepest column whenever the chain changes.
  const chainKey = dirs.join("\u0000");
  useEffect(() => {
    if (container) container.scrollLeft = container.scrollWidth;
  }, [chainKey, container]);

  return (
    <div
      className="miller-columns"
      ref={setContainer}
      onPointerDownCapture={(e) => {
        downOnRow.current = Boolean((e.target as Element | null)?.closest?.(".miller-row"));
      }}
    >
      {dirs.map((dir, idx) => (
        <MillerColumn
          key={dir}
          user={filesManager.user}
          dir={dir}
          nextDir={idx + 1 < dirs.length ? (dirs[idx + 1] ?? null) : null}
          isLast={idx === dirs.length - 1}
          pane={pane}
          actions={actions}
          manager={filesManager}
          dropTarget={dropTarget}
        />
      ))}

      <Selecto
        dragContainer={container}
        selectableTargets={[".miller-row"]}
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
          // A gesture that began on a row is a native drag/click, not marquee.
          if (downOnRow.current) return;
          const rect = e.rect;
          if (!rect || (rect.width < 4 && rect.height < 4)) return;
          filesManager.setActivePane(pane.paneId);
          // Only the deepest column lists the pane cwd's entries; ignore rows
          // of ancestor columns (and rows of sibling panes sharing the
          // global ".miller-row" selector).
          const deepest = new Set(pane.entries.map((en) => en.path));
          const paths = e.selected
            .map((el) => el.getAttribute("data-path"))
            .filter((p): p is string => p !== null && deepest.has(p));
          pane.setSelection(paths);
        }}
      />
    </div>
  );
}
