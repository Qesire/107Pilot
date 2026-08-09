import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  CopyPlus,
  Eye,
  FileDiff,
  Filter,
  Search,
  Users,
} from "lucide-react";
import { api } from "./api";
import { QueryBoundary, SectionHeading, StatusBadge, formatTimestamp } from "./components";
import { detailVersions } from "./market-state";
import { useMarketItem, useMarketItems, useTemplateDiff, useTemplateRelease, useTemplates } from "./query";
import type { MarketItem } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface MarketPageProps {
  user: string;
  location: LocationState;
  navigate: (path: string, options?: { replace?: boolean }) => void;
}

export function MarketPage({ user, location, navigate }: MarketPageProps) {
  const q = location.search.get("q") ?? "";
  const visibility = location.search.get("visibility") ?? "";
  const kind = location.search.get("kind") ?? "";
  const tag = location.search.get("tag") ?? "";
  const market = useMarketItems(user, {
    q: q || undefined,
    kind: kind || undefined,
    visibility: visibility || undefined,
    tag: tag || undefined,
  });
  const update = (values: Record<string, string | null>) =>
    navigate(withSearch("/market", location.search, values));

  return (
    <>
      <SectionHeading
        eyebrow="Market / templates and successful Runs"
        title="作业与模板市场"
        detail="统一市场按发布时间稳定分页。成功 Run 只证明发布者曾运行成功；curated release 才带审核与验证事实。"
        action={<div className="agent-action-row"><button className="button secondary" type="button" onClick={() => navigate(`/templates?user=${encodeURIComponent(user)}`)}>我的模板</button><button className="button primary" type="button" onClick={() => navigate(`/templates/new?user=${encodeURIComponent(user)}`)}>发布模板</button></div>}
      />
      <section className="market-filter" aria-label="市场筛选">
        <label className="search-field"><Search aria-hidden="true" size={17} /><span className="sr-only">搜索市场</span><input value={q} placeholder="搜索标题、描述或 Template ID" onChange={(event) => update({ q: event.target.value || null })} /></label>
        <label className="select-field"><Eye aria-hidden="true" size={16} /><span className="sr-only">可见性</span><select value={visibility} onChange={(event) => update({ visibility: event.target.value || null })}><option value="">全部可见性</option><option value="public">Public</option><option value="campus">Campus</option><option value="course">Course</option><option value="private">Private</option></select></label>
        <label className="select-field"><Filter aria-hidden="true" size={16} /><span className="sr-only">条目类型</span><select value={kind} onChange={(event) => update({ kind: event.target.value || null, tag: event.target.value === "curated_template" ? null : tag || null })}><option value="">全部类型</option><option value="run_publication">成功 Run</option><option value="curated_template">Curated template</option></select></label>
        <label className="search-field"><span className="sr-only">成功 Run 标签</span><input value={tag} disabled={kind === "curated_template"} placeholder="成功 Run 标签" onChange={(event) => update({ tag: event.target.value || null })} /></label>
      </section>
      <QueryBoundary
        pending={market.isPending}
        error={market.error}
        empty={(market.data?.items.length ?? 0) === 0}
        emptyTitle="没有匹配的市场条目"
        emptyDetail="调整类型、可见性或搜索条件；撤回条目和无权访问的 scope 不会显示。"
      >
        <section className="market-grid" aria-label="统一作业与模板市场">
          {(market.data?.items ?? []).map((item) => (
            <MarketItemCard
              key={item.item_id}
              item={item}
              onOpen={() => navigate(`/market/${encodeURIComponent(item.item_id)}?user=${encodeURIComponent(user)}`)}
            />
          ))}
        </section>
      </QueryBoundary>
    </>
  );
}

