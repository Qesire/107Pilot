import type { JsonObject } from "./types";

export interface ClientSchemaIssue {
  instancePath: string;
  keyword: string;
  message: string;
}

export type ClientSchemaValidator = (value: JsonObject) => ClientSchemaIssue[];

/**
 * Validate the declarative Contract schema without runtime code generation.
 *
 * The production Web CSP intentionally forbids `unsafe-eval`; Ajv's runtime
 * compiler therefore cannot run in the browser. This interpreter covers the
 * JSON Schema keywords published by the Contract V2 endpoint. The API remains
 * the authority for complete validation and materialization.
 */
export function compileClientSchemaValidator(schema: JsonObject): ClientSchemaValidator | null {
  if (!Object.keys(schema).length) return null;
  return (value) => validateSchema(value, schema, "");
}

function validateSchema(value: unknown, schema: JsonObject, instancePath: string): ClientSchemaIssue[] {
  const issues: ClientSchemaIssue[] = [];
  const types = normalizeTypes(schema.type);
  if (types.length && !types.some((type) => matchesType(value, type))) {
    return [issue(instancePath, "type", `必须是 ${types.join(" 或 ")}`)];
  }
  if ("const" in schema && !sameJson(value, schema.const)) {
    issues.push(issue(instancePath, "const", "必须使用固定值"));
  }
  if (Array.isArray(schema.enum) && !schema.enum.some((candidate) => sameJson(value, candidate))) {
    issues.push(issue(instancePath, "enum", "不在允许的取值范围内"));
  }
  if (typeof value === "string") {
    if (typeof schema.minLength === "number" && value.length < schema.minLength) {
      issues.push(issue(instancePath, "minLength", `长度至少为 ${schema.minLength}`));
    }
    if (typeof schema.pattern === "string") {
      try {
        if (!new RegExp(schema.pattern).test(value)) {
          issues.push(issue(instancePath, "pattern", "格式不符合要求"));
        }
      } catch {
        issues.push(issue(instancePath, "pattern", "服务器 schema 的 pattern 无效"));
      }
    }
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) {
      issues.push(issue(instancePath, "minimum", `不得小于 ${schema.minimum}`));
    }
    if (typeof schema.maximum === "number" && value > schema.maximum) {
      issues.push(issue(instancePath, "maximum", `不得大于 ${schema.maximum}`));
    }
  }
  if (Array.isArray(value)) {
    if (schema.uniqueItems === true && new Set(value.map((item) => JSON.stringify(item))).size !== value.length) {
      issues.push(issue(instancePath, "uniqueItems", "不能包含重复项"));
    }
    const itemSchema = asJsonObject(schema.items);
    if (itemSchema) {
      value.forEach((item, index) => issues.push(...validateSchema(item, itemSchema, `${instancePath}/${index}`)));
    }
  }
  if (isJsonObject(value)) {
    const required = Array.isArray(schema.required)
      ? schema.required.filter((key): key is string => typeof key === "string")
      : [];
    required.forEach((key) => {
      if (!(key in value)) issues.push(issue(instancePath, "required", `缺少必填字段 ${key}`));
    });
    const properties = asJsonObject(schema.properties) ?? {};
    Object.entries(properties).forEach(([key, child]) => {
      if (!(key in value)) return;
      const childSchema = asJsonObject(child);
      if (childSchema) issues.push(...validateSchema(value[key], childSchema, `${instancePath}/${escapePath(key)}`));
    });
    if (schema.additionalProperties === false) {
      Object.keys(value).forEach((key) => {
        if (!(key in properties)) issues.push(issue(`${instancePath}/${escapePath(key)}`, "additionalProperties", "不是允许的字段"));
      });
    } else {
      const additionalSchema = asJsonObject(schema.additionalProperties);
      if (additionalSchema) {
        Object.entries(value).forEach(([key, child]) => {
          if (!(key in properties)) {
            issues.push(...validateSchema(child, additionalSchema, `${instancePath}/${escapePath(key)}`));
          }
        });
      }
    }
  }
  return issues;
}

function normalizeTypes(value: unknown): string[] {
  if (typeof value === "string") return [value];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function matchesType(value: unknown, type: string): boolean {
  switch (type) {
    case "object": return isJsonObject(value);
    case "array": return Array.isArray(value);
    case "string": return typeof value === "string";
    case "integer": return typeof value === "number" && Number.isInteger(value);
    case "number": return typeof value === "number" && Number.isFinite(value);
    case "boolean": return typeof value === "boolean";
    case "null": return value === null;
    default: return true;
  }
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asJsonObject(value: unknown): JsonObject | null {
  return isJsonObject(value) ? value : null;
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function escapePath(value: string): string {
  return value.replaceAll("~", "~0").replaceAll("/", "~1");
}

function issue(instancePath: string, keyword: string, message: string): ClientSchemaIssue {
  return { instancePath, keyword, message };
}
