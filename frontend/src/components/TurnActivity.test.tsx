import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { makeEvent } from '../test/fixtures';
import { TurnActivity } from './TurnActivity';
import * as api from '../api';

it('过程文字与工具按序显示，思考无需二次展开，用户展开后终态仍保留阅读状态', async () => {
  // 同一轮连续增量驱动展示；不以静态截图代替流式更新与终态切换验证。
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const events = [
    {
      ...makeEvent(1),
      type: 'model_delta',
      payload: { model_turn: 1, channel: 'reasoning', delta: '需要先核对' },
    },
    {
      ...makeEvent(2),
      type: 'model_delta',
      payload: { model_turn: 1, channel: 'reasoning', delta: '入口。' },
    },
    {
      ...makeEvent(3),
      type: 'assistant_message',
      payload: {
        model_turn: 1,
        message: {
          reasoning_content: '需要先核对入口。',
          content: '我先读取文件。',
          tool_calls: [{}],
        },
      },
    },
    {
      ...makeEvent(4),
      type: 'tool_started',
      payload: { call_id: 'read', name: 'read_file', arguments_json: '{"path":"app.py"}' },
    },
    {
      ...makeEvent(5),
      type: 'tool_result',
      payload: {
        call_id: 'read',
        name: 'read_file',
        result: { ok: true, data: { content: '代码' } },
      },
    },
    {
      ...makeEvent(6),
      type: 'model_delta',
      payload: { model_turn: 2, channel: 'text', delta: '已经找到入口。' },
    },
  ];
  /** 以active指定服务端状态、rows指定当前事件，返回同一运行的可更新测试界面。 */
  function view(active: boolean, rows = events) {
    return (
      <QueryClientProvider client={client}>
        <TurnActivity
          runId="run-a"
          liveEvents={rows}
          active={active}
          phase="请求模型"
          onInspect={vi.fn()}
        />
      </QueryClientProvider>
    );
  }
  const { rerender, container } = render(view(true, events.slice(0, 1)));
  expect(screen.getByText('需要先核对')).toBeVisible();
  await user.click(screen.getByText('请求模型'));
  rerender(view(true));
  expect(screen.getByText('请求模型').closest('details')).not.toHaveAttribute('open');
  await user.click(screen.getByText('请求模型'));
  expect(screen.getByText('需要先核对入口。')).toBeVisible();
  expect(screen.getByText('思考').closest('details')).toHaveClass('turn-activity');
  const rows = [...container.querySelectorAll('.activity-steps > li')];
  expect(rows).toHaveLength(4);
  expect(within(rows[0] as HTMLElement).getByText('需要先核对入口。')).toBeVisible();
  expect(rows[1]).toHaveTextContent('我先读取文件。');
  expect(rows[2]).toHaveTextContent('app.py');
  expect(rows[3]).toHaveTextContent('已经找到入口。');
  rerender(view(false));
  expect(screen.getByText('执行过程').closest('details')).toHaveAttribute('open');
  expect(screen.getByText('需要先核对入口。')).toBeVisible();
});

it('历史过程展开后跨页恢复文字、工具和中断前缀，重新展开不重复读取', async () => {
  // 使用真实投影和分页接口契约，历史与实时使用同一可读正文结构。
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const getEvents = vi.spyOn(api, 'getEvents');
  getEvents.mockResolvedValueOnce({
    items: [
      {
        ...makeEvent(1),
        type: 'assistant_message',
        payload: {
          model_turn: 1,
          message: {
            reasoning_content: '先查看调用关系。',
            content: '读取入口文件。',
            tool_calls: [{}],
          },
        },
      },
      {
        ...makeEvent(2),
        type: 'tool_started',
        payload: { call_id: 'read', name: 'read_file', arguments_json: '{"path":"main.py"}' },
      },
    ],
    has_more: true,
    last_seq: 2,
  });
  getEvents.mockResolvedValueOnce({
    items: [
      {
        ...makeEvent(3),
        type: 'tool_result',
        payload: { call_id: 'read', name: 'read_file', result: { ok: true, data: {} } },
      },
      {
        ...makeEvent(4),
        type: 'model_delta',
        payload: { model_turn: 2, channel: 'text', delta: '已找到需要修改的位置' },
      },
    ],
    has_more: false,
    last_seq: 4,
  });
  try {
    render(
      <QueryClientProvider client={client}>
        <TurnActivity runId="run-a" active={false} phase="已结束" onInspect={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(getEvents).not.toHaveBeenCalled();
    await user.click(screen.getByText('执行过程'));
    expect(await screen.findByText('已找到需要修改的位置')).toBeVisible();
    expect(screen.getByText('先查看调用关系。')).toBeVisible();
    expect(screen.getByText('读取入口文件。')).toBeVisible();
    expect(screen.getByText('输出未完成')).toBeVisible();
    expect(getEvents).toHaveBeenNthCalledWith(2, 'run-a', 2, 100, expect.any(AbortSignal));
    await user.click(screen.getByText('执行过程'));
    await user.click(screen.getByText('执行过程'));
    expect(screen.getByText('先查看调用关系。')).toBeVisible();
    expect(getEvents).toHaveBeenCalledTimes(2);
  } finally {
    getEvents.mockRestore();
  }
});

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
