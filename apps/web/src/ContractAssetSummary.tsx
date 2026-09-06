import { File, Folder, Link2 } from "lucide-react";
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
