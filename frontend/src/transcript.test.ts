import { expect, it } from 'vitest';
import { makeEvent } from './test/fixtures';
import { projectTranscript } from './transcript';
import type { RunEvent } from './types';

/** 返回同一模型轮的真实文字增量；seq 是持久化游标，text 为新增片段。 */
function delta(seq: number, text: string): RunEvent {
  return {
    ...makeEvent(seq),
    type: 'model_delta',
    payload: { model_turn: 1, channel: 'text', delta: text },
  };
}

it('一千个文字片段累积为一个块，原始窗口之外的文字不会丢失，补读不重复', () => {
  const incoming = Array.from(
    { length: 1000 },
    /* 使用连续序号模拟长模型输出。 */ (_, index) => delta(index + 1, '中'),
  );
  const runId = incoming[0].run_id;
  const first = projectTranscript([], incoming.slice(0, 500), runId);
  const full = projectTranscript(first, incoming.slice(500), runId);
  expect(full).toHaveLength(1);
  expect(full[0].payload.text).toBe('中'.repeat(1000));
  expect(projectTranscript(full, incoming, runId)).toEqual(full);
});

it('完整模型消息覆盖增量，并保留有工具正文的过程语义', () => {
  const first = delta(1, '读取');
  const complete = {
    ...makeEvent(3),
    type: 'assistant_message',
    payload: {
      model_turn: 1,
      message: { content: '读取文件后检查调用方。', tool_calls: [{ id: 'read' }] },
    },
  };
  const items = projectTranscript([], [first, delta(2, '文件'), complete], first.run_id);
  expect(items).toHaveLength(1);
  expect(items[0].seq).toBe(1);
  expect(items[0].payload).toMatchObject({
    text: '读取文件后检查调用方。',
    complete: true,
    has_tools: true,
  });
});

it('参数生成与真实工具执行分开，开始事件到达后替换占位，结果不伪造成功', () => {
  const first = {
    ...makeEvent(1),
    type: 'model_delta',
    payload: {
      model_turn: 1,
      channel: 'tool_arguments',
      tool_index: 0,
      call_id: 'read',
      name: 'read_file',
      delta: '{"path":',
    },
  };
  const preparing = projectTranscript([], [first], first.run_id);
  expect(preparing[0].type).toBe('tool_preparing');
  const start = {
    ...makeEvent(2),
    type: 'tool_started',
    payload: { call_id: 'read', name: 'read_file', arguments_json: '{"path":"a.py"}' },
  };
  const items = projectTranscript(preparing, [start], first.run_id);
  expect(items).toHaveLength(1);
  expect(items[0].type).toBe('tool_started');
  expect(items[0].payload).not.toHaveProperty('result');
});

it('其他运行增量不污染本轮文字，缺少完整结束时保留未完成标记', () => {
  const first = delta(1, '部分输出');
  const other = { ...delta(2, '另一轮'), run_id: 'other' };
  const items = projectTranscript([], [first, other], first.run_id);
  expect(items).toHaveLength(1);
  expect(items[0].payload).toMatchObject({ text: '部分输出', complete: false });
});
