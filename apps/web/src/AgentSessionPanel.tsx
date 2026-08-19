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
          <p><strong>只读边界</strong>可读取平台、Workspace、Run、日志与 Evidence；不能提交或取消 Slurm 作业，也不能改写文件。</p>
        </div>
        <QueryBoundary
          pending={sessions.isPending}
          error={sessions.error}
          empty={(sessions.data?.items.length ?? 0) === 0}
          emptyTitle="还没有持久化对话"
          emptyDetail="创建后即可询问作业、日志、证据和只读 Workspace 信息。"
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
            {events.length ? events.map((item) => (
              <AgentEventRow key={item.event_id} event={item} />
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
              placeholder="询问 Run、日志、Evidence 或 Workspace…"
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

function AgentEventRow({ event }: { event: AgentTurnEvent }) {
  const text = agentEventText(event);
  const toolName = typeof event.payload.tool_name === "string" ? event.payload.tool_name : null;
  return (
    <article className={`agent-event agent-event-${event.event_type}`}>
      <div className="agent-event-sequence" aria-label={`事件 ${event.event_id}`}>
        <strong>#{event.event_id}</strong>
        <span>{event.sequence}</span>
      </div>
      <div>
        <header>
          <strong>{agentEventLabel(event.event_type)}</strong>
          <small>{toolName ?? formatTimestamp(event.created_at)}</small>
        </header>
        {text ? <p>{text}</p> : null}
        {!text && Object.keys(event.payload).length ? (
          <details>
            <summary>查看事件数据</summary>
            <pre><code>{JSON.stringify(event.payload, null, 2)}</code></pre>
          </details>
        ) : null}
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
  if (error && typeof error === "object" && "message" in error) {
    const message = error.message;
    return typeof message === "string" ? message : null;
  }
  return null;
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
