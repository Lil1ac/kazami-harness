import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { expect, it, vi } from 'vitest';
import { getConversations } from '../api';
import { makeConversationManagement } from '../test/fixtures';
import { ConversationHistory } from './ConversationHistory';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getConversations: vi.fn(),
}));

it('项目列表默认不出现复选框，主动多选后可选，退出清空并恢复浏览；分类仍由服务端筛选', async () => {
  // 验证轻量入口保留批量选择和服务端分类的完整交互。
  vi.mocked(getConversations).mockResolvedValue({
    items: [
      {
        session_id: 'run-a',
        title: '核对接口',
        management: makeConversationManagement(),
        entry_run_id: 'run-a',
        latest_run_id: 'run-a',
        turn_count: 1,
        created_at: '2026-11-01T00:00:00Z',
        updated_at: '2026-11-01T00:00:00Z',
        source: null,
        latest: null,
        error: null,
      },
    ],
    total: 1,
    busy_run_id: null,
  });
  const user = userEvent.setup();
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <ConversationHistory projectId="workspace" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByText('核对接口');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '多选' }));
  await user.click(screen.getByRole('checkbox', { name: '选择对话：核对接口' }));
  expect(screen.getByRole('checkbox', { name: '全选当前页' })).toBeChecked();
  await user.click(screen.getByRole('button', { name: '完成多选' }));
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  await user.click(screen.getByRole('tab', { name: '已归档' }));
  await waitFor(
    /* 页面不自行伪造分类数据，查询仍绑定项目。 */ () =>
      expect(getConversations).toHaveBeenLastCalledWith(
        expect.objectContaining({ projectId: 'workspace', state: 'archived', offset: 0 }),
        expect.any(AbortSignal),
      ),
  );
});
