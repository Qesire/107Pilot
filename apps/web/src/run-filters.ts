export interface SavedRunFilters {
  state: string;
  search: string;
}

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const runStates = new Set(["", "RUNNING", "PENDING", "SUCCEEDED", "FAILED", "CANCELLED"]);

function key(user: string): string {
  return `pilot107.run-filters.v1.${user}`;
}

export function loadRunFilters(user: string, storage: StorageLike = window.localStorage): SavedRunFilters | null {
  try {
    const raw = storage.getItem(key(user));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { state?: unknown; search?: unknown };
    if (typeof parsed.state !== "string" || !runStates.has(parsed.state)) return null;
    if (typeof parsed.search !== "string" || parsed.search.length > 256) return null;
    return { state: parsed.state, search: parsed.search };
  } catch {
    return null;
  }
}

export function saveRunFilters(
  user: string,
  filters: SavedRunFilters,
  storage: StorageLike = window.localStorage,
): void {
  storage.setItem(key(user), JSON.stringify(filters));
}

export function clearRunFilters(user: string, storage: StorageLike = window.localStorage): void {
  storage.removeItem(key(user));
}
