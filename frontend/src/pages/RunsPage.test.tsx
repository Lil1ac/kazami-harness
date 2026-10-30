import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getOptions, getConversations } from '../api';
import { ConfigDraftProvider } from '../config';
import { options } from '../test/fixtures';
import { RunsPage } from './RunsPage';

vi.mock(
  '../api',
  /* 页面读取与执行均受控，不弹系统窗口或调用模型。 */ () => ({
    getOptions: vi.fn(),
    getConversations: vi.fn(),
    createRun: vi.fn(),
    createProject: vi.fn(),
    openProject: vi.fn(),
    pickProject: vi.fn(),
    saveProjectSettings: vi.fn(),
    errorMessage: (error: unknown) => (error instanceof Error ? error.message : '读取失败'),
  }),
);

beforeEach(
  /* 独立测试不继承其他页面的选择或未发送文字。 */ () => {
    sessionStorage.clear();
    localStorage.clear();
    vi.resetAllMocks();
  },
);

it('后台项目刷新失败时保留现有表单与草稿，恢复后继续使用原输入节点', async () => {
  // 捕捉网络错误导致整张表单被卸载的问题，同时验证恢复不会创建空白输入。
  vi.mocked(getOptions).mockResolvedValue({ ...options, recent_projects: [] });
  vi.mocked(getConversations).mockResolvedValue({ items: [], total: 0, busy_run_id: null });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter>
          <RunsPage />
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  const prompt = await screen.findByLabelText('任务描述');
  await user.type(prompt, '保留未发送要求');
  vi.mocked(getOptions).mockRejectedValue(new Error('服务暂时不可达'));
  await user.click(screen.getByRole('button', { name: '刷新工作台' }));
  expect(await screen.findByText('项目列表刷新失败，已保留当前输入')).toBeInTheDocument();
  expect(screen.getByLabelText('任务描述')).toBe(prompt);
  expect(prompt).toHaveValue('保留未发送要求');
  vi.mocked(getOptions).mockResolvedValue({ ...options, recent_projects: [] });
  await user.click(screen.getByRole('button', { name: '刷新工作台' }));
  await waitFor(
    /* 恢复后移除错误，不卸载表单。 */ () =>
      expect(screen.queryByText('服务暂时不可达')).not.toBeInTheDocument(),
  );
  expect(screen.getByLabelText('任务描述')).toBe(prompt);
  expect(prompt).toHaveValue('保留未发送要求');
});

it('全部会话页的刷新操作重新读取当前会话页，而非只读取首页最近列表', async () => {
  // 全局历史与首页最近列表使用不同分页键，刷新必须使当前活动查询失效。
  vi.mocked(getOptions).mockResolvedValue({ ...options, recent_projects: [] });
  vi.mocked(getConversations).mockResolvedValue({ items: [], total: 0, busy_run_id: null });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter initialEntries={['/runs?view=history']}>
          <RunsPage />
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  await screen.findByRole('region', { name: '全部会话列表' });
  await waitFor(
    /* 确认初始历史页读取完成。 */ () =>
      expect(getConversations).toHaveBeenCalledWith(
        expect.objectContaining({ limit: 12 }),
        expect.anything(),
      ),
  );
  const before = vi
    .mocked(getConversations)
    .mock.calls.filter(/* 只统计当前完整历史页。 */ (call) => call[0]?.limit === 12).length;
  await userEvent.setup().click(screen.getByRole('button', { name: '刷新工作台' }));
  await waitFor(
    /* 刷新至少产生一次新的当前页读取。 */ () =>
      expect(
        vi.mocked(getConversations).mock.calls.filter((call) => call[0]?.limit === 12).length,
      ).toBeGreaterThan(before),
  );
});
