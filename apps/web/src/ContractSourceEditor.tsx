import CodeMirror from "@uiw/react-codemirror";
import { useMemo } from "react";
import { autocompletion, type Completion, type CompletionContext } from "@codemirror/autocomplete";
import { json as jsonLanguage } from "@codemirror/lang-json";
import { yaml as yamlLanguage } from "@codemirror/lang-yaml";
import { linter, type Diagnostic } from "@codemirror/lint";
import type { Extension } from "@codemirror/state";
import { parseContractSource, type SourceFormat } from "./contract-state";
import { compileClientSchemaValidator, type ClientSchemaValidator } from "./schema-validation";
import type { JsonObject } from "./types";

export default function ContractSourceEditor({
  format,
  schema,
  source,
  onChange,
}: {
  format: SourceFormat;
  schema: JsonObject;
  source: string;
  onChange: (value: string) => void;
}) {
  const extensions = useMemo(() => contractEditorExtensions(format, schema), [format, schema]);
  return (
    <CodeMirror
      value={source}
      height="560px"
      extensions={extensions}
      basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true }}
      onChange={onChange}
    />
  );
}

function contractEditorExtensions(format: SourceFormat, schema: JsonObject): Extension[] {
  const validate = compileEditorValidator(schema);
  const completions = collectSchemaCompletions(schema);
  return [
    format === "json" ? jsonLanguage() : yamlLanguage(),
    autocompletion({ override: [(context) => completeContractField(context, completions, format)] }),
    linter((view) => sourceDiagnostics(view.state.doc.toString(), format, validate)),
  ];
}

export function compileEditorValidator(schema: JsonObject): ClientSchemaValidator | null {
  return compileClientSchemaValidator(schema);
}

export function collectSchemaCompletions(schema: JsonObject): Completion[] {
  const found = new Map<string, Completion>();
  const visit = (node: unknown, prefix: string) => {
    if (typeof node !== "object" || node === null || Array.isArray(node)) return;
    const properties = (node as JsonObject).properties;
    if (typeof properties !== "object" || properties === null || Array.isArray(properties)) return;
    Object.entries(properties as JsonObject).forEach(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (!found.has(key)) found.set(key, { label: key, detail: path, type: "property" });
      visit(child, path);
    });
  };
  visit(schema, "");
  return [...found.values()].sort((left, right) => left.label.localeCompare(right.label));
}

function completeContractField(context: CompletionContext, options: Completion[], format: SourceFormat) {
  const word = context.matchBefore(/[A-Za-z0-9_.-]*/);
  if (!word || (word.from === word.to && !context.explicit)) return null;
  return {
    from: word.from,
    options: options.map((option) => ({
      ...option,
      apply: format === "json" ? `"${option.label}": ` : `${option.label}: `,
    })),
  };
}

export function sourceDiagnostics(source: string, format: SourceFormat, validate: ClientSchemaValidator | null): Diagnostic[] {
  let contract: JsonObject;
  try {
    contract = parseContractSource(source, format);
  } catch (error) {
    const position = sourceErrorPosition(error, source);
    return [{ from: position, to: Math.min(position + 1, source.length), severity: "error", message: error instanceof Error ? error.message : "源码解析失败" }];
  }
  const errors = validate?.(contract) ?? [];
  if (!errors.length) return [];
  return errors.slice(0, 20).map((error) => {
    const key = error.instancePath.split("/").filter(Boolean).at(-1);
    const position = key ? findSourceKey(source, key, format) : 0;
    return {
      from: position,
      to: Math.min(position + Math.max(key?.length ?? 1, 1), source.length),
      severity: "error" as const,
      message: `${error.instancePath || "/"} ${error.message ?? error.keyword}`,
    };
  });
}

function sourceErrorPosition(error: unknown, source: string): number {
  if (typeof error === "object" && error !== null && "pos" in error) {
    const raw = (error as { pos?: unknown }).pos;
    if (Array.isArray(raw) && typeof raw[0] === "number") return Math.min(raw[0], source.length);
    if (typeof raw === "number") return Math.min(raw, source.length);
  }
  const message = error instanceof Error ? error.message : "";
  const match = /position\s+(\d+)/i.exec(message);
  return match?.[1] ? Math.min(Number(match[1]), source.length) : 0;
}

function findSourceKey(source: string, key: string, format: SourceFormat): number {
  const quoted = format === "json" ? `"${key}"` : key;
  const position = source.indexOf(quoted);
  return position >= 0 ? position : 0;
}
