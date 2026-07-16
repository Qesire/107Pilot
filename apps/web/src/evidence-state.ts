import type { EvidenceObject, EvidenceObjectPreview, JsonObject } from "./types";

export type EvidenceSelectionTab = "overview" | "logs" | "results" | "diagnosis" | "capsule" | "objects";

export function selectActiveEvidenceObject(
  objects: readonly EvidenceObject[],
  tab: EvidenceSelectionTab,
  requestedObjectId: string | null,
): EvidenceObject | null {
  const requested = objects.find((item) => item.object_id === requestedObjectId) ?? null;
  if (tab === "logs") {
    if (requested?.category === "logs") return requested;
    const logs = objects.filter((item) => item.category === "logs");
    return logs.find((item) => item.logical_path.includes("stdout")) ?? logs[0] ?? null;
  }
  if (tab === "results") return requested?.category === "outputs" ? requested : null;
  if (tab === "objects") return requested;
  return null;
}

export function previewContent(
  payload: EvidenceObjectPreview,
  mode: "log" | "raw",
): string {
  const content = payload.preview.content ?? "";
  if (mode !== "log") return content;
  const parsed = parseJsonObject(content);
  return typeof parsed?.tail === "string" ? parsed.tail : content;
}

export function parseJsonObject(value: string | undefined): JsonObject | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return asObject(parsed);
  } catch {
    return null;
  }
}

export function asObject(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : null;
}

export function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
