import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "../api";
import type { FileEntry } from "../types";
import { computeMoveTargets } from "./selection";

export type PaneViewMode = "grid" | "list" | "column";

/** Imperative surface each FilePane exposes to the manager (global toolbar,
 * cross-pane coordination). Stored in a ref-map, not React state, so panes can
 * register/unregister without triggering manager re-renders. */
export interface PaneController {
  paneId: string;
  getCwd: () => string;
  getSelectedEntries: () => FileEntry[];
  clearSelection: () => void;
  refresh: () => void;
  getViewMode: () => PaneViewMode;
  setViewMode: (mode: PaneViewMode) => void;
  openMkdir: () => void;
}

export interface FilesManager {
  user: string;
  homePath: string;
  activePaneId: string | null;
  setActivePane: (paneId: string) => void;
  registerPane: (controller: PaneController) => void;
  unregisterPane: (paneId: string) => void;
  getController: (paneId: string) => PaneController | undefined;
  getActiveController: () => PaneController | undefined;
  /** Move entries into destDir via rename; returns the number moved. */
  moveEntries: (entries: FileEntry[], destDir: string) => Promise<number>;
  /** Entries currently being dragged. Stored in a ref (not state) so the value
   * is synchronously visible to a drop handler in another pane, with no
   * re-render timing race between dragstart and drop. */
  getDragPayload: () => DragPayload | null;
  setDragPayload: (payload: DragPayload | null) => void;
  /** Open the native file picker targeting the active pane's cwd. */
  requestUpload: () => void;
  setUploadTrigger: (trigger: (() => void) | null) => void;
}

/** What is being dragged and from which pane (so the source can refresh after
 * a cross-pane move). */
export interface DragPayload {
  sourcePaneId: string;
  entries: FileEntry[];
}

const FilesManagerContext = createContext<FilesManager | null>(null);

export function FilesManagerProvider({
  user,
  homePath,
  children,
}: {
  user: string;
  homePath: string;
  children: ReactNode;
}) {
  const [activePaneId, setActivePaneId] = useState<string | null>(null);
  const dragPayloadRef = useRef<DragPayload | null>(null);
  const controllers = useRef<Map<string, PaneController>>(new Map());
  const uploadTrigger = useRef<(() => void) | null>(null);

  const setActivePane = useCallback((paneId: string) => setActivePaneId(paneId), []);

  const registerPane = useCallback((controller: PaneController) => {
    controllers.current.set(controller.paneId, controller);
    setActivePaneId((prev) => prev ?? controller.paneId);
  }, []);

  const unregisterPane = useCallback((paneId: string) => {
    controllers.current.delete(paneId);
    setActivePaneId((prev) => (prev === paneId ? null : prev));
  }, []);

  const getController = useCallback(
    (paneId: string) => controllers.current.get(paneId),
    [],
  );

  const getActiveController = useCallback(
    () => (activePaneId ? controllers.current.get(activePaneId) : undefined),
    [activePaneId],
  );

  const moveEntries = useCallback(
    async (entries: FileEntry[], destDir: string): Promise<number> => {
      const targets = computeMoveTargets(entries, destDir);
      for (const target of targets) {
        await api.fileRename(user, target.from, target.to);
      }
      return targets.length;
    },
    [user],
  );

  const setDragPayload = useCallback((payload: DragPayload | null) => {
    dragPayloadRef.current = payload;
  }, []);

  const getDragPayload = useCallback(() => dragPayloadRef.current, []);

  const requestUpload = useCallback(() => {
    uploadTrigger.current?.();
  }, []);

  const setUploadTrigger = useCallback((trigger: (() => void) | null) => {
    uploadTrigger.current = trigger;
  }, []);

  const value = useMemo<FilesManager>(
    () => ({
      user,
      homePath,
      activePaneId,
      setActivePane,
      registerPane,
      unregisterPane,
      getController,
      getActiveController,
      moveEntries,
      getDragPayload,
      setDragPayload,
      requestUpload,
      setUploadTrigger,
    }),
    [
      user,
      homePath,
      activePaneId,
      setActivePane,
      registerPane,
      unregisterPane,
      getController,
      getActiveController,
      moveEntries,
      getDragPayload,
      setDragPayload,
      requestUpload,
      setUploadTrigger,
    ],
  );

  return (
    <FilesManagerContext.Provider value={value}>
      {children}
    </FilesManagerContext.Provider>
  );
}

export function useFilesManager(): FilesManager {
  const ctx = useContext(FilesManagerContext);
  if (!ctx) throw new Error("useFilesManager must be used within FilesManagerProvider");
  return ctx;
}
