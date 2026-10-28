import { expect, it } from 'vitest';
import { activitySteps } from './activity';
import { makeEvent } from './test/fixtures';

it('按调用编号配对工具，保留真实参数与失败结果', () => {
  // 工具结果会覆盖同一操作状态，不能把一个调用展示成两次执行。
  const steps = activitySteps([
    {
      ...makeEvent(1),
      type: 'tool_started',
      payload: { call_id: 'a', name: 'read_file', arguments_json: '{"path":"app.py"}' },
    },
    {
      ...makeEvent(2),
      type: 'tool_result',
      payload: {
        call_id: 'a',
        name: 'read_file',
        result: { ok: false, error: { message: '不存在' } },
      },
    },
  ]);
  expect(steps).toHaveLength(1);
  expect(steps[0]).toMatchObject({
    label: '读取文件 · app.py',
    state: 'failed',
    arguments: { path: 'app.py' },
    result: { ok: false },
  });
});

it('终态缺少结果时保持未知，验收通过必须来自宿主事件', () => {
  // 运行结束不能替代某个工具的结果或宿主验收结论。
  const steps = activitySteps([
    {
      ...makeEvent(1),
      type: 'tool_started',
      payload: { call_id: 'a', name: 'write_file', arguments_json: 'damaged' },
    },
    {
      ...makeEvent(2),
      type: 'verification_result',
      payload: { verification: { status: 'passed' } },
    },
    { ...makeEvent(3), type: 'run_finished', payload: {} },
  ]);
  expect(steps[0]).toMatchObject({ state: 'unknown', arguments: 'damaged' });
  expect(steps[1]).toMatchObject({ label: '宿主独立验收', state: 'passed' });
});

it('只有确认活动的运行才把未配对步骤显示为执行中', () => {
  // 中断或尚未核对的历史不能被一个旧开始事件伪装为仍在执行。
  const started = {
    ...makeEvent(1),
    type: 'tool_started',
    payload: { call_id: 'a', name: 'read_file', arguments_json: '{}' },
  };
  expect(activitySteps([started])[0].state).toBe('unknown');
  expect(activitySteps([started], true)[0].state).toBe('running');
});

it('局部编辑显示文件与真实替换参数，版本冲突保留失败原因', () => {
  // 沿用相同调用配对，不因新工具名称推断编辑成功，也不把旧文本当作渲染代码。
  const args = {
    path: 'app.py',
    old_text: 'a - b',
    new_text: 'a + b',
    expected_sha256: 'a'.repeat(64),
  };
  const result = { ok: false, error: { code: 'file_changed', message: '文件已变化，请重新读取' } };
  const steps = activitySteps([
    {
      ...makeEvent(1),
      type: 'tool_started',
      payload: { call_id: 'edit', name: 'edit_file', arguments_json: JSON.stringify(args) },
    },
    {
      ...makeEvent(2),
      type: 'tool_result',
      payload: { call_id: 'edit', name: 'edit_file', result },
    },
  ]);
  expect(steps).toHaveLength(1);
  expect(steps[0]).toMatchObject({
    label: '局部编辑文件 · app.py',
    state: 'failed',
    arguments: args,
    result,
  });
});
