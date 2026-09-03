from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1))


Path("apps/web/src/ContractAssetSummary.tsx").write_text(r'''import { File, Folder, Link2 } from "lucide-react";
import { readContractValue } from "./contract-state";
import { parseParameterSchema, readFieldValue } from "./template-schema";
import type { JsonObject } from "./types";
import "./styles/contract-assets-v2.css";

export interface ContractPathReference {
  field: string;
  path: string;
  detail: string | null;
}

export interface ContractAssetSummaryData {
  workdir: string | null;
  sharedPaths: ContractPathReference[];
  expectedOutputs: string[];
  typedOutputCount: number;
}

export function deriveContractAssets(
  contract: JsonObject,
  parameterSchema: unknown,
): ContractAssetSummaryData {
  const rawWorkdir = readContractValue(contract, ["project", "workdir"], "");
  const workdir = typeof rawWorkdir === "string" && rawWorkdir.trim()
    ? rawWorkdir.trim()
    : null;
  const sharedPaths = parseParameterSchema(parameterSchema)
    .filter((field) => field.type === "shared_path" && field.path !== "project.workdir")
    .map((field) => ({
      field: field.path,
      path: readFieldValue(contract, field).trim(),
      detail: field.contract,
    }))
    .filter((item) => item.path.length > 0);
  const expected = readContractValue<unknown[]>(contract, ["outputs", "expected"], []);
  const expectedOutputs = expected
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean);
  const typedOutputCount = expected.filter((item) => typeof item !== "string").length;

  return { workdir, sharedPaths, expectedOutputs, typedOutputCount };
}

function PathRows({
  items,
  empty,
  limit = 4,
}: {
  items: Array<{ key: string; label?: string; path: string; detail?: string | null }>;
  empty: string;
  limit?: number;
}) {
  if (items.length === 0) return <p className="contract-assets-empty">{empty}</p>;
  const visible = items.slice(0, limit);
  return (
    <>
      <ul className="contract-assets-list">
        {visible.map((item) => (
          <li key={item.key}>
            {item.label ? <span>{item.label}</span> : null}
            <code title={item.path}>{item.path}</code>
            {item.detail ? <small>{item.detail}</small> : null}
          </li>
        ))}
      </ul>
      {items.length > visible.length ? (
        <p className="contract-assets-more">另有 {items.length - visible.length} 项，完整值保留在 canonical Contract 中。</p>
      ) : null}
    </>
  );
}

export function ContractAssetSummary({
  contract,
  parameterSchema,
  dirty,
}: {
  contract: JsonObject;
  parameterSchema: unknown;
  dirty: boolean;
}) {
  const assets = deriveContractAssets(contract, parameterSchema);
  const sharedItems = assets.sharedPaths.map((item) => ({
    key: item.field,
    label: item.field,
    path: item.path,
    detail: item.detail,
  }));
  const outputItems = assets.expectedOutputs.map((path, index) => ({
    key: `${index}:${path}`,
    path,
  }));

  return (
    <section className="contract-assets" aria-labelledby="contract-assets-heading">
      <header className="contract-assets-header">
        <div>
          <p className="panel-kicker">Contract → file references</p>
          <h2 id="contract-assets-heading">实验资产</h2>
          <p>从当前 canonical Contract 实时派生。这里只展示路径引用与输出声明，不复制文件，也不表示服务器已经验证路径存在。</p>
        </div>
        <span className={`contract-assets-state${dirty ? " is-dirty" : ""}`}>
          {dirty ? "当前有未持久化修改" : "canonical 派生"}
        </span>
      </header>

      <div className="contract-assets-grid">
        <article className="contract-asset-card">
          <div className="contract-asset-card-heading"><Folder aria-hidden="true" /><div><strong>执行位置</strong><small>工作目录</small></div></div>
          <PathRows
            items={assets.workdir ? [{ key: "workdir", path: assets.workdir }] : []}
            empty="尚未指定工作目录。"
          />
        </article>

        <article className="contract-asset-card">
          <div className="contract-asset-card-heading"><Link2 aria-hidden="true" /><div><strong>共享路径引用</strong><small>Recipe schema 中的 shared_path</small></div></div>
          <PathRows items={sharedItems} empty="当前 Recipe 尚未绑定共享路径。" />
        </article>

        <article className="contract-asset-card">
          <div className="contract-asset-card-heading"><File aria-hidden="true" /><div><strong>输出声明</strong><small>运行后预期产生</small></div></div>
          <PathRows items={outputItems} empty="尚未声明字符串路径输出。" />
          {assets.typedOutputCount > 0 ? (
            <p className="contract-assets-more">另有 {assets.typedOutputCount} 个结构化输出声明，保持原样由 Contract 管理。</p>
          ) : null}
        </article>
      </div>
    </section>
  );
}
''')