function MarketItemCard({ item, onOpen }: { item: MarketItem; onOpen: () => void }) {
  const curated = item.kind === "curated_template";
  return (
    <article className="template-card">
      <header><div><p className="panel-kicker">{curated ? `Curated · ${item.template.template_id}` : `Successful Run · ${item.source.run_id}`}</p><h2>{item.title}</h2></div><StatusBadge label={item.visibility} tone={item.visibility === "public" ? "success" : "neutral"} /></header>
      <p className="template-description">{item.description || "发布者未填写说明。"}</p>
      <div className="template-meta"><span>{curated ? `v${item.template.release_version}` : "成功记录"}</span><span>{formatTimestamp(item.published_at)}</span><span>{item.publisher}</span></div>
      {item.tags.length ? <p className="request-key">标签：{item.tags.join(" · ")}</p> : null}
      {curated ? <dl className="template-metrics"><div><dt>采用</dt><dd><Users aria-hidden="true" />{item.metrics.adoption_count}</dd></div><div><dt>通过验证</dt><dd><CheckCircle2 aria-hidden="true" />{item.metrics.verification_passed}</dd></div><div><dt>成功率</dt><dd>{item.metrics.success_rate === null ? "—" : `${Math.round(item.metrics.success_rate * 100)}%`}</dd></div></dl> : <p className="side-detail">{item.reproduction_note || "代码与数据由发布者自行说明；采用后请检查路径、依赖与环境。"}</p>}
      <button className="button secondary wide" type="button" onClick={onOpen}>查看条目 <ArrowRight aria-hidden="true" size={15} /></button>
    </article>
  );
}

export function MarketItemDetailPage({ user, location, navigate }: MarketPageProps) {
  const itemId = decodeURIComponent(location.pathname.slice("/market/".length));
  const item = useMarketItem(user, itemId);
  const queryClient = useQueryClient();
  const [requestKey, setRequestKey] = useState<string | null>(null);
  const [withdrawReason, setWithdrawReason] = useState("");
  const adoption = useMutation({
    mutationFn: (key: string) => api.adoptMarketItem(user, itemId, key),
    onSuccess: (result) => {
      if (result.target_contract_id) {
        navigate(`/studio/${encodeURIComponent(result.target_contract_id)}?user=${encodeURIComponent(user)}&tab=basic&adoption=${encodeURIComponent(result.adoption_id)}`);
      }
    },
  });
  const withdraw = useMutation({
    mutationFn: (reason: string) => api.withdrawMarketItem(user, itemId, reason),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["market-items"] });
      await queryClient.invalidateQueries({ queryKey: ["market-item", user, itemId] });
      navigate(`/market?user=${encodeURIComponent(user)}`);
    },
  });
  const adopt = () => {
    const key = requestKey ?? `web-adopt-market-item-${crypto.randomUUID()}`;
    setRequestKey(key);
    adoption.mutate(key);
  };
  const record = item.data;
  return (
    <>
      <SectionHeading eyebrow="Market item / public read model" title={record?.title ?? itemId} detail={record?.kind === "curated_template" ? "Curated release 带审核与验证事实；采用后仍创建你的私有 Contract。" : "此条目只证明发布者曾成功运行；代码、数据、依赖与可移植性不由市场保证。"} />
      <QueryBoundary pending={item.isPending} error={item.error}>
        {record ? <div className="template-detail-grid">
          <section className="panel template-release-main">
            <div className="release-heading"><div><StatusBadge label={record.kind === "curated_template" ? "curated template" : "successful Run"} tone={record.kind === "curated_template" ? "success" : "neutral"} /><StatusBadge label={record.visibility} tone="info" /></div><span>{formatTimestamp(record.published_at)}</span></div>
            <p className="template-description large">{record.description || "发布者未填写说明。"}</p>
            <dl className="fact-list"><div><dt>Publisher</dt><dd>{record.publisher}</dd></div><div><dt>Scope</dt><dd>{record.scope_key ?? "—"}</dd></div><div><dt>Item ID</dt><dd className="mono wrap-anywhere">{record.item_id}</dd></div><div><dt>Adoption</dt><dd>{record.adoption.available ? "可采用" : record.adoption.reason ?? "不可采用"}</dd></div></dl>
            {record.kind === "run_publication" ? <>
              <p className="side-detail">{record.reproduction_note || "采用后请替换自己的工作目录、代码、数据和依赖。"}</p>
              <p className="request-key mono">source Run: {record.source.run_id}</p>
            </> : <>
              <dl className="template-metrics"><div><dt>采用</dt><dd>{record.metrics.adoption_count}</dd></div><div><dt>验证通过</dt><dd>{record.metrics.verification_passed}</dd></div><div><dt>验证失败</dt><dd>{record.metrics.verification_failed}</dd></div></dl>
              <div className="release-json-grid"><JsonPanel title="Compatibility" value={record.compatibility} /><JsonPanel title="Publication" value={record.publication} /></div>
              <JsonPanel title="Canonical Contract payload" value={record.contract_payload} tall />
            </>}
          </section>
          <aside className="template-detail-side">
            <section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Adopt</p><h2>采用为私有 Contract</h2></div><CopyPlus aria-hidden="true" size={19} /></div><p className="side-detail">采用只创建你的私有副本；提交前必须在 Studio 重新检查路径、资源和依赖。</p>{adoption.isError ? <p className="limitation" role="alert">{adoption.error.message}</p> : null}<button className="button primary wide" type="button" disabled={!record.adoption.available || adoption.isPending} onClick={adopt}>{adoption.isPending ? "采用中" : "采用并进入 Studio"}</button>{requestKey ? <p className="request-key mono">request key: {requestKey}</p> : null}</section>
            {record.kind === "run_publication" && record.publisher === user ? <section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Publisher control</p><h2>撤回条目</h2></div></div><label className="form-field"><span>撤回原因</span><textarea value={withdrawReason} onChange={(event) => setWithdrawReason(event.target.value)} /></label>{withdraw.isError ? <p className="limitation" role="alert">{withdraw.error.message}</p> : null}<button className="button danger wide" type="button" disabled={!withdrawReason.trim() || withdraw.isPending} onClick={() => withdraw.mutate(withdrawReason.trim())}>{withdraw.isPending ? "撤回中" : "确认撤回"}</button></section> : null}
          </aside>
        </div> : null}
      </QueryBoundary>
    </>
  );
}

