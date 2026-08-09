import { useCallback, useEffect, useState } from "react";
import {
  closePane as closePaneNode,
  collectPanes,
  defaultLayout,
  parseLayout,
  splitPane as splitPaneNode,
  type LayoutNode,
  type SplitDirection,
} from "./layout";

export const PANE_LAYOUT_STORAGE_KEY = "pilot107.files.panes.v1";

function loadInitial(): LayoutNode {
  if (typeof window === "undefined") return defaultLayout();
  try {
    const raw = window.localStorage.getItem(PANE_LAYOUT_STORAGE_KEY);
    if (raw) {
      const parsed = parseLayout(JSON.parse(raw));
      if (parsed) return parsed;
    }
  } catch {
    // Corrupt storage falls back to the default layout.
  }
  return defaultLayout();
}

export interface PaneLayoutApi {
  layout: LayoutNode;
  paneIds: string[];
  split: (paneId: string, direction: SplitDirection) => string;
  close: (paneId: string) => void;
  reset: () => void;
}

/**
 * Layout-tree state with localStorage persistence. `split` returns the id of
 * the newly created pane so callers can focus/activate it.
 */
export function usePaneLayout(): PaneLayoutApi {
  const [layout, setLayout] = useState<LayoutNode>(loadInitial);

  useEffect(() => {
    try {
      window.localStorage.setItem(PANE_LAYOUT_STORAGE_KEY, JSON.stringify(layout));
    } catch {
      // Storage may be unavailable (private mode); layout still works in-memory.
    }
  }, [layout]);

  const split = useCallback((paneId: string, direction: SplitDirection): string => {
    let created = "";
    setLayout((current) => {
      const result = splitPaneNode(current, paneId, direction);
      created = result.newPaneId;
      return result.root;
    });
    return created;
  }, []);

  const close = useCallback((paneId: string) => {
    setLayout((current) => closePaneNode(current, paneId));
  }, []);

  const reset = useCallback(() => setLayout(defaultLayout()), []);

  return { layout, paneIds: collectPanes(layout), split, close, reset };
}
