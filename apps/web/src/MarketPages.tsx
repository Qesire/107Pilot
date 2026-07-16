import { useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  CopyPlus,
  Eye,
  FileDiff,
  Filter,
  Search,
  ShieldCheck,
  Users,
} from "lucide-react";
import { api } from "./api";
import { QueryBoundary, SectionHeading, StatusBadge, formatTimestamp } from "./components";
import { detailVersions } from "./market-state";
import { useTemplateDiff, useTemplateRelease, useTemplates } from "./query";
import type { TemplateMarketItem } from "./types";
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
  const gpu = location.search.get("gpu") ?? "";
  const verified = location.search.get("verified") ?? "";
  const environment = location.search.get("environment") ?? "";
  const partition = location.search.get("partition") ?? "";
  const templates = useTemplates(user, {
    q: q || undefined,
    visibility: visibility || undefined,
    gpu: gpu || undefined,
    verified: verified || undefined,
    verification_environment: environment || undefined,
    partition: partition || undefined,
  });
  const update = (values: Record<string, string | null>) =>
    navigate(withSearch("/market", location.search, values));

  return (
    <>
      <SectionHeading
        eyebrow="Template Market / live releases"
        title="从审核过的 release 开始"
        detail="查询、可见性和验证等级均来自服务器；采用后生成你的 private draft 与 immutable canonical Contract。"
      />
      <section className="market-filter" aria-label="模板筛选">
        <label className="search-field"><Search aria-hidden="true" size={17} /><span className="sr-only">搜索模板</span><input value={q} placeholder="搜索标题、描述或 Template ID" onChange={(event) => update({ q: event.target.value || null })} /></label>
        <label className="select-field"><Eye aria-hidden="true" size={16} /><span className="sr-only">可见性</span><select value={visibility} onChange={(event) => update({ visibility: event.target.value || null })}><option value="">全部可见性</option><option value="public">Public</option><option value="campus">Campus</option><option value="course">Course</option><option value="private">Private</option></select></label>
        <label className="select-field"><Filter aria-hidden="true" size={16} /><span className="sr-only">GPU</span><select value={gpu} onChange={(event) => update({ gpu: event.target.value || null })}><option value="">CPU / GPU</option><option value="true">需要 GPU</option><option value="false">CPU</option></select></label>
        <label className="select-field"><ShieldCheck aria-hidden="true" size={16} /><span className="sr-only">验证</span><select value={verified} onChange={(event) => update({ verified: event.target.value || null })}><option value="">全部验证状态</option><option value="true">已有通过验证</option></select></label>
        <label className="select-field"><span className="sr-only">验证环境</span><select value={environment} onChange={(event) => update({ environment: event.target.value || null })}><option value="">全部环境</option><option value="docker">Docker</option><option value="real107_cpu">real107 CPU</option><option value="real107_gpu">real107 GPU</option></select></label>
      </section>
      <QueryBoundary
        pending={templates.isPending}
        error={templates.error}
        empty={(templates.data?.items.length ?? 0) === 0}
        emptyTitle="没有匹配的 release"
        emptyDetail="调整筛选；withdrawn release 和无权访问的 scope 不会出现在结果中。"
      >
        <section className="market-grid" aria-label="模板 release">
          {(templates.data?.items ?? []).map((item) => (
            <TemplateCard key={item.release_id} item={item} onOpen={() => navigate(`/templates/${encodeURIComponent(item.template_id)}?user=${encodeURIComponent(user)}&version=${encodeURIComponent(item.release_version)}`)} />
          ))}
        </section>
      </QueryBoundary>
    </>
  );
}

function TemplateCard({ item, onOpen }: { item: TemplateMarketItem; onOpen: () => void }) {
  return (
    <article className="template-card">
      <header><div><p className="panel-kicker">{item.template_id}</p><h2>{item.title}</h2></div><StatusBadge label={item.visibility} tone={item.visibility === "public" ? "success" : "neutral"} /></header>
      <p className="template-description">{item.description}</p>
      <div className="template-meta"><span>v{item.release_version}</span><span>{formatTimestamp(item.published_at)}</span><span className="mono">{item.content_sha256.slice(0, 10)}</span></div>
      <dl className="template-metrics"><div><dt>采用</dt><dd><Users aria-hidden="true" />{item.metrics.adoption_count}</dd></div><div><dt>通过验证</dt><dd><CheckCircle2 aria-hidden="true" />{item.metrics.verification_passed}</dd></div><div><dt>成功率</dt><dd>{item.metrics.success_rate === null ? "—" : `${Math.round(item.metrics.success_rate * 100)}%`}</dd></div></dl>
      <button className="button secondary wide" type="button" onClick={onOpen}>查看 release <ArrowRight aria-hidden="true" size={15} /></button>
    </article>
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
