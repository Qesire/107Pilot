import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Ban, Bot, MessageSquarePlus, Plus, Send, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, ApiRequestError } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { useAgentSession, useAgentSessionEvents, useAgentSessions } from "./query";
import type { AgentSessionState, AgentTurn, AgentTurnEvent } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface AgentSessionPanelProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

const defaultModelProfile = "campus-default";

export function AgentSessionPanel({ user, location, navigate }: AgentSessionPanelProps) {
  const queryClient = useQueryClient();
  const sessions = useAgentSessions(user);
  const requestedSession = location.search.get("session");
  const selectedId = requestedSession ?? sessions.data?.items[0]?.session_id ?? null;
  const [createRequestKey, setCreateRequestKey] = useState<string | null>(null);
  const createSession = useMutation({
    mutationFn: (requestKey: string) => api.createAgentSession(user, {
      profile: defaultModelProfile,
      request_key: requestKey,
    }),
    onSuccess: (session) => {
      setCreateRequestKey(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-sessions", user] });
      navigate(withSearch("/agent", location.search, { session: session.session_id }));
    },
  });
  const startSession = () => {
    const requestKey = createRequestKey ?? `ui:agent-session:${crypto.randomUUID()}`;
    setCreateRequestKey(requestKey);
    createSession.mutate(requestKey);
  };

  return (
    <div className="agent-layout agent-conversation-layout">
      <section className="panel agent-queue" aria-labelledby="conversation-queue-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Durable conversations</p>
            <h2 id="conversation-queue-heading">{sessions.data?.items.length ?? 0} 个对话</h2>
          </div>
          <div className="agent-queue-actions">
            <button
              type="button"
              className="button secondary"
              disabled={createSession.isPending}
              onClick={startSession}
            >
              <Plus aria-hidden="true" size={15} />
              {createSession.isPending ? "正在创建" : "新建对话"}
            </button>
            {sessions.isFetching ? <StatusBadge label="同步中" tone="info" /> : null}
          </div>
        </div>
        {createSession.error ? <AgentMutationError error={createSession.error} /> : null}
        <div className="agent-readonly-note">
          <ShieldCheck aria-hidden="true" size={17} />
          <p><strong>只读边界</strong>可读取平台事实；仅在会话明确绑定后读取 Run、日志与 Evidence。不能提交或取消 Slurm 作业，也不能改写文件。</p>
        </div>
        <QueryBoundary
          pending={sessions.isPending}
          error={sessions.error}
          empty={(sessions.data?.items.length ?? 0) === 0}
          emptyTitle="还没有持久化对话"
          emptyDetail="创建后即可询问平台事实，以及会话明确绑定的 Run、日志和 Evidence。"
        >
          <div className="agent-session-list">
            {(sessions.data?.items ?? []).map((session) => (
              <button
                key={session.session_id}
                type="button"
                className={session.session_id === selectedId ? "active" : undefined}
                onClick={() => navigate(withSearch("/agent", location.search, {
                  session: session.session_id,
                }))}
              >
                <span>
                  <StatusBadge
                    label={agentSessionStateLabel(session.state)}
                    tone={agentSessionStateTone(session.state)}
                  />
                  <small>{formatTimestamp(session.updated_at)}</small>
                </span>
                <strong className="mono wrap-anywhere">{session.session_id}</strong>
                <small>{session.model_profile_id} · v{session.state_version}</small>
              </button>
            ))}
          </div>
        </QueryBoundary>
      </section>

      <section className="panel agent-detail" aria-labelledby="conversation-detail-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Persisted event stream</p>
            <h2 id="conversation-detail-heading">对话与事件</h2>
          </div>
        </div>
        <QueryBoundary
          pending={false}
          error={null}
          empty={!selectedId}
          emptyTitle="选择或创建一个对话"
          emptyDetail="每个 Turn 的事件先持久化，再按全局事件 ID 增量重放。"
        >
          {selectedId ? (
            <AgentConversation key={selectedId} user={user} sessionId={selectedId} />
          ) : null}
        </QueryBoundary>
      </section>
    </div>
  );
}

function AgentConversation({ user, sessionId }: { user: string; sessionId: string }) {
  const queryClient = useQueryClient();
  const session = useAgentSession(user, sessionId);
  const [events, setEvents] = useState<AgentTurnEvent[]>([]);
  const lastEventId = events.at(-1)?.event_id ?? 0;
  const eventPage = useAgentSessionEvents(user, sessionId, lastEventId);
  const [message, setMessage] = useState("");
  const [turnRequestKey, setTurnRequestKey] = useState<string | null>(null);
  const [activeTurn, setActiveTurn] = useState<AgentTurn | null>(null);

  useEffect(() => {
    if (eventPage.data?.items.length) {
      setEvents((current) => mergeAgentEvents(current, eventPage.data?.items ?? []));
    }
  }, [eventPage.data]);

  const terminalTurnIds = useMemo(() => new Set(
    events
      .filter((item) => item.event_type === "turn_completed" || item.event_type === "turn_failed")
      .map((item) => item.turn_id),
  ), [events]);
  const turnIsActive = activeTurn !== null && !terminalTurnIds.has(activeTurn.turn_id);
  const createTurn = useMutation({
    mutationFn: ({ text, requestKey }: { text: string; requestKey: string }) => {
      if (!session.data) throw new Error("会话状态尚未加载");
      return api.createAgentTurn(user, sessionId, {
        message: text,
        request_key: requestKey,
        expected_state_version: session.data.state_version,
      });
    },
    onSuccess: (turn) => {
      setActiveTurn(turn);
      setMessage("");
      setTurnRequestKey(null);
      void queryClient.invalidateQueries({ queryKey: ["agent-session", user, sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["agent-sessions", user] });
    },
  });
  const cancelTurn = useMutation({
    mutationFn: () => {
      if (!activeTurn) throw new Error("没有可取消的 Turn");
      return api.cancelAgentTurn(
        user,
        sessionId,
        activeTurn.turn_id,
        activeTurn.state_version,
      );
    },
    onSuccess: (turn) => {
      setActiveTurn(turn);
      void queryClient.invalidateQueries({ queryKey: ["agent-session", user, sessionId] });
    },
  });
  const submitMessage = () => {
    const text = message.trim();
    if (!text || !session.data || session.data.state !== "idle") return;
    const requestKey = turnRequestKey ?? `ui:agent-turn:${crypto.randomUUID()}`;
    setTurnRequestKey(requestKey);
    createTurn.mutate({ text, requestKey });
  };
  const mutationError = createTurn.error ?? cancelTurn.error;
  const eventGroups = useMemo(() => groupAgentEvents(events), [events]);

  return (
    <QueryBoundary
      pending={session.isPending}
      error={session.error}
      empty={false}
    >
      {session.data ? (
        <div className="agent-conversation">
          <header className="agent-conversation-heading">
            <div>
              <StatusBadge
                label={agentSessionStateLabel(session.data.state)}
                tone={agentSessionStateTone(session.data.state)}
              />
              <p className="mono wrap-anywhere">{session.data.session_id}</p>
            </div>
            <small className="mono">last event #{lastEventId}</small>
          </header>

          <div className="agent-event-stream" aria-live="polite" aria-label="持久化 Agent 事件">
            {eventGroups.length ? eventGroups.map((group) => (
              <AgentEventGroupRow key={group.key} group={group} />
            )) : (
              <div className="agent-event-empty">
                <MessageSquarePlus aria-hidden="true" size={22} />
                <strong>从一个具体问题开始</strong>
                <p>例如：解释某个 Run 为何长期排队，并引用相关平台事实。</p>
              </div>
            )}
          </div>

          {mutationError ? <AgentMutationError error={mutationError} /> : null}
          <div className="agent-composer">
            <label htmlFor="agent-message">发送给只读 Agent</label>
            <textarea
              id="agent-message"
              value={message}
              rows={3}
              maxLength={64_000}
              placeholder="询问平台，或会话已绑定的 Run、日志与 Evidence…"
              disabled={session.data.state !== "idle" || createTurn.isPending}
              onChange={(event) => {
                setMessage(event.target.value);
                if (turnRequestKey) setTurnRequestKey(null);
                createTurn.reset();
              }}
            />
            <div>
              <small>{session.data.state === "idle" ? "上下文和工具调用会进入持久化事件流。" : "当前 Turn 完成后可继续提问。"}</small>
              {turnIsActive ? (
                <button
                  className="button danger"
                  type="button"
                  disabled={cancelTurn.isPending}
                  onClick={() => cancelTurn.mutate()}
                >
                  <Ban aria-hidden="true" size={15} />
                  {cancelTurn.isPending ? "正在取消" : "取消 Turn"}
                </button>
              ) : (
                <button
                  className="button primary"
                  type="button"
                  disabled={!message.trim() || session.data.state !== "idle" || createTurn.isPending}
                  onClick={submitMessage}
                >
                  <Send aria-hidden="true" size={15} />
                  {createTurn.isPending ? "正在提交" : "发送"}
                </button>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </QueryBoundary>
  );
}

function AgentEventGroupRow({ group }: { group: AgentEventGroup }) {
  const first = group.events[0];
  const last = group.events.at(-1);
  if (!first || !last) return null;
  const toolName = group.events
    .map((event) => event.payload.tool_name)
    .find((value): value is string => typeof value === "string");
  const label = group.kind === "assistant"
    ? "Agent 回复"
    : group.kind === "tool"
      ? toolName ?? "工具调用"
      : first.event_type === "turn_started"
        ? agentTaskKindLabel(first.payload.task_kind)
        : agentEventLabel(first.event_type);
  const meta = group.kind === "tool"
    ? `${group.events.length} 条事件`
    : formatTimestamp(last.created_at);
  return (
    <article className={`agent-event agent-event-${group.kind}`}>
      <div
        className="agent-event-sequence"
        aria-label={`事件 ${first.event_id} 至 ${last.event_id}`}
      >
        <strong>#{first.event_id}{first.event_id === last.event_id ? "" : `–${last.event_id}`}</strong>
        <span>{first.sequence === last.sequence ? first.sequence : `${first.sequence}–${last.sequence}`}</span>
      </div>
      <div>
        <header>
          <strong>{label}</strong>
          <small>{meta}</small>
        </header>
        {group.text ? <p>{group.text}</p> : null}
        <details>
          <summary>查看{group.events.length > 1 ? `${group.events.length} 条` : ""}原始事件</summary>
          <pre><code>{JSON.stringify(group.events, null, 2)}</code></pre>
        </details>
      </div>
    </article>
  );
}

function AgentMutationError({ error }: { error: Error }) {
  const apiError = error instanceof ApiRequestError ? error : null;
  return (
    <div className="agent-mutation-error" role="alert">
      <strong>无法完成此操作</strong>
      <p>{error.message}</p>
      {apiError ? <small className="mono">{apiError.code}</small> : null}
    </div>
  );
}

export function mergeAgentEvents(
  current: AgentTurnEvent[],
  incoming: AgentTurnEvent[],
): AgentTurnEvent[] {
  const byId = new Map(current.map((item) => [item.event_id, item]));
  incoming.forEach((item) => {
    if (!byId.has(item.event_id)) byId.set(item.event_id, item);
  });
  return [...byId.values()].sort((left, right) => left.event_id - right.event_id);
}

export interface AgentEventGroup {
  readonly key: string;
  readonly kind: "assistant" | "tool" | "lifecycle" | "failure";
  readonly events: readonly AgentTurnEvent[];
  readonly text: string | null;
}

export function groupAgentEvents(events: AgentTurnEvent[]): AgentEventGroup[] {
  const groups: AgentEventGroup[] = [];
  const toolGroups = new Map<string, number>();

  for (const event of events) {
    if (event.event_type === "message_delta") {
      const delta = typeof event.payload.delta === "string" ? event.payload.delta : "";
      const lastIndex = groups.length - 1;
      const previous = groups[lastIndex];
      if (
        previous?.kind === "assistant" &&
        previous.events.at(-1)?.turn_id === event.turn_id
      ) {
        groups[lastIndex] = {
          ...previous,
          events: [...previous.events, event],
          text: `${previous.text ?? ""}${delta}`,
        };
      } else {
        groups.push({
          key: `assistant:${event.turn_id}:${event.event_id}`,
          kind: "assistant",
          events: [event],
          text: delta,
        });
      }
      continue;
    }

    if (event.event_type.startsWith("tool_call_")) {
      const toolCallId = typeof event.payload.tool_call_id === "string"
        ? event.payload.tool_call_id
        : `event-${event.event_id}`;
      const key = `tool:${event.turn_id}:${toolCallId}`;
      const existingIndex = toolGroups.get(key);
      if (existingIndex === undefined) {
        toolGroups.set(key, groups.length);
        groups.push({
          key,
          kind: "tool",
          events: [event],
          text: toolGroupText([event]),
        });
      } else {
        const previous = groups[existingIndex];
        if (!previous) continue;
        const groupedEvents = [...previous.events, event];
        groups[existingIndex] = {
          ...previous,
          events: groupedEvents,
          text: toolGroupText(groupedEvents),
        };
      }
      continue;
    }

    groups.push({
      key: `event:${event.event_id}`,
      kind: event.event_type === "turn_failed" ? "failure" : "lifecycle",
      events: [event],
      text: agentEventText(event),
    });
  }

  return groups.filter(
    (group) => group.kind !== "assistant" || Boolean(group.text?.trim()),
  );
}

export function agentEventText(event: AgentTurnEvent): string | null {
  const candidates = [
    event.payload.delta,
    event.payload.content,
    event.payload.progress,
    event.payload.result,
  ];
  const text = candidates.find((value) => typeof value === "string");
  if (typeof text === "string" && text.trim()) return text;
  const error = event.payload.error;
  const directError = errorMessage(error);
  if (directError) return directError;
  const resultError = event.payload.result;
  if (resultError && typeof resultError === "object" && "error" in resultError) {
    const nestedError = errorMessage(resultError.error);
    if (nestedError) return nestedError;
  }
  return null;
}

function toolGroupText(events: readonly AgentTurnEvent[]): string | null {
  for (const event of [...events].reverse()) {
    const text = agentEventText(event);
    if (text) return text;
  }
  return null;
}

function errorMessage(value: unknown): string | null {
  if (!value || typeof value !== "object" || !("message" in value)) return null;
  return typeof value.message === "string" && value.message.trim()
    ? value.message
    : null;
}

export function agentTaskKindLabel(taskKind: unknown): string {
  if (typeof taskKind !== "string") return "Turn 开始";
  return {
    interactive: "交互 Turn",
    interactive_readonly: "平台只读 Turn",
    experiment_builder: "实验构建 Turn",
    run_diagnosis_repair: "诊断修复 Turn",
    market_application: "市场应用 Turn",
    template_publication: "模板发布 Turn",
    explain: "解释 Turn",
    contract_patch: "Contract 建议 Turn",
    remediation_plan: "修复规划 Turn",
  }[taskKind] ?? taskKind;
}

function agentEventLabel(eventType: string): string {
  return {
    turn_started: "Turn 开始",
    message_delta: "Agent 回复",
    tool_call_requested: "请求工具",
    tool_call_started: "工具开始",
    tool_call_progress: "工具进度",
    tool_call_completed: "工具完成",
    checkpoint: "检查点",
    turn_completed: "Turn 完成",
    turn_failed: "Turn 失败",
  }[eventType] ?? eventType;
}

function agentSessionStateLabel(state: AgentSessionState): string {
  return { idle: "可提问", queued: "排队中", running: "运行中" }[state];
}

function agentSessionStateTone(state: AgentSessionState): "neutral" | "info" | "success" {
  return state === "idle" ? "success" : state === "running" ? "info" : "neutral";
}
