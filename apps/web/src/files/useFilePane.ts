import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { FileEntry } from "../types";
import { useFilesManager, type PaneViewMode } from "./FilesManagerContext";
import { fileDirectoryQueryKey, useFileDirectoryListing } from "./useFileDirectoryListing";
import {
  clampToHome,
  computeMoveTargets,
  invertSelection,
  joinPath,
  normalizeDir,
  parentPath,
  selectAllPaths,
  toggleSelection,
} from "./selection";

/** The pane's inline name form: creating a directory or a new file. */
export type InlineFormMode = "mkdir" | "newfile" | null;

export interface UseFilePaneResult {
  paneId: string;
  home: string;
  cwd: string;
  entries: FileEntry[];
  isPending: boolean;
  isError: boolean;
  isFetching: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
  error: Error | null;
  selected: string[];
  selectedEntries: FileEntry[];
  viewMode: PaneViewMode;
  canBack: boolean;
  canForward: boolean;
  canUp: boolean;
  inlineForm: InlineFormMode;
  busy: boolean;
  renamingPath: string | null;
  navigateTo: (path: string) => void;
  goBack: () => void;
  goForward: () => void;
  goUp: () => void;
  toggleSelect: (path: string) => void;
  selectAll: () => void;
  invertSelect: () => void;
  clearSelection: () => void;
  setSelection: (paths: string[]) => void;
  setViewMode: (mode: PaneViewMode) => void;
  setInlineForm: (mode: InlineFormMode) => void;
  setRenamingPath: (path: string | null) => void;
  createDir: (name: string) => void;
  createFile: (name: string) => void;
  deleteEntries: (paths: string[]) => Promise<void>;
  archiveEntries: (paths: string[]) => Promise<void>;
  renameEntry: (path: string, newName: string) => Promise<void>;
  moveEntriesTo: (paths: string[], destDir: string) => Promise<number>;
  copyEntriesTo: (paths: string[], destDir: string) => Promise<void>;
  extractEntry: (path: string) => Promise<void>;
  refresh: () => void;
}

/**
 * Independent browsing context for a single pane (the QSpace core): its own
 * cwd, back/forward history, view mode and selection. Registers an imperative
 * controller with the FilesManager so the global toolbar and cross-pane moves
 * can drive it.
 */
