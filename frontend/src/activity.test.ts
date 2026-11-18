import { expect, it } from 'vitest';
import { activitySteps, toolFailureMessage } from './activity';
import { makeEvent } from './test/fixtures';

it('多次检查各自保留，修复后通过不会覆盖首次失败', () => {
  // 开始和结果按实际顺序成对投影，用户可回看每次测试。
  const steps = activitySteps([
    { ...makeEvent(1), type: 'verification_started', payload: { repair_attempt: 0 } },
    {
      ...makeEvent(2),
      type: 'verification_result',
      payload: { repair_attempt: 0, verification: { status: 'failed' } },
    },
    { ...makeEvent(3), type: 'repair_started', payload: { repair_attempt: 1 } },
    { ...makeEvent(4), type: 'verification_started', payload: { repair_attempt: 1 } },
    {
      ...makeEvent(5),
      type: 'verification_result',
      payload: { repair_attempt: 1, verification: { status: 'passed' } },
    },
  ]);
  expect(steps).toHaveLength(2);
  expect(steps.map((step) => step.state)).toEqual(['failed', 'passed']);
  expect(steps[1].label).toBe('第 1 次修复后检查');
});

it('收尾宿主自测按自身编号配对，模型自测仍沿用命令活动', () => {
  // 只把宿主发起的自测新增为活动，避免重复显示模型 run_command。
  const steps = activitySteps([
    { ...makeEvent(1), type: 'public_self_test_finished', payload: { self_test_id: 'tool-test', status: 'passed', source: 'model' } },
    { ...makeEvent(2), type: 'public_self_test_started', payload: { self_test_id: 'host-test', source: 'handoff' } },
    { ...makeEvent(3), type: 'public_self_test_finished',
      payload: { self_test_id: 'host-test', status: 'all_skipped', source: 'handoff', reason: 'self_test_all_skipped' } },
  ]);
  expect(steps).toHaveLength(1);
  expect(steps[0]).toMatchObject({ label: '公开自测', state: 'failed', errorMessage: '用例全部跳过' });
});

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
    errorMessage: '文件已变化，请重新读取',
  });
});

it('错误说明只来自明确失败的工具结果，损坏字段和成功结果不伪造失败', () => {
  // 终态和原始载荷可能不完整，不能因出现error字段就把成功步骤改成失败。
  for (const result of [
    null,
    'damaged',
    { ok: true, error: { message: '过期错误' } },
    { ok: false, error: { message: 123 } },
    { ok: false, error: { message: ' ' } },
  ])
    expect(toolFailureMessage(result)).toBeNull();
  expect(
    toolFailureMessage({ ok: false, error: { message: '原片段匹配多处，请补充上下文。' } }),
  ).toBe('原片段匹配多处，请补充上下文。');
});
