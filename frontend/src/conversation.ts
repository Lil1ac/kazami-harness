import type { ConversationDelivery, ConversationTurn, ConversationView, RunView } from './types';

export const maxFollowupLength = 65_536; // 续轮正文的宿主上限，草稿恢复与输入使用同一值。

/** 从实时 run 投影同一交付索引；运行中返回空值，避免与终态快照混用。 */
export function runDelivery(run: RunView): ConversationDelivery | null {
  if (run.active) return null;
  const changedFiles = run.local_changes
    ? run.local_changes.status === 'ready'
      ? run.local_changes.files.length
      : null
    : run.project?.snapshot_status === 'ready'
      ? run.project.files.length
      : null;
  return {
    changed_files: changedFiles,
    artifacts: run.result.artifacts.length,
    has_verification: Boolean(run.result.verification || run.result.verification_attempts.length),
  };
}

/** 将已确认运行并入对应用户轮；重复确认不增消息，迟到确认不倒退最新轮次。 */
export function mergeConfirmedRun(current: ConversationView, view: RunView): ConversationView {
  const ordinal = view.turn_index || current.turns.length + 1;
  const existing = current.turns.find(
    /* 一条用户要求对应唯一运行，不按相同正文合并不同要求。 */ (turn) =>
      turn.turn_index === ordinal,
  );
  if (
    current.turns.some(
      /* 更新轮次已经接纳时保留当前会话头。 */ (turn) => turn.turn_index > ordinal,
    ) ||
    (existing && existing.run_id !== view.run_id)
  )
    return current;

  const updated: ConversationTurn = {
    run_id: view.run_id,
    parent_run_id: existing ? existing.parent_run_id : view.parent_run_id || current.latest_run_id,
    turn_index: ordinal,
    prompt: view.prompt,
    final_answer: view.result.final_answer,
    execution_status: view.result.execution_status,
    stop_reason: view.result.stop_reason,
    validation_status: view.result.validation_status,
    created_at: view.created_at,
    interrupted: view.interrupted,
    delivery: runDelivery(view),
    file_references: existing?.file_references,
  };
  return {
    ...current,
    latest_run_id: view.run_id,
    can_continue: false,
    active_run_ids: view.active ? [view.run_id] : [],
    blocked_code: 'run_busy',
    blocked_reason: '正在核对新一轮运行状态。',
    continuation_notice: null,
    turns: existing
      ? current.turns.map(
          /* 只更新原运行的已确认快照。 */ (turn) => (turn.run_id === view.run_id ? updated : turn),
        )
      : [...current.turns, updated],
  };
}
/** 取 run 当前真正等待的决定入口；暂无待处理快照时返回 null。 */
export function pendingDecisionLabel(run?: RunView): string | null {
  if (run?.result.permissions.pending) return '审阅动作';
  const request = run?.result.planning.pending;
  if (!request) return null;
  return request.kind === 'question' ? '回答问题' : '审阅计划';
}
