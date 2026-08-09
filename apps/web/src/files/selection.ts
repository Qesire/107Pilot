// Pure selection + path helpers for the multi-pane file manager. Kept free of
// React so the logic (toggle / select-all / move-target computation) can be
// unit-tested directly.

import type { FileEntry } from "../types";

/** Strip trailing slashes (but keep a lone "/" as "/"). */
export function normalizeDir(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  return trimmed === "" ? "/" : trimmed;
}

/** Join a directory and a name into a single absolute path. */
export function joinPath(dir: string, name: string): string {
  const base = normalizeDir(dir);
  return base === "/" ? `/${name}` : `${base}/${name}`;
}

/** The parent directory of a path ("/" for top-level entries). */
export function parentPath(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const idx = trimmed.lastIndexOf("/");
  if (idx <= 0) return "/";
  return trimmed.slice(0, idx);
}

/** Breadcrumb segments for a path, starting at the "/" root. */
export function pathSegments(path: string): Array<{ label: string; path: string }> {
  const parts = path.split("/").filter(Boolean);
  const segments: Array<{ label: string; path: string }> = [{ label: "/", path: "/" }];
  let accumulated = "";
  for (const part of parts) {
    accumulated += `/${part}`;
    segments.push({ label: part, path: accumulated });
  }
  return segments;
}

/**
 * Clamp a navigation target to the home root. The command gateway only
 * serves listings at or below home, so any path above it (parent of home,
 * "/public", "/") snaps back to home itself instead of rendering columns
 * that all fail with "path outside allowed roots".
 */
export function clampToHome(path: string, home: string): string {
  const target = normalizeDir(path);
  const root = normalizeDir(home);
  return target === root || target.startsWith(root + "/") ? target : root;
}

/**
 * Miller-column directory chain for a cwd: every directory from `home`
 * (inclusive) down to `cwd` (inclusive). When `cwd` is not underneath
 * `home`, the chain falls back to just the cwd itself so we never render
 * columns for directories the gateway refuses to list.
 */
export function columnDirsFor(cwd: string, home: string): string[] {
  const segments = pathSegments(normalizeDir(cwd));
  const homeDir = normalizeDir(home);
  const homeIndex = homeDir === "/" ? 0 : segments.findIndex((seg) => seg.path === homeDir);
  if (homeIndex < 0) return [normalizeDir(cwd)];
  return segments.slice(homeIndex).map((seg) => seg.path);
}

/** Toggle a path in an immutable selection list (order preserved). */
export function toggleSelection(selected: string[], path: string): string[] {
  return selected.includes(path)
    ? selected.filter((item) => item !== path)
    : [...selected, path];
}

/** Select every entry path (directories included). */
export function selectAllPaths(entries: FileEntry[]): string[] {
  return entries.map((entry) => entry.path);
}

/** Invert a selection: every entry path not currently selected, in listing
 * order. Used by the “反选” (invert selection) action. */
export function invertSelection(entries: FileEntry[], selected: string[]): string[] {
  const selectedSet = new Set(selected);
  return entries.filter((entry) => !selectedSet.has(entry.path)).map((entry) => entry.path);
}

export interface MoveTarget {
  from: string;
  to: string;
  name: string;
}

/**
 * Compute rename targets that move each entry into `destDir`. Entries already
 * living directly in `destDir` are skipped (moving onto itself is a no-op).
 */
export function computeMoveTargets(
  entries: FileEntry[],
  destDir: string,
): MoveTarget[] {
  const dest = normalizeDir(destDir);
  const targets: MoveTarget[] = [];
  for (const entry of entries) {
    if (normalizeDir(parentPath(entry.path)) === dest) continue;
    targets.push({ from: entry.path, to: joinPath(dest, entry.name), name: entry.name });
  }
  return targets;
}

/**
 * True when the file name looks like an archive the command gateway can
 * extract: tar family via Python ``tarfile`` (``r:*``), ``.zip`` via Python
 * ``zipfile``, and ``.rar`` via ``unar`` in the slurm image.
 */
export function isArchiveName(name: string): boolean {
  const lower = name.toLowerCase();
  return [".tar", ".gz", ".tgz", ".bz2", ".xz", ".zip", ".rar"].some((ext) =>
    lower.endsWith(ext),
  );
}

/** Directories-first, then alphabetical, listing order used across panes. */
export function sortEntries(entries: FileEntry[]): FileEntry[] {
  return [...entries].sort((a, b) => {
    if (a.kind === "directory" && b.kind !== "directory") return -1;
    if (a.kind !== "directory" && b.kind === "directory") return 1;
    return a.name.localeCompare(b.name);
  });
}
