import type { ConversationAttempt, ConversationTurn, ConversationView, RunView } from './types';

/** 将旧单次执行兼容为 attempts；返回真实已接纳记录，不创建新用户要求。 */
export function turnAttempts(turn: ConversationTurn): ConversationAttempt[] {
  return turn.attempts?.length
    ? turn.attempts
    : [
        {
          run_id: turn.run_id,
          attempt_index: 1,
          interrupted: turn.interrupted,
          resume_source: null,
          final_answer: turn.final_answer,
          execution_status: turn.execution_status,
          validation_status: turn.validation_status,
          created_at: turn.created_at,
        },
      ];
}

/** 将已确认 RunView 合入会话；恢复仅更新同轮 attempts，普通追加才增加用户消息。 */
export function mergeConfirmedRun(current: ConversationView, view: RunView): ConversationView {
  const attempt: ConversationAttempt = {
    run_id: view.run_id,
    attempt_index: view.attempt_index || 1,
    interrupted: view.interrupted,
    resume_source: view.resume_source || null,
    final_answer: view.result.final_answer,
    execution_status: view.result.execution_status,
    validation_status: view.result.validation_status,
    created_at: view.created_at,
  };
  const ordinal = view.turn_index || current.turns.length + 1;
  const existing = current.turns.find(
    /* 恢复保持原用户序号；不依赖正文相同而猜测两次不同要求。 */ (item) =>
      item.turn_index === ordinal,
  );
  // 网络迟到的幂等确认可能对应较早尝试；不能让会话最新指针倒退。
  const newerTurn = current.turns.some(
    /* 已有更新用户要求时，只补读原运行即可，不能改会话头。 */ (item) => item.turn_index > ordinal,
  );
  const newerAttempt =
    existing &&
    turnAttempts(existing).some(
      /* 同轮另一标签页已恢复到后续尝试时保留最新执行。 */ (item) =>
        item.attempt_index > attempt.attempt_index,
    );
  if (newerTurn || newerAttempt) return current;
  const updated: ConversationTurn = {
    run_id: view.run_id,
    parent_run_id: existing ? existing.parent_run_id : view.parent_run_id || current.latest_run_id,
    turn_index: ordinal,
    interrupted: view.interrupted,
    prompt: existing?.prompt || view.prompt,
    final_answer: view.result.final_answer,
    execution_status: view.result.execution_status,
    validation_status: view.result.validation_status,
    created_at: existing?.created_at ?? view.created_at,
    attempts: [
      ...(existing
        ? turnAttempts(existing).filter(
            /* 幂等确认已有运行时替换快照，不增加重复尝试。 */ (item) =>
              item.run_id !== view.run_id,
          )
        : []),
      attempt,
    ],
  };
  return {
    ...current,
    latest_run_id: view.run_id,
    can_continue: false,
    busy_run_id: view.active ? view.run_id : null,
    blocked_code: 'run_busy',
    blocked_reason: view.resume_source
      ? '正在核对恢复尝试的运行状态。'
      : '正在核对新一轮运行状态。',
    turns: existing
      ? current.turns.map(
          /* 保持用户消息的既有顺序与序号。 */ (item) => (item === existing ? updated : item),
        )
      : [...current.turns, updated],
  };
}
