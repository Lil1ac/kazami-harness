import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { makeEvent } from '../test/fixtures';
import { EventTrail } from './EventTrail';

it('展示可编辑末轮的目标、额外写入和真实自测状态', () => {
  // 页面复用与 Runner 持久化相同的事件结构；提示不表示已经交付。
  render(<EventTrail events={[{ ...makeEvent(1), type: 'closeout_advice',
    payload: { model_turn: 12, remaining: 1, target: 'src/a.py', current_target_write: true,
      other_writes: ['scratch.py'], self_test: 'unavailable' } }]}
    connection="closed" error={null} canLoadOlder={false} loadingOlder={false} loadOlder={vi.fn()} />);
  expect(screen.getByText('可编辑收尾提醒')).toBeInTheDocument();
  expect(screen.getByText('第 12 轮 · 剩余 1 轮 · src/a.py（已核对写入） · 其他写入 scratch.py · 未执行自测')).toBeInTheDocument();
});

it('上下文事件使用中文摘要并保留真实拟请求轮次与超限状态', () => {
  // 事件窗口只负责展示历史摘要，详情聚合仍由 RunView 提供。
  render(
    <EventTrail
      events={[
        {
          ...makeEvent(1),
          type: 'context_prepared',
          payload: { instruction: { status: 'loaded' }, serialization: 'json_utf8_v1' },
        },
        {
          ...makeEvent(2),
          type: 'context_checked',
          payload: {
            model_turn: 3,
            input_bytes: 4096,
            max_input_bytes: 2048,
            message_count: 8,
            allowed: false,
          },
        },
      ]}
      connection="closed"
      error={null}
      canLoadOlder={false}
      loadingOlder={false}
      loadOlder={vi.fn()}
    />,
  );
  expect(screen.getByText('准备项目指令')).toBeInTheDocument();
  expect(screen.getByText('AGENTS.md · 已加载')).toBeInTheDocument();
  expect(screen.getByText('检查输入预算')).toBeInTheDocument();
  expect(screen.getByText('第 3 轮 · 4 KiB · 达到上限')).toBeInTheDocument();
});

it('区分同轮的普通输入与紧凑收尾，并保留公开自测的真实执行数', () => {
  // 自测和同轮两次检查来自三个独立的宿主事件。
  render(<EventTrail
    events={[
      { ...makeEvent(1), type: 'context_checked', payload: { model_turn: 21, input_bytes: 65642, allowed: false, phase: 'execution' } },
      { ...makeEvent(2), type: 'public_self_test_finished', payload: { self_test_id: 'check', status: 'passed', tests_run: 1, source: 'handoff' } },
      { ...makeEvent(3), type: 'budget_handoff_started', payload: { trigger: 'input_rejected' } },
      { ...makeEvent(4), type: 'context_checked', payload: { model_turn: 21, input_bytes: 4096, allowed: true, phase: 'handoff' } },
    ]}
    connection="closed" error={null} canLoadOlder={false} loadingOlder={false} loadOlder={vi.fn()}
  />);
  expect(screen.getByText(/普通输入 · 第 21 轮 · .* · 达到上限/)).toBeVisible();
  expect(screen.getByText('紧凑收尾 · 第 21 轮 · 4 KiB · 允许请求')).toBeVisible();
  expect(screen.getByText('公开自测结果')).toBeVisible();
  expect(screen.getByText('通过 · 实际执行 1 项')).toBeVisible();
  expect(screen.getByText('普通输入达到上限')).toBeVisible();
});

it('编辑失败的轨迹摘要直接展示后端原因，完整载荷仍可展开核对', () => {
  // 原因来自失败事件，不用前端按错误码猜测，也不覆盖事件原文。
  render(
    <EventTrail
      events={[
        {
          ...makeEvent(1),
          type: 'tool_result',
          payload: {
            name: 'edit_file',
            call_id: 'edit',
            result: {
              ok: false,
              error: { code: 'edit_not_found', message: '文件包含混合换行，请提供原文。' },
            },
          },
        },
      ]}
      connection="closed"
      error={null}
      canLoadOlder={false}
      loadingOlder={false}
      loadOlder={vi.fn()}
    />,
  );
  expect(screen.getByText('edit_file · 文件包含混合换行，请提供原文。')).toBeVisible();
});
