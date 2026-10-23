import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { makeEvent } from '../test/fixtures';
import { EventTrail } from './EventTrail';

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