export function useFilePane(paneId: string, initialPath: string): UseFilePaneResult {
  const manager = useFilesManager();
  const queryClient = useQueryClient();

  // The command gateway only serves listings at/below the home root, so all
  // navigation is clamped to it. This security boundary must stay explicit.
  const home = normalizeDir(initialPath);

  const [cwd, setCwd] = useState(initialPath);
  const [backStack, setBackStack] = useState<string[]>([]);
  const [forwardStack, setForwardStack] = useState<string[]>([]);
  const [viewMode, setViewModeState] = useState<PaneViewMode>("list");
  const [selected, setSelected] = useState<string[]>([]);
  const [inlineForm, setInlineForm] = useState<InlineFormMode>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const pendingOpenRef = useRef<{ cwd: string; selectedPath: string } | null>(null);

  const listing = useFileDirectoryListing(manager.user, cwd);

  const invalidatePaths = useCallback((paths: string[]) => {
    for (const path of new Set(paths.map(normalizeDir))) {
      void queryClient.invalidateQueries({
        queryKey: fileDirectoryQueryKey(manager.user, path),
        exact: true,
      });
    }
  }, [manager.user, queryClient]);

  const invalidateUsage = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["files-usage", manager.user] });
  }, [manager.user, queryClient]);

  const invalidateCurrent = useCallback(() => invalidatePaths([cwd]), [cwd, invalidatePaths]);

  const navigateTo = useCallback((path: string) => {
    const target = clampToHome(path, home);
    setCwd((current) => {
      if (current === target) return current;
      setBackStack((stack) => [...stack, current]);
      setForwardStack([]);
      return target;
    });
    setSelected([]);
  }, [home]);

  const openPath = useCallback((path: string, selectedPath?: string) => {
    const target = clampToHome(path, home);
    pendingOpenRef.current = selectedPath ? { cwd: target, selectedPath } : null;
    navigateTo(target);
  }, [home, navigateTo]);

  const goBack = useCallback(() => {
    setBackStack((stack) => {
      const previous = stack[stack.length - 1];
      if (previous === undefined) return stack;
      setCwd((current) => {
        setForwardStack((fwd) => [...fwd, current]);
        return previous;
      });
      return stack.slice(0, -1);
    });
    setSelected([]);
  }, []);

  const goForward = useCallback(() => {
    setForwardStack((stack) => {
      const next = stack[stack.length - 1];
      if (next === undefined) return stack;
      setCwd((current) => {
        setBackStack((back) => [...back, current]);
        return next;
      });
      return stack.slice(0, -1);
    });
    setSelected([]);
  }, []);

  const goUp = useCallback(() => {
    const parent = parentPath(cwd);
    if (parent !== cwd) navigateTo(parent);
  }, [cwd, navigateTo]);

  const toggleSelect = useCallback((path: string) => {
    setSelected((prev) => toggleSelection(prev, path));
  }, []);

  const entries = listing.entries;

  const selectAll = useCallback(() => {
    setSelected(selectAllPaths(entries));
  }, [entries]);

  const invertSelect = useCallback(() => {
    setSelected((prev) => invertSelection(entries, prev));
  }, [entries]);

  const clearSelection = useCallback(() => setSelected([]), []);
  const setSelection = useCallback((paths: string[]) => setSelected(paths), []);
  const setViewMode = useCallback((mode: PaneViewMode) => setViewModeState(mode), []);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const selectedEntries = useMemo(
    () => entries.filter((entry) => selectedSet.has(entry.path)),
    [entries, selectedSet],
  );

  useEffect(() => {
    manager.setPaneSelection(paneId, selectedEntries);
  }, [manager.setPaneSelection, paneId, selectedEntries]);

  useEffect(() => {
    manager.setPanePath(paneId, cwd);
  }, [cwd, manager.setPanePath, paneId]);

  useEffect(() => {
    const pending = pendingOpenRef.current;
    if (!pending || pending.cwd !== cwd || listing.isPending) return;
    if (!listing.isError && entries.some((entry) => entry.path === pending.selectedPath)) {
      setSelected([pending.selectedPath]);
    }
    pendingOpenRef.current = null;
  }, [cwd, entries, listing.isError, listing.isPending]);

  const mkdirMutation = useMutation({
    mutationFn: (name: string) =>
      api.fileMkdir(manager.user, `${cwd.replace(/\/+$/, "")}/${name}`),
    onSuccess: () => {
      setInlineForm(null);
      invalidateCurrent();
      invalidateUsage();
    },
  });

  const createFileMutation = useMutation({
    mutationFn: (name: string) => api.fileCreate(manager.user, cwd, name),
    onSuccess: () => {
      setInlineForm(null);
      invalidateCurrent();
      invalidateUsage();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (paths: string[]) => {
      for (const path of paths) await api.fileDelete(manager.user, path);
    },
    onSuccess: () => {
      setSelected([]);
      invalidateCurrent();
      invalidateUsage();
    },
  });

  const archiveMutation = useMutation({
    mutationFn: (paths: string[]) => api.fileArchive(manager.user, paths, cwd),
    onSuccess: () => {
      setSelected([]);
      invalidateCurrent();
      invalidateUsage();
    },
  });

  const renameMutation = useMutation({
    mutationFn: ({ path, newName }: { path: string; newName: string }) =>
      api.fileRename(manager.user, path, joinPath(parentPath(path), newName)),
    onSuccess: (_result, variables) => {
      setRenamingPath(null);
      setSelected([]);
      invalidatePaths([cwd, parentPath(variables.path)]);
    },
  });

  const moveMutation = useMutation({
    mutationFn: async ({ paths, destDir }: { paths: string[]; destDir: string }) => {
      const resolved: FileEntry[] = paths.map((path) => {
        const known = entries.find((entry) => entry.path === path);
        if (known) return known;
        const name = path.split("/").filter(Boolean).pop() ?? path;
        return { name, path, kind: "file", size: 0, modified: "" };
      });
      const targets = computeMoveTargets(resolved, destDir);
      for (const target of targets) {
        await api.fileRename(manager.user, target.from, target.to);
      }
      return targets.length;
    },
    onSuccess: (_count, variables) => {
      setSelected([]);
      invalidatePaths([cwd, variables.destDir, ...variables.paths.map(parentPath)]);
    },
  });

  const copyMutation = useMutation({
    mutationFn: ({ paths, destDir }: { paths: string[]; destDir: string }) =>
      api.fileCopy(manager.user, paths, destDir),
    onSuccess: (_result, variables) => {
      // Sources stay put, so keep the selection and refresh only the target.
      invalidatePaths([variables.destDir]);
      invalidateUsage();
    },
  });

  const extractMutation = useMutation({
    mutationFn: (path: string) => api.fileExtract(manager.user, path),
    onSuccess: () => {
      invalidateCurrent();
      invalidateUsage();
    },
  });

  const refresh = useCallback(() => invalidateCurrent(), [invalidateCurrent]);

  // Keep the latest values available to the imperative controller without
  // re-registering on every render.
  const stateRef = useRef({ cwd, selectedEntries, viewMode });
  stateRef.current = { cwd, selectedEntries, viewMode };

  useEffect(() => {
    manager.registerPane({
      paneId,
      getCwd: () => stateRef.current.cwd,
      getSelectedEntries: () => stateRef.current.selectedEntries,
      clearSelection: () => setSelected([]),
      refresh: () => invalidatePaths([stateRef.current.cwd]),
      getViewMode: () => stateRef.current.viewMode,
      setViewMode: (mode) => setViewModeState(mode),
      openMkdir: () => setInlineForm("mkdir"),
      openPath,
    });
    return () => manager.unregisterPane(paneId);
    // Registration is stable per pane; callbacks read current values via refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paneId]);

  return {
    paneId,
    home,
    cwd,
    entries,
    isPending: listing.isPending,
    isError: listing.isError,
    isFetching: listing.isFetching,
    hasNextPage: Boolean(listing.hasNextPage),
    isFetchingNextPage: listing.isFetchingNextPage,
    fetchNextPage: () => { void listing.fetchNextPage(); },
    error: listing.error as Error | null,
    selected,
    selectedEntries,
    viewMode,
    canBack: backStack.length > 0,
    canForward: forwardStack.length > 0,
    canUp: clampToHome(parentPath(cwd), home) === parentPath(cwd) && parentPath(cwd) !== cwd,
    inlineForm,
    busy:
      mkdirMutation.isPending ||
      createFileMutation.isPending ||
      deleteMutation.isPending ||
      archiveMutation.isPending ||
      renameMutation.isPending ||
      moveMutation.isPending ||
      copyMutation.isPending ||
      extractMutation.isPending,
    renamingPath,
    navigateTo,
    goBack,
    goForward,
    goUp,
    toggleSelect,
    selectAll,
    invertSelect,
    clearSelection,
    setSelection,
    setViewMode,
    setInlineForm,
    setRenamingPath,
    createDir: (name) => mkdirMutation.mutate(name),
    createFile: (name) => createFileMutation.mutate(name),
    deleteEntries: (paths) => deleteMutation.mutateAsync(paths),
    archiveEntries: async (paths) => {
      await archiveMutation.mutateAsync(paths);
    },
    renameEntry: async (path, newName) => {
      await renameMutation.mutateAsync({ path, newName });
    },
    moveEntriesTo: (paths, destDir) => moveMutation.mutateAsync({ paths, destDir }),
    copyEntriesTo: async (paths, destDir) => {
      await copyMutation.mutateAsync({ paths, destDir });
    },
    extractEntry: async (path) => {
      await extractMutation.mutateAsync(path);
    },
    refresh,
  };
}
