import { act, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { getRun } from '../api';
import { makeRun } from '../test/fixtures';
import { RunDetailPage } from './RunDetailPage';

vi.mock(
  '../api',
  /* 隔离后端读取；取消接口仍可用于检查未被意外调用。 */ () => ({
    getRun: vi.fn(),
    cancelRun: vi.fn(),
    errorMessage: (error: unknown) => (error instanceof Error ? error.message : '读取失败'),
  }),
);
vi.mock(
  '../hooks/useRunEvents',
  /* 本用例聚焦快照缓存，订阅生命周期由独立 hook 测试覆盖。 */ () => ({
    useRunEvents: () => ({
      events: [],
      connection: 'closed',
      error: null,
      loadingOlder: false,
      loadOlder: vi.fn(),
      canLoadOlder: false,
    }),
  }),
);

it('后台刷新失败时保留上次任务快照，恢复后可以继续查看', async () => {
  // 成功快照是已知事实，短暂网络故障不能把已有任务和轨迹移除。
  vi.mocked(getRun)
    .mockResolvedValueOnce(makeRun())
    .mockRejectedValueOnce(new Error('本地服务暂时断开'));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/runs/run-a']}>
        <Routes>
          <Route path="/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText('统计日志')).toBeInTheDocument();
  await act(async () => /* 模拟窗口重新聚焦或轮询时的刷新失败。 */ {
    await client.invalidateQueries({ queryKey: ['run', 'run-a'] });
  });
  expect(await screen.findByText('暂时无法刷新任务，保留上次快照')).toBeInTheDocument();
  expect(screen.getByText('统计日志')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: /运行详情/ })).toBeInTheDocument();
});
