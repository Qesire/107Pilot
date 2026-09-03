from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"expected one match in {path}, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")


marquee_path = ROOT / "apps/web/src/files/useMarqueeSelection.tsx"
marquee_path.write_text(
    '''import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";

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
  itemSelector,
  blockedStartSelector,
  onSelect,
}: {
  itemSelector: string;
  blockedStartSelector: string;
  onSelect: (paths: string[], additive: boolean) => void;
}) {
  const startRef = useRef<Point | null>(null);
  const rootRef = useRef<HTMLElement | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const [rect, setRect] = useState<Rect | null>(null);

  const clearGesture = useCallback(() => {
    cleanupRef.current?.();
    cleanupRef.current = null;
    startRef.current = null;
    rootRef.current = null;
    setRect(null);
  }, []);

  useEffect(() => clearGesture, [clearGesture]);

  const onPointerDownCapture = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    const target = event.target as Element | null;
    if (target?.closest(blockedStartSelector)) return;

    clearGesture();
    const start: Point = {
      x: event.clientX,
      y: event.clientY,
      pointerId: event.pointerId,
    };
    const root = event.currentTarget;
    startRef.current = start;
    rootRef.current = root;

    const move = (nativeEvent: PointerEvent) => {
      if (startRef.current?.pointerId !== nativeEvent.pointerId) return;
      const next = rectangle(start, nativeEvent.clientX, nativeEvent.clientY);
      if (next.width >= 2 || next.height >= 2) {
        nativeEvent.preventDefault();
        setRect(next);
      }
    };

    const finish = (nativeEvent: PointerEvent) => {
      if (startRef.current?.pointerId !== nativeEvent.pointerId) return;
      const finalRect = rectangle(start, nativeEvent.clientX, nativeEvent.clientY);
      const activeRoot = rootRef.current;
      const additive = nativeEvent.shiftKey || nativeEvent.metaKey || nativeEvent.ctrlKey;
      clearGesture();
      if (!activeRoot || (finalRect.width < 4 && finalRect.height < 4)) return;
      const paths = Array.from(activeRoot.querySelectorAll(itemSelector))
        .filter((element) => intersects(finalRect, element))
        .map((element) => element.getAttribute("data-path"))
        .filter((path): path is string => Boolean(path));
      onSelect(paths, additive);
    };

    const cancel = (nativeEvent: PointerEvent) => {
      if (startRef.current?.pointerId === nativeEvent.pointerId) clearGesture();
    };

    window.addEventListener("pointermove", move, true);
    window.addEventListener("pointerup", finish, true);
    window.addEventListener("pointercancel", cancel, true);
    cleanupRef.current = () => {
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", finish, true);
      window.removeEventListener("pointercancel", cancel, true);
    };
  }, [blockedStartSelector, clearGesture, itemSelector, onSelect]);

  const marqueeStyle: CSSProperties | null = rect
    ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
    : null;

  return {
    marqueeStyle,
    onPointerDownCapture,
    onPointerMoveCapture: undefined,
    onPointerUpCapture: undefined,
    onPointerCancelCapture: undefined,
  };
}
''',
    encoding="utf-8",
)

spec_path = ROOT / "tests/ui/files.spec.js"
replace_once(
    spec_path,
    '''  // Wait for both panes to be fully rendered (4 tiles each) before dragging.\n  await expect(page.locator(".file-tile")).toHaveCount(8);\n  const panes = page.locator(".file-pane");\n''',
    '''  // Wait for the actual drag source and target in their respective panes.\n  // The fixture may gain unrelated home entries as file-workspace coverage grows.\n  const panes = page.locator(".file-pane");\n  await expect(panes.nth(0).locator(".file-tile", { hasText: "readme.md" })).toBeVisible();\n  await expect(panes.nth(1).locator(".file-tile", { hasText: "data" })).toBeVisible();\n''',
)

print("file virtualization v2 interaction fixes staged")
