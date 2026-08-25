import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleX, FlaskConical } from "lucide-react";
import { api, ApiRequestError } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import type { AgentTask } from "./types";

export function AgentTaskPanel({ user, sessionId }: { user: string; sessionId: string }) {
  const queryClient = useQueryClient();
  const tasks = useQuery({
    queryKey: ["agent-tasks", user, sessionId],
    queryFn: ({ signal }) => api.agentSessionTasks(user, sessionId, signal),
    refetchInterval: (query) => agentTaskPollInterval(query.state.data?.items ?? []),
    retry: false,
  });
  const cancel = useMutation({
    mutationFn: ({ taskId, expectedVersion }: { taskId: string; expectedVersion: number }) => (
      api.cancelAgentTask(user, taskId, expectedVersion)
    ),
    onSuccess: (task) => {
      queryClient.setQueryData<{ items: AgentTask[] }>(
        ["agent-tasks", user, sessionId],
        (current) => ({
          items: (current?.items ?? []).map((item) => item.task_id === task.task_id ? task : item),
        }),
      );
    },
  });
  const items = tasks.data?.items ?? [];

  return (
    <section className="agent-task-panel" aria-labelledby="agent-task-heading">
      <header>
        <div>
          <p className="panel-kicker">Slurm validation lifecycle</p>
          <h3 id="agent-task-heading">异步验证任务</h3>
        </div>
        <small>{items.length} 个任务</small>
      </header>
      <QueryBoundary
        pending={tasks.isPending}
        error={tasks.error}
        empty={items.length === 0}
        emptyTitle="尚无验证任务"
        emptyDetail="Agent 请求 Slurm 验证后，资源、Run 和 Evidence 会显示在这里。"
      >
        <div className="agent-task-list">
          {items.map((task) => {
            const cancellation = agentTaskCancellation(task);
            return (
              <article className="agent-task-card" key={task.task_id}>
                <div className="agent-task-card-heading">
                  <FlaskConical aria-hidden="true" size={17} />
                  <div>
                    <strong className="mono wrap-anywhere">{task.task_id}</strong>
                    <small>更新于 {formatTimestamp(task.updated_at)}</small>
                  </div>
                  <StatusBadge label={agentTaskStateLabel(task)} tone={agentTaskTone(task)} />
                </div>
                <dl className="agent-task-facts">
                  <div><dt>请求资源</dt><dd>{resourceLabel(task)}</dd></div>
                  <div><dt>Partition / QoS</dt><dd className="mono">{task.resource_envelope.partition} / {task.resource_envelope.qos}</dd></div>
                  <div><dt>时限</dt><dd>{task.resource_envelope.walltime_seconds} 秒</dd></div>
                  <div>
                    <dt>Run / Slurm Job</dt>
                    <dd>{task.linked_run_id ? (
                      <a href={`/runs/${encodeURIComponent(task.linked_run_id)}?user=${encodeURIComponent(user)}&tab=overview`}>
                        {task.linked_run_id}
                      </a>
                    ) : "等待创建"}</dd>
                  </div>
                </dl>
                {task.result?.evidence_refs.length ? (
                  <div className="agent-task-evidence">
                    <strong>Evidence</strong>
                    {task.result.evidence_refs.map((ref) => <code key={ref}>{ref}</code>)}
                  </div>
                ) : null}
                {task.state === "auth_required" ? (
                  <p className="agent-task-warning" role="alert">
                    <strong>集群认证已失效</strong> 请恢复连接认证后重新发起验证。
                  </p>
                ) : null}
                {task.result?.error_code || task.result?.message ? (
                  <p className="agent-task-error" role="alert">
                    {task.result.error_code ? <code>{task.result.error_code}</code> : null}
                    {task.result.message ? <span>{task.result.message}</span> : null}
                  </p>
                ) : null}
                {task.result && task.state !== "auth_required" ? (
                  <p className="agent-task-followup">结果与 Evidence 已回写到 Agent 会话，可继续审阅或发起下一步。</p>
                ) : null}
                {cancellation ? (
                  <button
                    className="button secondary"
                    type="button"
                    disabled={cancel.isPending || task.cancel_requested}
                    onClick={() => cancel.mutate(cancellation)}
                  >
                    <CircleX aria-hidden="true" size={15} />
                    {task.cancel_requested ? "正在取消" : "取消验证"}
                  </button>
                ) : null}
              </article>
            );
          })}
        </div>
      </QueryBoundary>
      {cancel.error ? (
        <p className="agent-task-error" role="alert">
          <strong>取消失败</strong>
          <span>{cancel.error.message}</span>
          {cancel.error instanceof ApiRequestError ? <code>{cancel.error.code}</code> : null}
        </p>
      ) : null}
    </section>
  );
}

export function agentTaskPollInterval(tasks: AgentTask[]): number | false {
  return tasks.some((task) => task.state === "pending" || task.state === "running") ? 2_000 : false;
}

export function agentTaskCancellation(
  task: AgentTask,
): { taskId: string; expectedVersion: number } | null {
  if (task.state !== "pending" && task.state !== "running") return null;
  return { taskId: task.task_id, expectedVersion: task.version };
}

function resourceLabel(task: AgentTask): string {
  const resource = task.resource_envelope;
  return `${resource.cpus} CPU · ${resource.memory_mib} MiB · ${resource.gpus} GPU`;
}

function agentTaskStateLabel(task: AgentTask): string {
  if (task.cancel_requested && (task.state === "pending" || task.state === "running")) return "取消中";
  return ({
    pending: "等待调度",
    running: "运行中",
    succeeded: "已成功",
    failed: "已失败",
    cancelled: "已取消",
    auth_required: "需要认证",
  } as const)[task.state];
}

function agentTaskTone(task: AgentTask): "info" | "success" | "warning" | "danger" | "neutral" {
  if (task.cancel_requested) return "warning";
  if (task.state === "succeeded") return "success";
  if (task.state === "failed" || task.state === "auth_required") return "danger";
  if (task.state === "pending" || task.state === "running") return "info";
  return "neutral";
}
