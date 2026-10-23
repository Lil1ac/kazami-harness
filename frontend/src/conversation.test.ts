import { expect, it } from 'vitest';
import { mergeConfirmedRun } from './conversation';
import { makeRun } from './test/fixtures';
import type { ConversationView, RunView } from './types';

/** 构造已有中断首轮；返回仅含一条用户消息的真实契约视图，不产生执行尝试。 */
function original(): ConversationView {
  return {
    session_id: 'run-a',
    latest_run_id: 'run-a',
    can_continue: true,
    blocked_code: null,
    blocked_reason: null,
    busy_run_id: null,
    continuation_notice: '可发送继续',
    turns: [
      {
        run_id: 'run-a',
        parent_run_id: null,
        turn_index: 1,
        prompt: '继续',
        final_answer: null,
        execution_status: 'running',
        validation_status: 'unverified',
        created_at: null,
        interrupted: true,
      },
    ],
  };
}

/** 构造已确认的第二轮；独立编号和父轮关系由服务端确定，返回RunView供合并。 */
function followed(): RunView {
  return makeRun({
    run_id: 'run-b',
    turn_index: 2,
    prompt: '继续',
    session_id: 'run-a',
    parent_run_id: 'run-a',
  });
}

it('中断后相同文字的继续也新增用户轮，并保留原轮中断事实', () => {
  // 两条要求即使文字相同，也不能覆盖或合并用户消息。
  const current = original();
  const merged = mergeConfirmedRun(current, followed());
  expect(merged.turns).toHaveLength(2);
  expect(merged.turns.map(/* 每轮独立保留运行编号。 */ (turn) => turn.run_id)).toEqual([
    'run-a',
    'run-b',
  ]);
  expect(merged.turns[0].interrupted).toBe(true);
  expect(merged.turns[1].parent_run_id).toBe('run-a');
  expect(merged.continuation_notice).toBeNull();
  expect(current.turns).toHaveLength(1);
});

it('同一个追加请求重复确认只更新原运行，不重复增加消息', () => {
  // 幂等POST确认沿用run_id，消息数保持真实两轮。
  const view = followed();
  const twice = mergeConfirmedRun(mergeConfirmedRun(original(), view), view);
  expect(twice.turns).toHaveLength(2);
  expect(twice.latest_run_id).toBe('run-b');
  expect(twice.turns[0].parent_run_id).toBeNull();
});

it('迟到的旧确认不倒退最新轮，也不接纳相同轮次的其他运行', () => {
  // 别的标签页已推进后，旧响应只确认旧请求，不重写最新会话头。
  const second = mergeConfirmedRun(original(), followed());
  const third = mergeConfirmedRun(
    second,
    makeRun({
      run_id: 'run-c',
      turn_index: 3,
      parent_run_id: 'run-b',
      prompt: '补充说明',
    }),
  );
  expect(mergeConfirmedRun(third, followed())).toBe(third);
  expect(mergeConfirmedRun(second, { ...followed(), run_id: 'unexpected' })).toBe(second);
});
