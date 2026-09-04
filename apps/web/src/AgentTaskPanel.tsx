import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleX, FlaskConical } from "lucide-react";
import { api, ApiRequestError } from "./api";
import {
  agentTaskCompletionPolicyLabel,
  agentTaskGateView,
} from "./agentTaskGate";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { useAgentSessionTasks } from "./query";
export { agentTaskPollInterval } from "./query";
import type { AgentTask } from "./types";

export function AgentTaskPanel({ user, sessionId }: { user: string; sessionId: string }) {
  const queryClient = useQueryClient();
  const tasks = useAgentSessionTasks(user, sessionId);
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
        emptyDetail="Agent 请求 Slurm 验证后，调度回执、Run、Evidence gate 与可选 Capsule 会显示在这里。"
      >
        <div className="agent-task-list">
          {items.map((task) => {
            const cancellation = agentTaskCancellation(task);
            const gate = agentTaskGateView(task);
            return (
              <article className="agent-task-card" key={task.task_id}>
                <div className="agent-task-card-heading">
                  <FlaskConical aria-hidden="true" size={17} />
                  <div>
                    <strong className="mono wrap-anywhere">{task.task_id}</strong>
                    <small>更新于 {formatTimestamp(task.updated_at)}</small>
                  </div>
                  <StatusBadge label={gate.label} tone={gate.tone} />
                </div>
                <dl className="agent-task-facts">
                  <div><dt>请求资源</dt><dd>{resourceLabel(task)}</dd></div>
                  <div><dt>Partition / QoS</dt><dd className="mono">{task.resource_envelope.partition} / {task.resource_envelope.qos}</dd></div>
                  <div><dt>完成门禁</dt><dd>{agentTaskCompletionPolicyLabel(task)}</dd></div>
                  <div><dt>时限</dt><dd>{task.resource_envelope.walltime_seconds} 秒</dd></div>
                  <div>
                    <dt>Run / Slurm Job</dt>
                    <dd>
                      {task.linked_run_id ? (
                        <a href={`/runs/${encodeURIComponent(task.linked_run_id)}?user=${encodeURIComponent(user)}&tab=overview`}>
                          {task.linked_run_id}
                        </a>
                      ) : "等待创建"}
                      {gate.slurmJobId ? <small className="mono"> · Job {gate.slurmJobId}</small> : null}
                    </dd>
                  </div>
                </dl>
                {gate.scheduleSummary ? (
                  <p className="agent-task-followup">
                    <strong>调度回执：</strong>{gate.scheduleSummary}。该回执仅证明请求已进入调度流程，不代表验证完成。
                  </p>
                ) : null}
                {gate.verifiedComplete ? (
                  <div className="agent-task-evidence">
                    <strong>已验证 Evidence</strong>
                    {gate.evidenceRefs.map((ref) => <code key={ref}>{ref}</code>)}
                    {gate.capsuleRef ? <code>Capsule: {gate.capsuleRef}</code> : null}
                  </div>
                ) : null}
                {!gate.verifiedComplete && (task.result?.evidence_refs.length ?? 0) > 0 ? (
                  <p className="agent-task-warning" role="status">
                    <strong>结果尚不可作为正式验证依据</strong> 当前记录存在结果引用，但尚未通过可验证的 terminal Evidence gate。
                  </p>
                ) : null}
                {gate.legacyUnverified && task.state === "succeeded" ? (
                  <p className="agent-task-warning" role="status">
                    <strong>旧版完成记录</strong> 缺少当前 completion gate 事实，不能据此生成新的正式实验结论。
                  </p>
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
                {gate.verifiedComplete ? (
                  <p className="agent-task-followup">Run、Evidence 完整性门禁已通过，可继续审阅或发起下一步。</p>
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