export function TemplateDetailPage({ user, location, navigate }: MarketPageProps) {
  const templateId = decodeURIComponent(location.pathname.slice("/templates/".length));
  const requestedVersion = location.search.get("version");
  const releases = useTemplates(user, { q: templateId, limit: "100" });
  const exactReleases = useMemo(
    () => (releases.data?.items ?? []).filter((item) => item.template_id === templateId),
    [releases.data?.items, templateId],
  );
  const versions = detailVersions(exactReleases, templateId, requestedVersion);
  const version = requestedVersion ?? versions[0] ?? null;
  const release = useTemplateRelease(user, templateId, version);
  const from = location.search.get("from") ?? (versions.find((item) => item !== version) ?? null);
  const diff = useTemplateDiff(user, templateId, from, version);
  const [requestKey, setRequestKey] = useState<string | null>(null);
  const [withdrawReason, setWithdrawReason] = useState("");
  const queryClient = useQueryClient();
  const withdrawRelease = useMutation({
    mutationFn: (reason: string) =>
      api.withdrawTemplateRelease(user, templateId, version ?? "", reason),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["template-release", user, templateId] });
      await queryClient.invalidateQueries({ queryKey: ["templates", user] });
      await queryClient.invalidateQueries({ queryKey: ["market-items"] });
    },
  });
  const adoption = useMutation({
    mutationFn: (key: string) => api.adoptTemplate(user, templateId, version ?? "", key),
    onSuccess: (result) => {
      if (!result.target_contract_id) return;
      navigate(`/studio/${encodeURIComponent(result.target_contract_id)}?user=${encodeURIComponent(user)}&tab=basic&adoption=${encodeURIComponent(result.adoption_id)}`);
    },
  });

  useEffect(() => {
    if (!requestedVersion && version) {
      navigate(withSearch(location.pathname, location.search, { version }), { replace: true });
    }
  }, [location.pathname, location.search, navigate, requestedVersion, version]);

  const adopt = () => {
    const key = requestKey ?? `web-adopt-${crypto.randomUUID()}`;
    setRequestKey(key);
    adoption.mutate(key);
  };

  return (
    <>
      <SectionHeading eyebrow="Template release / immutable" title={release.data?.title ?? templateId} detail="Release 内容不可变；采用会创建你的 private draft、canonical Contract 和 lineage。" />
      <QueryBoundary pending={release.isPending || releases.isPending} error={release.error ?? releases.error}>
        {release.data ? (
          <div className="template-detail-grid">
            <section className="panel template-release-main">
              <div className="release-heading"><div><StatusBadge label={release.data.visibility} tone="info" /><span>v{release.data.release_version}</span></div><span className="mono">sha256 {release.data.content_sha256}</span></div>
              <p className="template-description large">{release.data.description}</p>
              <dl className="fact-list"><div><dt>Publisher</dt><dd>{release.data.publisher}</dd></div><div><dt>Scope</dt><dd>{release.data.scope_key ?? "—"}</dd></div><div><dt>Published</dt><dd>{formatTimestamp(release.data.published_at)}</dd></div><div><dt>Withdrawn</dt><dd>{release.data.withdrawn_at ? formatTimestamp(release.data.withdrawn_at) : "否"}</dd></div></dl>
              <div className="release-json-grid"><JsonPanel title="Compatibility" value={release.data.compatibility} /><JsonPanel title="Publication" value={release.data.publication} /></div>
              <JsonPanel title="Canonical Contract payload" value={release.data.payload} tall />
            </section>
            <aside className="template-detail-side">
              <section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Adopt</p><h2>采用此 release</h2></div><CopyPlus aria-hidden="true" size={19} /></div><p className="side-detail">不会修改 release；服务器原子创建 private draft、Contract 和 lineage。</p>{adoption.isError ? <p className="limitation" role="alert">{adoption.error.message}</p> : null}<button className="button primary wide" type="button" disabled={Boolean(release.data.withdrawn_at) || adoption.isPending} onClick={adopt}>{adoption.isPending ? "采用中" : "采用并进入 Studio"}</button>{requestKey ? <p className="request-key mono">request key: {requestKey}</p> : null}</section>
              {release.data.publisher === user && !release.data.withdrawn_at ? (
                <section className="panel">
                  <div className="panel-heading"><div><p className="panel-kicker">Publisher control</p><h2>撤回 release</h2></div></div>
                  <p className="side-detail">撤回后条目不再出现在市场，也不能被采用；已创建的采用副本不受影响。</p>
                  <label className="form-field"><span>撤回原因</span><textarea value={withdrawReason} onChange={(event) => setWithdrawReason(event.target.value)} /></label>
                  {withdrawRelease.isError ? <p className="limitation" role="alert">{withdrawRelease.error.message}</p> : null}
                  <button className="button danger wide" type="button" disabled={!withdrawReason.trim() || withdrawRelease.isPending} onClick={() => withdrawRelease.mutate(withdrawReason.trim())}>{withdrawRelease.isPending ? "撤回中" : "确认撤回"}</button>
                </section>
              ) : null}
              <section className="panel"><div className="panel-heading"><div><p className="panel-kicker">Versions</p><h2>Release diff</h2></div><FileDiff aria-hidden="true" size={19} /></div><label className="form-field"><span>当前版本</span><select value={version ?? ""} onChange={(event) => navigate(withSearch(location.pathname, location.search, { version: event.target.value, from: null }))}>{versions.map((item) => <option key={item} value={item}>{item}</option>)}</select></label><label className="form-field"><span>对比版本</span><select value={from ?? ""} onChange={(event) => navigate(withSearch(location.pathname, location.search, { from: event.target.value || null }))}><option value="">不对比</option>{versions.filter((item) => item !== version).map((item) => <option key={item} value={item}>{item}</option>)}</select></label><QueryBoundary pending={diff.isPending && Boolean(from)} error={diff.error}>{diff.data ? <ul className="diff-list">{diff.data.changes.map((change, index) => <li key={`${change.path}-${index}`}><strong>{change.path}</strong><span className="removed">− {compactJson(change.before)}</span><span className="added">+ {compactJson(change.after)}</span></li>)}</ul> : <p className="no-findings">选择另一个版本查看 immutable content diff。</p>}</QueryBoundary></section>
            </aside>
          </div>
        ) : null}
      </QueryBoundary>
    </>
  );
}

function JsonPanel({ title, value, tall }: { title: string; value: unknown; tall?: boolean | undefined }) {
  return <section className={`json-panel ${tall ? "tall" : ""}`}><h3>{title}</h3><pre><code>{JSON.stringify(value, null, 2)}</code></pre></section>;
}

function compactJson(value: unknown): string {
  const encoded = JSON.stringify(value);
  if (encoded === undefined) return "undefined";
  return encoded.length > 180 ? `${encoded.slice(0, 177)}…` : encoded;
}
