import { expect, it } from 'vitest';
import { mergeConfirmedRun, turnAttempts } from './conversation';
import { makeRun } from './test/fixtures';
import type { ConversationView, RunView } from './types';

/** 构造一个用户要求的旧版会话；不含 attempts，用于验证旧记录兼容。 */
function original(): ConversationView {
  return {
    session_id: 'run-a',
    latest_run_id: 'run-a',
    can_continue: false,
    blocked_code: 'run_interrupted',
    blocked_reason: '执行中断',
    busy_run_id: null,
    turns: [
      {
        run_id: 'run-a',
        parent_run_id: null,
        turn_index: 1,
        prompt: '继续修复',
        final_answer: null,
        execution_status: 'failed',
        validation_status: 'unverified',
        created_at: null,
      },
    ],
  };
}

/** 构造已由后端确认的同轮恢复尝试，仍使用原用户序号。 */
function resumed(): RunView {
  return makeRun({
    run_id: 'run-b',
    turn_index: 1,
    attempt_index: 2,
    prompt: '继续修复',
    session_id: 'run-a',
    resume_source: {
      source_run_id: 'run-a',
      checkpoint_id: '00000000-0000-4000-8000-000000000001',
      checkpoint_sha256: 'a'.repeat(64),
    },
  });
}

it('旧版单运行映射为首次尝试，恢复不重复用户消息且保留空父轮', () => {
  // 对话按要求组织，原始失败记录不会被恢复结果替换掉。
  const current = original();
  expect(turnAttempts(current.turns[0])[0].attempt_index).toBe(1);
  const merged = mergeConfirmedRun(current, resumed());
  expect(merged.turns).toHaveLength(1);
  expect(merged.turns[0].parent_run_id).toBeNull();
  expect(merged.turns[0].prompt).toBe('继续修复');
  expect(
    merged.turns[0].attempts?.map(/* 顺序按各独立执行先后保留。 */ (item) => item.run_id),
  ).toEqual(['run-a', 'run-b']);
  expect(current.turns[0].run_id).toBe('run-a');
});

it('同一个恢复请求重复确认只更新一次尝试，明确追加才增加用户轮', () => {
  // 幂等结果必须保持同一运行编号，不能重复计数。
  const view = resumed();
  const twice = mergeConfirmedRun(mergeConfirmedRun(original(), view), view);
  expect(twice.turns).toHaveLength(1);
  expect(twice.turns[0].attempts).toHaveLength(2);
  const next = mergeConfirmedRun(
    twice,
    makeRun({
      run_id: 'run-c',
      turn_index: 2,
      attempt_index: 1,
      parent_run_id: 'run-b',
      prompt: '补充新要求',
    }),
  );
  expect(next.turns).toHaveLength(2);
  expect(next.turns[1].parent_run_id).toBe('run-b');
});

it('网络迟到的旧恢复确认不倒退已有最新用户轮或后续恢复尝试', () => {
  // 另一标签页已推进时，旧 POST 仅确认原请求，不能改写最新会话头。
  const view = resumed();
  const second = mergeConfirmedRun(original(), view);
  const third = mergeConfirmedRun(second, {
    ...view,
    run_id: 'run-c',
    attempt_index: 3,
  });
  expect(mergeConfirmedRun(third, view)).toBe(third);
  const next = mergeConfirmedRun(
    second,
    makeRun({
      run_id: 'run-next',
      turn_index: 2,
      prompt: '新的用户要求',
    }),
  );
  expect(mergeConfirmedRun(next, view)).toBe(next);
});
