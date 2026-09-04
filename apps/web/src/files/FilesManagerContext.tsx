import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { FileEntry } from "../types";
import { computeMoveTargets, normalizeDir, parentPath } from "./selection";

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
  openPath: (path: string, selectedPath?: string) => void;
}

export interface FilesManager {
  user: string;
  homePath: string;
  activePaneId: string | null;
  activePath: string;
  activeSelection: FileEntry[];
  sessionPaths: string[];
  setActivePane: (paneId: string) => void;
  setPaneSelection: (paneId: string, entries: FileEntry[]) => void;
  registerPane: (controller: PaneController) => void;
  unregisterPane: (paneId: string) => void;
  getController: (paneId: string) => PaneController | undefined;
  getActiveController: () => PaneController | undefined;
  setPanePath: (paneId: string, path: string) => void;
  openPath: (path: string, selectedPath?: string) => void;
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
  const queryClient = useQueryClient();
  const [activePaneId, setActivePaneId] = useState<string | null>(null);
  const [panePaths, setPanePaths] = useState<Record<string, string>>({});
  const [paneSelections, setPaneSelections] = useState<Record<string, FileEntry[]>>({});
  const [sessionPaths, setSessionPaths] = useState<string[]>([]);
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
    setPanePaths((current) => {
      const next = { ...current };
      delete next[paneId];
      return next;
    });
    setPaneSelections((current) => {
      if (!(paneId in current)) return current;
      const next = { ...current };
      delete next[paneId];
      return next;
    });
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

  const setPanePath = useCallback((paneId: string, path: string) => {
    setPanePaths((current) => current[paneId] === path
      ? current
      : { ...current, [paneId]: path });
    if (path !== homePath) {
      setSessionPaths((current) => [path, ...current.filter((item) => item !== path)].slice(0, 6));
    }
  }, [homePath]);

  const setPaneSelection = useCallback((paneId: string, entries: FileEntry[]) => {
    setPaneSelections((current) => {
      const previous = current[paneId] ?? [];
      const unchanged = previous.length === entries.length && previous.every((entry, index) => {
        const next = entries[index];
        return next !== undefined
          && entry.path === next.path
          && entry.kind === next.kind
          && entry.size === next.size
          && entry.modified === next.modified;
      });
      return unchanged ? current : { ...current, [paneId]: entries };
    });
  }, []);

  const openPath = useCallback((path: string, selectedPath?: string) => {
    const controller = activePaneId
      ? controllers.current.get(activePaneId)
      : controllers.current.values().next().value;
    controller?.openPath(path, selectedPath);
  }, [activePaneId]);

  const activePath = activePaneId
    ? panePaths[activePaneId] ?? controllers.current.get(activePaneId)?.getCwd() ?? homePath
    : homePath;
  const activeSelection = activePaneId ? paneSelections[activePaneId] ?? [] : [];

  const invalidateFilePaths = useCallback((paths: string[]) => {
    for (const path of new Set(paths.map(normalizeDir))) {
      void queryClient.invalidateQueries({
        queryKey: ["files-list", user, path],
        exact: true,
      });
    }
  }, [queryClient, user]);

  const moveEntries = useCallback(
    async (entries: FileEntry[], destDir: string): Promise<number> => {
      const targets = computeMoveTargets(entries, destDir);
      for (const target of targets) {
        await api.fileRename(user, target.from, target.to);
      }
      if (targets.length > 0) {
        invalidateFilePaths([
          destDir,
          ...targets.map((target) => parentPath(target.from)),
        ]);
        void queryClient.invalidateQueries({ queryKey: ["files-usage", user] });
      }
      return targets.length;
    },
    [invalidateFilePaths, queryClient, user],
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
      activePath,
      activeSelection,
      sessionPaths,
      setActivePane,
      setPaneSelection,
      registerPane,
      unregisterPane,
      getController,
      getActiveController,
      setPanePath,
      openPath,
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
      activePath,
      activeSelection,
      sessionPaths,
      setActivePane,
      setPaneSelection,
      registerPane,
      unregisterPane,
      getController,
      getActiveController,
      setPanePath,
      openPath,
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
