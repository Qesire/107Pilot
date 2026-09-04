// Destination-picker dialog for "移动到…" / "复制到…". Lazily expands a
// directory tree starting at the user's home (each level reuses the shared
// files-list query cache), then confirms a destination directory.

import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { ChevronRight, Folder, FolderOpen } from "lucide-react";
import { api } from "../api";

function MoveTreeNode({
  user,
  dir,
  depth,
  dest,
  expanded,
  onToggle,
  onSelect,
}: {
  user: string;
  dir: string;
  depth: number;
  dest: string;
  expanded: Set<string>;
  onToggle: (dir: string) => void;
  onSelect: (dir: string) => void;
}) {
  const isExpanded = expanded.has(dir);
  const listing = useInfiniteQuery({
    queryKey: ["files-list", user, dir],
    queryFn: ({ signal, pageParam }) => api.fileList(user, dir, { limit: 500, cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
    retry: false,
    enabled: isExpanded,
  });
  const subdirs = useMemo(
    () => (listing.data?.pages.flatMap((page) => page.entries) ?? []).filter((entry) => entry.kind === "directory"),
    [listing.data],
  );
  const label = dir === "/" ? "/" : (dir.split("/").filter(Boolean).pop() ?? dir);
  const isDest = dest === dir;

  return (
    <div className="move-tree-node">
      <div
        className={`move-tree-row${isDest ? " selected" : ""}`}
        style={{ paddingLeft: `${8 + depth * 16}px` }}
        role="treeitem"
        aria-selected={isDest}
        aria-expanded={isExpanded}
      >
        <button
          type="button"
          className="move-tree-caret"
          title={isExpanded ? "折叠" : "展开"}
          onClick={() => onToggle(dir)}
        >
          <ChevronRight
            size={13}
            aria-hidden="true"
            style={{ transform: isExpanded ? "rotate(90deg)" : undefined }}
          />
        </button>
        <button
          type="button"
          className="move-tree-label"
          title={dir}
          onClick={() => onSelect(dir)}
          onDoubleClick={() => onToggle(dir)}
        >
          {isExpanded ? (
            <FolderOpen size={14} aria-hidden="true" className="icon-folder" />
          ) : (
            <Folder size={14} aria-hidden="true" className="icon-folder" />
          )}
          <span>{label}</span>
        </button>
      </div>
      {isExpanded &&
        (listing.isPending ? (
          <div className="move-tree-loading" style={{ paddingLeft: `${24 + depth * 16}px` }}>
            加载中…
          </div>
        ) : (
          <>
            {subdirs.map((entry) => (
              <MoveTreeNode key={entry.path} user={user} dir={entry.path} depth={depth + 1} dest={dest} expanded={expanded} onToggle={onToggle} onSelect={onSelect} />
            ))}
            {listing.hasNextPage ? (
              <button type="button" className="move-tree-loading" style={{ paddingLeft: `${24 + depth * 16}px` }} disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>
                {listing.isFetchingNextPage ? "加载中…" : "加载更多目录"}
              </button>
            ) : null}
          </>
        ))}
    </div>
  );
}

export function MoveDialog({
  user,
  homePath,
  initialDir,
  count,
  busy,
  headerText,
  confirmText,
  onConfirm,
  onClose,
}: {
  user: string;
  homePath: string;
  /** Directory highlighted by default (usually the pane cwd). */
  initialDir: string;
  count: number;
  busy: boolean;
  /** Dialog title; defaults to the move wording. */
  headerText?: string | undefined;
  /** Confirm button label; defaults to the move wording. */
  confirmText?: string | undefined;
  onConfirm: (destDir: string) => void;
  onClose: () => void;
}) {
  const [dest, setDest] = useState(initialDir);
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set([homePath]),
  );

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const toggle = (dir: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });

  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal-card move-dialog" role="dialog" aria-modal="true" aria-label={headerText ?? "移动项目"}>
        <header className="move-dialog-header">
          {headerText ?? `移动 ${count} 个项目到…`}
        </header>
        <div className="move-tree" role="tree" aria-label="选择目标目录">
          <MoveTreeNode
            user={user}
            dir={homePath}
            depth={0}
            dest={dest}
            expanded={expanded}
            onToggle={toggle}
            onSelect={setDest}
          />
        </div>
        <footer className="move-dialog-footer">
          <span className="move-dialog-dest" title={dest}>目标：{dest}</span>
          <span className="move-dialog-actions">
            <button
              type="button"
              className="button primary"
              disabled={busy}
              autoFocus
              onClick={() => onConfirm(dest)}
            >
              {confirmText ?? "移动到此"}
            </button>
            <button type="button" className="button ghost" onClick={onClose}>
              取消
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
