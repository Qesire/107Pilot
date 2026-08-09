import { isJsonObject, readContractValue } from "./contract-state";
import type { JsonObject } from "./types";

/**
 * One declared parameter of a recipe `parameter_schema`. Paths are dotted
 * contract paths (e.g. `resources.partition` or `runtime.environment.KIT_ROOT`).
 */
export interface ParameterField {
  path: string;
  keys: string[];
  required: boolean;
  type: string;
  allowed: string[];
  prefix: string | null;
  contract: string | null;
}

/**
 * Dotted paths that BasicProjection already renders as first-class inputs.
 * These are *enhanced* in place (enum select, required marker, hints) instead
 * of being duplicated in the extra "模板参数" fieldset.
 */
export const BASIC_PROJECTION_PATHS = new Set([
  "project.workdir",
  "entry.command",
  "resources.partition",
  "resources.qos",
  "resources.memory",
  "resources.time_limit",
  "resources.gpus_per_node",
]);

/** Split `recipe_id@version` into its two halves; null when malformed. */
export function splitRecipeVersionId(
  recipeVersionId: string,
): { recipeId: string; version: string } | null {
  const atIndex = recipeVersionId.indexOf("@");
  if (atIndex <= 0 || atIndex >= recipeVersionId.length - 1) return null;
  return {
    recipeId: recipeVersionId.slice(0, atIndex),
    version: recipeVersionId.slice(atIndex + 1),
  };
}

/**
 * Parse a recipe `parameter_schema` object into an ordered field list.
 * Tolerant of unknown shapes: non-object schemas yield an empty list, unknown
 * field types keep their raw type string so the UI can fall back to text.
 */
export function parseParameterSchema(schema: unknown): ParameterField[] {
  if (!isJsonObject(schema)) return [];
  const requiredSet = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((item): item is string => typeof item === "string")
      : [],
  );
  const fields: ParameterField[] = [];
  for (const [path, raw] of Object.entries(schema)) {
    if (path === "required") continue;
    const keys = path.split(".").filter(Boolean);
    if (keys.length === 0 || keys.join(".") !== path) continue;
    const meta = isJsonObject(raw) ? raw : {};
    const allowed = Array.isArray(meta.allowed)
      ? meta.allowed.filter((item): item is string => typeof item === "string")
      : [];
    fields.push({
      path,
      keys,
      required: requiredSet.has(path),
      type: typeof meta.type === "string" ? meta.type : "text",
      allowed,
      prefix: typeof meta.prefix === "string" ? meta.prefix : null,
      contract: typeof meta.contract === "string" ? meta.contract : null,
    });
  }
  // Required fields first, then stable declaration order.
  return fields.sort((a, b) => Number(b.required) - Number(a.required));
}

/** Contract path a schema field actually writes to (special cases included). */
export function fieldWriteKeys(field: ParameterField): string[] {
  // `resources.array` is an object in the canonical contract; the user-facing
  // value lives at resources.array.expression.
  if (field.path === "resources.array" || field.type === "slurm_array") {
    return [...field.keys, "expression"];
  }
  return field.keys;
}

/** Fields BasicProjection does not already render. */
export function extraParameterFields(schema: unknown): ParameterField[] {
  return parseParameterSchema(schema).filter((field) => !BASIC_PROJECTION_PATHS.has(field.path));
}

function isMissing(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string" && value.trim() === "") return true;
  return false;
}

/**
 * Client-side check that every `required` schema path has a value in the
 * canonical contract. The server validate endpoint remains authoritative; this
 * only gives immediate inline feedback. Returns human-readable messages keyed by
 * dotted path.
 */
export function validateRequiredParameters(
  contract: JsonObject,
  schema: unknown,
): Array<{ path: string; message: string }> {
  return parseParameterSchema(schema)
    .filter((field) => field.required)
    .map((field) => {
      const keys = fieldWriteKeys(field);
      const value = readContractValue(contract, keys, null);
      if (isMissing(value)) {
        return { path: field.path, message: `必填参数 ${field.path} 尚未填写。` };
      }
      return null;
    })
    .filter((item): item is { path: string; message: string } => item !== null);
}

/** Read the current contract value a schema field maps to (for form inputs). */
export function readFieldValue(contract: JsonObject, field: ParameterField): string {
  const value = readContractValue(contract, fieldWriteKeys(field), "");
  return typeof value === "string" ? value : value === null ? "" : String(value);
}
