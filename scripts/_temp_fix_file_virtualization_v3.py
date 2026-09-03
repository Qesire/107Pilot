from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Marquee gestures belong to the stable scroll viewport, not virtual content.
(ROOT / "apps/web/src/files/useMarqueeSelection.tsx").write_text(
    '''import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

type Point = { x: number; y: number; pointerId: number };
type Rect = { left: number; top: number; width: number; height: number };

function rectangle(start: Point, x: number, y: number): Rect {
  return {
    left: Math.min(start.x, x),
    top: Math.min(start.y, y),
    width: Math.abs(x - start.x),
    height: Math.abs(y - start.y),
  };
}

function intersects(rect: Rect, element: Element): boolean {
  const item = element.getBoundingClientRect();
  return !(
    item.right < rect.left
    || item.left > rect.left + rect.width
    || item.bottom < rect.top
    || item.top > rect.top + rect.height
  );
}

export function useMarqueeSelection({
  rootElement,
  surfaceElement,
  itemSelector,
  blockedStartSelector,
  onSelect,
}: {
  rootElement: HTMLElement | null;
  surfaceElement: HTMLElement | null;
  itemSelector: string;
  blockedStartSelector: string;
  onSelect: (paths: string[], additive: boolean) => void;
}) {
  const startRef = useRef<Point | null>(null);
  const [rect, setRect] = useState<Rect | null>(null);

  const reset = useCallback(() => {
    startRef.current = null;
    setRect(null);
  }, []);

  useEffect(() => {
    if (!rootElement || !surfaceElement) return;

    const down = (event: PointerEvent) => {
      if (event.button !== 0) return;
      const target = event.target as Element | null;
      if (target?.closest(blockedStartSelector)) return;
      startRef.current = {
        x: event.clientX,
        y: event.clientY,
        pointerId: event.pointerId,
      };
    };

    const move = (event: PointerEvent) => {
      const start = startRef.current;
      if (!start || start.pointerId !== event.pointerId) return;
      const next = rectangle(start, event.clientX, event.clientY);
      if (next.width >= 2 || next.height >= 2) {
        event.preventDefault();
        setRect(next);
      }
    };

    const finish = (event: PointerEvent) => {
      const start = startRef.current;
      if (!start || start.pointerId !== event.pointerId) return;
      const finalRect = rectangle(start, event.clientX, event.clientY);
      reset();
      if (finalRect.width < 4 && finalRect.height < 4) return;
      const paths = Array.from(rootElement.querySelectorAll(itemSelector))
        .filter((element) => intersects(finalRect, element))
        .map((element) => element.getAttribute("data-path"))
        .filter((path): path is string => Boolean(path));
      onSelect(paths, event.shiftKey || event.metaKey || event.ctrlKey);
    };

    const cancel = (event: PointerEvent) => {
      if (startRef.current?.pointerId === event.pointerId) reset();
    };

    surfaceElement.addEventListener("pointerdown", down, true);
    window.addEventListener("pointermove", move, true);
    window.addEventListener("pointerup", finish, true);
    window.addEventListener("pointercancel", cancel, true);
    return () => {
      surfaceElement.removeEventListener("pointerdown", down, true);
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", finish, true);
      window.removeEventListener("pointercancel", cancel, true);
    };
  }, [blockedStartSelector, itemSelector, onSelect, reset, rootElement, surfaceElement]);

  const marqueeStyle: CSSProperties | null = rect
    ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
    : null;

  return { marqueeStyle };
}
''',
    encoding="utf-8",
)

# List/grid use .filepane-body as the gesture surface.
for relative in ["FileListView.tsx", "FileGrid.tsx"]:
    path = ROOT / "apps/web/src/files" / relative
    replace_once(
        path,
        '''  const marquee = useMarqueeSelection({
    itemSelector:''',
        '''  const marquee = useMarqueeSelection({
    rootElement: container,
    surfaceElement: scrollElement,
    itemSelector:''',
    )
    text = path.read_text(encoding="utf-8")
    for prop in [
        '      onPointerDownCapture={marquee.onPointerDownCapture}\n',
        '      onPointerMoveCapture={marquee.onPointerMoveCapture}\n',
        '      onPointerUpCapture={marquee.onPointerUpCapture}\n',
        '      onPointerCancelCapture={marquee.onPointerCancelCapture}\n',
    ]:
        text = text.replace(prop, "")
    path.write_text(text, encoding="utf-8")

