import type { ConversationTurn, ConversationView, RunView } from './types';

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
    validation_status: view.result.validation_status,
    created_at: view.created_at,
    interrupted: view.interrupted,
  };
  return {
    ...current,
    latest_run_id: view.run_id,
    can_continue: false,
    busy_run_id: view.active ? view.run_id : null,
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
