import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { makeEvent } from '../test/fixtures';
import { TurnActivity } from './TurnActivity';

it('运行中自动展示真实过程文字，结束后折叠并从过程移除最终答复', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const events = [
    {
      ...makeEvent(1),
      type: 'model_delta',
      payload: { model_turn: 1, channel: 'text', delta: '我先检查调用方。' },
    },
  ];
  const { rerender } = render(
    <QueryClientProvider client={client}>
      <TurnActivity runId="run-a" liveEvents={events} active phase="请求模型" onInspect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(screen.getByText('我先检查调用方。')).toBeVisible();
  expect(screen.getByText('我先检查调用方。').closest('details')).toHaveAttribute('open');
  const complete = {
    ...makeEvent(2),
    type: 'assistant_message',
    payload: { model_turn: 1, message: { content: '最终结果。' } },
  };
  rerender(
    <QueryClientProvider client={client}>
      <TurnActivity
        runId="run-a"
        liveEvents={[...events, complete]}
        active={false}
        phase="已结束"
        finalAnswer="最终结果。"
        onInspect={vi.fn()}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByText('执行过程').closest('details')).not.toHaveAttribute('open');
  expect(screen.queryByText('最终结果。')).not.toBeInTheDocument();
});

it('展开执行过程即可看到真实编辑冲突说明，原始参数仍按需展开并安全转义', async () => {
  // 真实事件驱动组件；不把失败提示藏在JSON里，也不执行消息中的HTML。
  const message = '文件已在读取后变化，请重新read_file。<img src=x onerror=alert(1)>';
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={client}>
      <TurnActivity
        runId="edit-run"
        liveEvents={[
          {
            ...makeEvent(1),
            type: 'tool_started',
            payload: {
              call_id: 'edit',
              name: 'edit_file',
              arguments_json: '{"path":"app.py"}',
            },
          },
          {
            ...makeEvent(2),
            type: 'tool_result',
            payload: {
              call_id: 'edit',
              name: 'edit_file',
              result: { ok: false, data: null, error: { code: 'file_changed', message } },
            },
          },
        ]}
        active={false}
        phase="已结束"
        onInspect={vi.fn()}
      />
    </QueryClientProvider>,
  );
  await userEvent.setup().click(screen.getByText('执行过程'));
  expect(screen.getByText('局部编辑文件 · app.py')).toBeVisible();
  expect(screen.getByText(message)).toBeVisible();
  expect(screen.getByText('参数与结果').closest('details')).not.toHaveAttribute('open');
  expect(container.querySelector('img')).toBeNull();
});
