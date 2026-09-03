import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

type Point = { x: number; y: number };
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

    const down = (event: MouseEvent) => {
      if (event.button !== 0) return;
      const target = event.target as Element | null;
      if (target?.closest(blockedStartSelector)) return;
      startRef.current = { x: event.clientX, y: event.clientY };
    };

    const move = (event: MouseEvent) => {
      const start = startRef.current;
      if (!start) return;
      const next = rectangle(start, event.clientX, event.clientY);
      if (next.width >= 2 || next.height >= 2) {
        event.preventDefault();
        setRect(next);
      }
    };

    const finish = (event: MouseEvent) => {
      const start = startRef.current;
      if (!start) return;
      const finalRect = rectangle(start, event.clientX, event.clientY);
      reset();
      if (finalRect.width < 4 && finalRect.height < 4) return;
      const paths = Array.from(rootElement.querySelectorAll(itemSelector))
        .filter((element) => intersects(finalRect, element))
        .map((element) => element.getAttribute("data-path"))
        .filter((path): path is string => Boolean(path));
      onSelect(paths, event.shiftKey || event.metaKey || event.ctrlKey);
    };

    surfaceElement.addEventListener("mousedown", down, true);
    window.addEventListener("mousemove", move, true);
    window.addEventListener("mouseup", finish, true);
    return () => {
      surfaceElement.removeEventListener("mousedown", down, true);
      window.removeEventListener("mousemove", move, true);
      window.removeEventListener("mouseup", finish, true);
    };
  }, [blockedStartSelector, itemSelector, onSelect, reset, rootElement, surfaceElement]);

  const marqueeStyle: CSSProperties | null = rect
    ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
    : null;

  return { marqueeStyle };
}