# Miller uses its own stable columns surface.
miller = ROOT / "apps/web/src/files/MillerColumns.tsx"
replace_once(
    miller,
    '''  const marquee = useMarqueeSelection({
    itemSelector:''',
    '''  const marquee = useMarqueeSelection({
    rootElement: container,
    surfaceElement: container,
    itemSelector:''',
)
text = miller.read_text(encoding="utf-8")
for prop in [
    '      onPointerDownCapture={marquee.onPointerDownCapture}\n',
    '      onPointerMoveCapture={marquee.onPointerMoveCapture}\n',
    '      onPointerUpCapture={marquee.onPointerUpCapture}\n',
    '      onPointerCancelCapture={marquee.onPointerCancelCapture}\n',
]:
    text = text.replace(prop, "")
miller.write_text(text, encoding="utf-8")

# Directory targets know their exact destination. Add a direct drop action.
widgets = ROOT / "apps/web/src/files/entry-widgets.tsx"
replace_once(
    widgets,
    '''  onDragStartEntry: (entry: FileEntry, e: React.DragEvent) => void;
  /** Drag gesture finished — clears the shared drag payload/highlight. */
''',
    '''  onDragStartEntry: (entry: FileEntry, e: React.DragEvent) => void;
  /** Drop an internal file payload directly into a known directory entry. */
  onDropIntoDirectory: (entry: FileEntry, e: React.DragEvent) => void;
  /** Drag gesture finished — clears the shared drag payload/highlight. */
''',
)

pane_path = ROOT / "apps/web/src/files/FilePane.tsx"
replace_once(
    pane_path,
    '''  const handleBodyDrop = useCallback(
    (e: React.DragEvent) => {
      if (e.dataTransfer.types.includes("Files")) return; // page handles upload
      e.preventDefault();
      const dest = dropTargetAt(e.clientX, e.clientY);
      setDropTarget(null);
      const payload = manager.getDragPayload() ?? dataTransferPayload(e.dataTransfer);
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
''',
    '''  const completeInternalDrop = useCallback(
    (dest: string | null, dataTransfer: DataTransfer) => {
      setDropTarget(null);
      const payload = manager.getDragPayload() ?? dataTransferPayload(dataTransfer);
      manager.setDragPayload(null);
      if (!dest || !payload || payload.entries.length === 0) return;
      const sourceController = manager.getController(payload.sourcePaneId);
      void manager.moveEntries(payload.entries, dest).then((moved) => {
        if (moved > 0) {
          sourceController?.refresh();
          if (payload.sourcePaneId !== paneId) pane.refresh();
          pane.clearSelection();
        }
      });
    },
    [manager, pane, paneId],
  );

  const handleBodyDrop = useCallback(
    (e: React.DragEvent) => {
      if (e.dataTransfer.types.includes("Files")) return; // page handles upload
      e.preventDefault();
      completeInternalDrop(dropTargetAt(e.clientX, e.clientY), e.dataTransfer);
    },
    [completeInternalDrop, dropTargetAt],
  );

  const handleDropIntoDirectory = useCallback(
    (entry: FileEntry, e: React.DragEvent) => {
      if (entry.kind !== "directory" || e.dataTransfer.types.includes("Files")) return;
      e.preventDefault();
      e.stopPropagation();
      completeInternalDrop(entry.path, e.dataTransfer);
    },
    [completeInternalDrop],
  );
''',
)
replace_once(
    pane_path,
    '''      onDragStartEntry: (entry, e) => handleEntryDragStart(entry, e),
      onDragEndEntry: () => handleEntryDragEnd(),
''',
    '''      onDragStartEntry: (entry, e) => handleEntryDragStart(entry, e),
      onDropIntoDirectory: (entry, e) => handleDropIntoDirectory(entry, e),
      onDragEndEntry: () => handleEntryDragEnd(),
''',
)
replace_once(
    pane_path,
    '''    [pane, downloadEntry, manager, paneId, handleEntryDragStart, handleEntryDragEnd],
''',
    '''    [
      pane,
      downloadEntry,
      manager,
      paneId,
      handleEntryDragStart,
      handleDropIntoDirectory,
      handleEntryDragEnd,
    ],
''',
)

# Add direct directory dragover/drop handlers to all virtualized entry views.
for relative, marker in [
    ("FileGrid.tsx", '              onDragEnd={actions.onDragEndEntry}\n'),
    ("FileListView.tsx", '              onDragEnd={actions.onDragEndEntry}\n'),
    ("MillerColumns.tsx", '              onDragEnd={actions.onDragEndEntry}\n'),
]:
    path = ROOT / "apps/web/src/files" / relative
    replace_once(
        path,
        marker,
        marker
        + '''              onDragOver={(e) => {
                if (entry.kind !== "directory" || e.dataTransfer.types.includes("Files")) return;
                e.preventDefault();
                e.stopPropagation();
              }}
              onDrop={(e) => actions.onDropIntoDirectory(entry, e)}
''',
    )

print("file virtualization v3 surface/drop fixes staged")