Path("apps/web/src/ContractAssetSummary.test.ts").write_text(r'''import { describe, expect, it } from "vitest";
import { deriveContractAssets } from "./ContractAssetSummary";
import type { JsonObject } from "./types";

const contract: JsonObject = {
  project: { workdir: "/public/home/alice/exp-a" },
  runtime: { environment: { DATA_ROOT: "/public/home/alice/data", KIT_ROOT: "" } },
  outputs: {
    expected: ["outputs/result.json", " logs/train.log ", { kind: "metric", path: "metrics.json" }],
  },
};

const schema = {
  required: ["runtime.environment.DATA_ROOT"],
  "project.workdir": { type: "shared_path" },
  "runtime.environment.DATA_ROOT": { type: "shared_path", contract: "existing dataset" },
  "runtime.environment.KIT_ROOT": { type: "shared_path" },
  "resources.partition": { type: "enum", allowed: ["Students"] },
};

describe("deriveContractAssets", () => {
  it("derives workdir, populated shared paths, and output declarations without duplicating workdir", () => {
    expect(deriveContractAssets(contract, schema)).toEqual({
      workdir: "/public/home/alice/exp-a",
      sharedPaths: [{
        field: "runtime.environment.DATA_ROOT",
        path: "/public/home/alice/data",
        detail: "existing dataset",
      }],
      expectedOutputs: ["outputs/result.json", "logs/train.log"],
      typedOutputCount: 1,
    });
  });

  it("does not invent references from missing or unknown schema fields", () => {
    expect(deriveContractAssets({}, null)).toEqual({
      workdir: null,
      sharedPaths: [],
      expectedOutputs: [],
      typedOutputCount: 0,
    });
  });
});
''')

Path("apps/web/src/styles/contract-assets-v2.css").write_text(r'''.contract-assets {
  display: grid;
  gap: 14px;
  margin-bottom: 18px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-panel);
  background: var(--bg-surface);
  padding: 16px;
}

.contract-assets-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.contract-assets-header h2 {
  margin: 0;
  color: var(--text-primary);
  font-size: 16px;
}

.contract-assets-header > div > p:last-child {
  max-width: 760px;
  margin: 5px 0 0;
  color: var(--text-tertiary);
  font-size: 12px;
  line-height: 1.55;
}

.contract-assets-state {
  flex: none;
  border: 1px solid var(--border-default);
  border-radius: 999px;
  background: var(--bg-subtle);
  color: var(--text-secondary);
  padding: 5px 9px;
  font-size: 11px;
  font-weight: 650;
}

.contract-assets-state.is-dirty {
  border-color: color-mix(in srgb, var(--state-warning) 38%, var(--border-default));
  background: color-mix(in srgb, var(--state-warning) 8%, var(--bg-surface));
  color: var(--state-warning);
}

.contract-assets-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.contract-asset-card {
  min-width: 0;
  border: 1px solid var(--border-default);
  border-radius: 10px;
  background: var(--bg-subtle);
  padding: 12px;
}

.contract-asset-card-heading {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 10px;
}

.contract-asset-card-heading > svg {
  width: 17px;
  flex: none;
  color: var(--accent-primary);
}

.contract-asset-card-heading > div {
  display: grid;
  min-width: 0;
  gap: 1px;
}

.contract-asset-card-heading strong {
  color: var(--text-primary);
  font-size: 12px;
}

.contract-asset-card-heading small,
.contract-assets-more,
.contract-assets-empty {
  color: var(--text-tertiary);
  font-size: 11px;
}

.contract-assets-list {
  display: grid;
  gap: 7px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.contract-assets-list li {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.contract-assets-list span {
  overflow: hidden;
  color: var(--text-tertiary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.contract-assets-list code {
  overflow: hidden;
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.contract-assets-list small {
  overflow: hidden;
  color: var(--text-tertiary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.contract-assets-more,
.contract-assets-empty {
  margin: 7px 0 0;
  line-height: 1.45;
}

@media (max-width: 1100px) {
  .contract-assets-grid { grid-template-columns: 1fr; }
}

@media (max-width: 760px) {
  .contract-assets-header { flex-direction: column; gap: 10px; }
  .contract-assets-state { align-self: flex-start; }
}
''')

