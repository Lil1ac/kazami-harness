import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getTasks, mutateTask } from '../api';
import type { AgentTask } from '../types';
import { TasksPanel } from './TasksPanel';

vi.mock(
  '../api',
  /* 只替换 HTTP，组件使用真实查询和版本提交。 */ () => ({
    getTasks: vi.fn(),
    mutateTask: vi.fn(),
    errorMessage: (error: Error) => error.message,
  }),
);

const item: AgentTask = {
  id: 'task-a',
  display_id: 1,
  subject: '实现接口',
  description: '保持字段约束',
  acceptance: ['检查接口返回'],
  parent_task_id: null,
  depends_on: [],
  status: 'pending',
  revision: 7,
  blocked_reason: '前置任务未完成',
  is_group: false,
  source: null,
  completion_summary: null,
  evidence_refs: [],
  run_ids: ['run-a'],
}; // 当前服务端工作项。

/** 在真实查询容器展示组件，historical 指定历史只读，返回挂载结果。 */
function show(historical = false) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
        })
      }
    >
      <MemoryRouter>
        <TasksPanel runId="run-a" active={false} historical={historical} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(
  /* 每例独立服务响应。 */ () => {
    vi.resetAllMocks();
    vi.mocked(getTasks).mockResolvedValue({ revision: 12, items: [item] });
    vi.mocked(mutateTask).mockResolvedValue({});
  },
);

it('显示真实阻塞原因和证据，取消携带当前版本且不启动运行', async () => {
  const user = userEvent.setup();
  show();
  expect(await screen.findByText('前置任务未完成')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '取消任务' }));
  await waitFor(
    /* 只发送一次版本化状态变更。 */ () =>
      expect(mutateTask).toHaveBeenCalledWith(
        'run-a',
        expect.objectContaining({ task_id: 'task-a', expected_revision: 7, status: 'cancelled' }),
        undefined,
      ),
  );
});

it('历史按固定快照查询，隐藏全部编辑和执行入口', async () => {
  show(true);
  expect(await screen.findByText('#1 实现接口')).toBeInTheDocument();
  expect(getTasks).toHaveBeenCalledWith('run-a', true);
  expect(screen.queryByRole('button', { name: '新增工作项' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '取消任务' })).not.toBeInTheDocument();
});

it('过期更新错误可见，页面不伪造完成', async () => {
  vi.mocked(mutateTask).mockRejectedValue(new Error('任务已更新，请重新读取后操作。'));
  const user = userEvent.setup();
  show();
  await user.click(await screen.findByRole('button', { name: '取消任务' }));
  expect(await screen.findByText('任务已更新，请重新读取后操作。')).toBeInTheDocument();
  expect(screen.queryByText('已完成')).not.toBeInTheDocument();
});

it('前置重开后保留旧完成证据，但当前进度不再计入待复核项', async () => {
  vi.mocked(getTasks).mockResolvedValue({
    revision: 13,
    items: [
      {
        ...item,
        status: 'completed',
        blocked_reason: '前置变更，需要复核',
        completion_summary: '旧轮检查通过',
      },
    ],
  });
  show();
  expect(await screen.findByText('任务 · 0/1')).toBeInTheDocument();
  expect(screen.getByText('前置变更，需要复核')).toBeInTheDocument();
  expect(screen.getByText('旧轮检查通过')).toBeInTheDocument();
});
