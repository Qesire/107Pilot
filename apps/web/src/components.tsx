import type { ReactNode } from "react";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDashed,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { ApiRequestError } from "./api";

type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export function StatusBadge({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  return <span className={`status-badge tone-${tone}`}>{label}</span>;
}

export function SectionHeading({
  eyebrow,
  title,
  detail,
  action,
}: {
  eyebrow: string;
  title: string;
  detail: string;
  action?: ReactNode;
}) {
  return (
    <header className="section-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{detail}</p>
      </div>
      {action ? <div className="heading-action">{action}</div> : null}
    </header>
  );
}

export function RefreshButton({ onClick, pending }: { onClick: () => void; pending: boolean }) {
  return (
    <button className="button secondary" type="button" onClick={onClick} disabled={pending}>
      <RefreshCw aria-hidden="true" size={16} className={pending ? "spin" : undefined} />
      刷新
    </button>
  );
}

export function QueryBoundary({
  pending,
  error,
  empty,
  emptyTitle = "暂无数据",
  emptyDetail = "当前范围内没有可显示的记录。",
  children,
}: {
  pending: boolean;
  error: unknown;
  empty?: boolean;
  emptyTitle?: string;
  emptyDetail?: ReactNode;
  children: ReactNode;
}) {
  if (pending) {
    return (
      <div className="query-state" role="status">
        <LoaderCircle aria-hidden="true" className="spin" />
        <div>
          <strong>正在读取实时数据</strong>
          <p>从 107Pilot API 获取最新状态。</p>
        </div>
      </div>
    );
  }
  if (error) {
    const forbidden = error instanceof ApiRequestError && error.status === 403;
    return (
      <div className="query-state error" role="alert">
        {forbidden ? <Ban aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
        <div>
          <strong>{forbidden ? "无权查看此范围" : "数据读取失败"}</strong>
          <p>{error instanceof Error ? error.message : "未知错误"}</p>
        </div>
      </div>
    );
  }
  if (empty) {
    return (
      <div className="query-state empty">
        <CircleDashed aria-hidden="true" />
        <div>
          <strong>{emptyTitle}</strong>
          <p>{emptyDetail}</p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

export function FactState({
  status,
  label,
}: {
  status: string | undefined;
  label?: string;
}) {
  const normalized = status?.toLowerCase() ?? "unknown";
  const tone: Tone =
    normalized === "fresh" || normalized === "ok" || normalized === "ready"
      ? "success"
      : normalized === "stale" || normalized === "degraded" || normalized === "partial"
        ? "warning"
        : "neutral";
  return (
    <span className={`fact-state tone-${tone}`}>
      {tone === "success" ? <CheckCircle2 aria-hidden="true" size={14} /> : null}
      {label ?? status ?? "unknown"}
    </span>
  );
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}