replace_once(
    "apps/web/src/StudioPage.tsx",
    'import { QueryBoundary, SectionHeading, StatusBadge } from "./components";\n',
    'import { QueryBoundary, SectionHeading, StatusBadge } from "./components";\nimport { ContractAssetSummary } from "./ContractAssetSummary";\n',
)
replace_once(
    "apps/web/src/StudioPage.tsx",
    '''          {validation.isError || creation.isError ? (
            <div className="studio-notice error" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>服务器拒绝请求</strong><p>{(validation.error ?? creation.error)?.message}</p></div></div>
          ) : null}

          <div className="studio-body-3col">
''',
    '''          {validation.isError || creation.isError ? (
            <div className="studio-notice error" role="alert"><AlertTriangle aria-hidden="true" /><div><strong>服务器拒绝请求</strong><p>{(validation.error ?? creation.error)?.message}</p></div></div>
          ) : null}

          <ContractAssetSummary contract={canonical} parameterSchema={parameterSchema} dirty={canonicalDirty} />

          <div className="studio-body-3col">
''',
)

visual = Path("tests/ui/visual.spec.js")
text = visual.read_text()
old_workdir = '''  await page.getByRole("button", { name: "选择此目录" }).click();
  await expect(page.getByRole("textbox", { name: "工作目录", exact: true })).toHaveValue("/public/home/alice/project-a");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
'''
new_workdir = '''  await page.getByRole("button", { name: "选择此目录" }).click();
  await expect(page.getByRole("textbox", { name: "工作目录", exact: true })).toHaveValue("/public/home/alice/project-a");
  await expect(page.getByRole("region", { name: "实验资产" })).toContainText("/public/home/alice/project-a");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
'''
if text.count(old_workdir) != 1:
    raise RuntimeError("visual.spec.js: workdir picker assertion anchor mismatch")
text = text.replace(old_workdir, new_workdir, 1)
old_shared = '''  await page.getByRole("button", { name: "选择此文件" }).click();
  await expect(page.getByRole("textbox", { name: /^runtime\\.environment\\.DATA_ROOT/ })).toHaveValue("/public/home/alice/dataset.tar.gz");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
'''
new_shared = '''  await page.getByRole("button", { name: "选择此文件" }).click();
  await expect(page.getByRole("textbox", { name: /^runtime\\.environment\\.DATA_ROOT/ })).toHaveValue("/public/home/alice/dataset.tar.gz");
  await expect(page.getByRole("region", { name: "实验资产" })).toContainText("/public/home/alice/dataset.tar.gz");
  await expect(page).toHaveURL(/\\/studio\\/new\\?user=alice/);
'''
if text.count(old_shared) != 1:
    raise RuntimeError("visual.spec.js: shared_path assertion anchor mismatch")
text = text.replace(old_shared, new_shared, 1)
visual.write_text(text)

print("A6 contract asset references staged")
