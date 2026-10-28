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
