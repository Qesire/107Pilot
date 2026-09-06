import { useInfiniteQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";
import { api } from "../api";
import type { FileEntry } from "../types";
import { EntryActionButtons, InlineRenameInput, type PaneActions } from "./entry-widgets";
import { useFilesManager, type FilesManager } from "./FilesManagerContext";
import { TileIcon } from "./FileGrid";
import { columnDirsFor } from "./selection";
import { useMarqueeSelection } from "./useMarqueeSelection";
import type { UseFilePaneResult } from "./useFilePane";
import { MILLER_ROW_HEIGHT, useVirtualWindow } from "./virtualization";

function MillerColumn({
  user, dir, nextDir, isLast, pane, actions, manager, dropTarget,
}: {
  user: string;
  dir: string;
  nextDir: string | null;
  isLast: boolean;
  pane: UseFilePaneResult;
  actions: PaneActions;
  manager: FilesManager;
  dropTarget: string | null;
}) {
  const [body, setBody] = useState<HTMLDivElement | null>(null);
  const listing = useInfiniteQuery({
    queryKey: ["files-list", user, dir],
    queryFn: ({ signal, pageParam }) => api.fileList(user, dir, { limit: 500, cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
    retry: false,
  });
  const entries = useMemo(
    () => listing.data?.pages.flatMap((page) => page.entries) ?? [],
    [listing.data],
  );
  const selectedSet = useMemo(() => new Set(pane.selected), [pane.selected]);
  const focusPath = nextDir ?? (isLast ? pane.selected[0] ?? null : null);
  const focusIndex = focusPath ? entries.findIndex((entry) => entry.path === focusPath) : -1;

  useEffect(() => {
    if (!body || focusIndex < 0) return;
    const top = focusIndex * MILLER_ROW_HEIGHT;
    const bottom = top + MILLER_ROW_HEIGHT;
    if (top < body.scrollTop) body.scrollTop = top;
    else if (bottom > body.scrollTop + body.clientHeight) {
      body.scrollTop = bottom - body.clientHeight;
    }
  }, [body, focusIndex]);

  const range = useVirtualWindow(body, entries.length, MILLER_ROW_HEIGHT, 6);
  const visibleEntries = entries.slice(range.start, range.end);
  const label = dir === "/" ? "/" : (dir.split("/").filter(Boolean).pop() ?? dir);

  return (
    <div
      className="miller-column"
      data-miller-dir={dir}
      data-last-column={isLast ? "true" : undefined}
    >
      <div className="miller-column-header" title={dir}>{label}</div>
      <div className="miller-column-body" ref={setBody}>
        {listing.isPending && <div className="miller-empty">加载中…</div>}
        {listing.isError && <div className="miller-empty miller-error">加载失败</div>}
        {!listing.isPending && !listing.isError && entries.length === 0 && (
          <div className="miller-empty">空目录</div>
        )}
        {range.paddingBefore > 0 ? (
          <div className="virtual-block-spacer" style={{ height: range.paddingBefore }} aria-hidden="true" />
        ) : null}
        {visibleEntries.map((entry: FileEntry, localIndex) => {
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
              data-virtual-index={range.start + localIndex}
              title={entry.path}
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
        {range.paddingAfter > 0 ? (
          <div className="virtual-block-spacer" style={{ height: range.paddingAfter }} aria-hidden="true" />
        ) : null}
        {listing.hasNextPage ? (
          <button
            type="button"
            className="miller-load-more"
            disabled={listing.isFetchingNextPage}
            onClick={() => void listing.fetchNextPage()}
          >
            {listing.isFetchingNextPage ? "加载中…" : "加载更多"}
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function MillerColumns({
  pane, homePath, actions, dropTarget,
}: {
  pane: UseFilePaneResult;
  homePath: string;
  actions: PaneActions;
  dropTarget: string | null;
}) {
  const filesManager = useFilesManager();
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const dirs = useMemo(() => columnDirsFor(pane.cwd, homePath), [pane.cwd, homePath]);
  const chainKey = dirs.join("\u0000");
  useEffect(() => {
    if (container) container.scrollLeft = container.scrollWidth;
  }, [chainKey, container]);

  const handleMarquee = useCallback((paths: string[], additive: boolean) => {
    filesManager.setActivePane(pane.paneId);
    pane.setSelection(additive
      ? Array.from(new Set([...pane.selected, ...paths]))
      : paths);
  }, [filesManager, pane]);
  const marquee = useMarqueeSelection({
    rootElement: container,
    surfaceElement: container,
    itemSelector: '.miller-column[data-last-column="true"] .miller-row[data-path]',
    blockedStartSelector: ".miller-row",
    onSelect: handleMarquee,
  });

  return (
    <div
      className="miller-columns"
      ref={setContainer}
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
      {marquee.marqueeStyle ? <div className="file-marquee" style={marquee.marqueeStyle} /> : null}
    </div>
  );
}
