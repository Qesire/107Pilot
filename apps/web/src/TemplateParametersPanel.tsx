import { useState } from "react";
import {
  extraParameterFields,
  fieldWriteKeys,
  readFieldValue,
  type ParameterField,
} from "./template-schema";
import { FilePickerDialog } from "./files/FilePickerDialog";
import { SelectField, TextField } from "./StudioPage";
import type { JsonObject } from "./types";

/**
 * Renders recipe `parameter_schema` fields that BasicProjection does not
 * already cover (e.g. `runtime.environment.KIT_ROOT`, `resources.array`,
 * `resources.gpu_type`). Paths map directly onto canonical contract fields,
 * so every control writes through the shared `update` path.
 */
export function TemplateExtraParameters({ user, contract, schema, update }: {
  user: string;
  contract: JsonObject;
  schema: unknown;
  update: (path: readonly string[], value: unknown) => void;
}) {
  const fields = extraParameterFields(schema);
  const [pickerField, setPickerField] = useState<ParameterField | null>(null);
  const homePath = `/public/home/${user}`;
  if (fields.length === 0) return null;
  return (
    <>
      <fieldset className="field-group">
        <legend>模板参数（来自 Recipe schema）</legend>
        <div className="form-grid two">
          {fields.map((field) => (
            <SchemaFieldControl
              key={field.path}
              field={field}
              value={readFieldValue(contract, field)}
              onChange={(value) => update(fieldWriteKeys(field), value)}
              onBrowse={field.type === "shared_path" ? () => setPickerField(field) : undefined}
            />
          ))}
        </div>
      </fieldset>
      {pickerField ? (
        <FilePickerDialog
          user={user}
          homePath={homePath}
          initialPath={homePath}
          title={`选择共享路径 · ${pickerField.path}`}
          selectionMode="path"
          onSelect={(path) => {
            update(fieldWriteKeys(pickerField), path);
            setPickerField(null);
          }}
          onClose={() => setPickerField(null)}
        />
      ) : null}
    </>
  );
}

/** One schema-driven control; unknown field types fall back to a text input. */
export function SchemaFieldControl({ field, value, onChange, onBrowse }: {
  field: ParameterField;
  value: string;
  onChange: (value: string) => void;
  onBrowse?: (() => void) | undefined;
}) {
  const label = `${field.path}${field.required ? "（必填）" : ""}`;
  const detail = field.contract ?? undefined;
  if (field.type === "enum" && field.allowed.length > 0) {
    const options = field.allowed.map((item) => ({ value: item, label: item }));
    if (value && !field.allowed.includes(value)) {
      options.unshift({ value, label: `${value} · 当前值（不在 allowed 列表）` });
    }
    return (
      <SelectField
        label={label}
        value={value}
        onChange={onChange}
        options={[{ value: "", label: "请选择" }, ...options]}
      />
    );
  }
  if (field.type === "shared_path") {
    return (
      <div className="path-field-browser">
        <TextField
          label={label}
          value={value}
          onChange={onChange}
          placeholder={field.prefix ?? undefined}
          detail={detail}
        />
        <button type="button" className="button secondary" aria-label={`浏览 ${field.path}`} onClick={onBrowse}>
          浏览…
        </button>
      </div>
    );
  }
  if (field.type === "slurm_time") {
    return (
      <TextField
        label={label}
        value={value}
        onChange={onChange}
        placeholder="HH:MM:SS"
        detail={detail ?? "Slurm 时限格式 HH:MM:SS（或 D-HH:MM:SS）。"}
      />
    );
  }
  return (
    <TextField
      label={label}
      value={value}
      onChange={onChange}
      detail={detail ?? (field.type !== "text" ? `类型：${field.type}` : undefined)}
    />
  );
}
