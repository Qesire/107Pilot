import { useEffect, useMemo, useState } from "react";

export const LIST_ROW_HEIGHT = 40;
export const MILLER_ROW_HEIGHT = 32;
export const GRID_TILE_HEIGHT = 108;
export const GRID_GAP = 8;
export const GRID_ROW_HEIGHT = GRID_TILE_HEIGHT + GRID_GAP;
export const GRID_MIN_TILE_WIDTH = 96;

export interface VirtualRange {
  start: number;
  end: number;
  paddingBefore: number;
  paddingAfter: number;
}

export function computeVirtualRange(
  count: number,
  itemSize: number,
  scrollOffset: number,
  viewportSize: number,
  overscan = 8,
): VirtualRange {
  if (count <= 0 || itemSize <= 0) {
    return { start: 0, end: 0, paddingBefore: 0, paddingAfter: 0 };
  }
  const safeOffset = Math.max(0, scrollOffset);
  const safeViewport = Math.max(itemSize, viewportSize);
  const visibleStart = Math.floor(safeOffset / itemSize);
  const visibleEnd = Math.ceil((safeOffset + safeViewport) / itemSize);
  const start = Math.max(0, visibleStart - overscan);
  const end = Math.min(count, visibleEnd + overscan);
  return {
    start,
    end,
    paddingBefore: start * itemSize,
    paddingAfter: Math.max(0, (count - end) * itemSize),
  };
}

export function useVirtualWindow(
  scrollElement: HTMLElement | null,
  count: number,
  itemSize: number,
  overscan = 8,
): VirtualRange {
  const [viewport, setViewport] = useState({ scrollOffset: 0, size: 720 });

  useEffect(() => {
    if (!scrollElement) return;
    const update = () => {
      setViewport({
        scrollOffset: scrollElement.scrollTop,
        size: scrollElement.clientHeight || 720,
      });
    };
    update();
    scrollElement.addEventListener("scroll", update, { passive: true });
    const resize = new ResizeObserver(update);
    resize.observe(scrollElement);
    return () => {
      scrollElement.removeEventListener("scroll", update);
      resize.disconnect();
    };
  }, [scrollElement]);

  return useMemo(
    () => computeVirtualRange(count, itemSize, viewport.scrollOffset, viewport.size, overscan),
    [count, itemSize, overscan, viewport.scrollOffset, viewport.size],
  );
}

export function useElementWidth(element: HTMLElement | null, fallback = 800): number {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    if (!element) return;
    const update = () => setWidth(element.clientWidth || fallback);
    update();
    const resize = new ResizeObserver(update);
    resize.observe(element);
    return () => resize.disconnect();
  }, [element, fallback]);
  return width;
}
