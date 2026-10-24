import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { getDiff } from '../api';
import { LocalChangesPanel } from './LocalChangesPanel';
import type { LocalChangeView } from '../types';

vi.mock(
  '../api',
  /* 模拟只读HTTP边界，组件与查询缓存真实挂载。 */ () => ({
    getDiff: vi.fn(),
    patchUrl: (id: string) => '/api/runs/' + id + '/patch',
    errorMessage: (error: Error) => error.message,
  }),
);
beforeEach(/* 不让差异结果或调用计数跨用例共享。 */ () => vi.resetAllMocks());

/** 为指定差异状态挂载组件；默认使用终态，返回值不创建任务或审阅请求。 */
function setup(changes: LocalChangeView) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LocalChangesPanel
        runId="saved-round"
        workspace="D:/自己的项目"
        changes={changes}
        active={false}
      />
    </QueryClientProvider>,
  );
}

it('原目录差异读取指定轮的固定记录，显示实际目录且没有丢弃原目录操作', async () => {
  vi.mocked(getDiff).mockResolvedValue({
    content: '--- a/hello.py\n+++ b/hello.py\n+print("hello")',
    truncated: false,
    size_bytes: 60,
  });
  setup({
    status: 'ready',
    patch_available: true,
    warnings: [],
    files: [{ path: 'hello.py', status: 'added', binary: false }],
  });
  expect(screen.getByText('修改已在原目录生效。')).toBeInTheDocument();
  expect(screen.getByText('D:/自己的项目')).toBeInTheDocument();
  expect(await screen.findByLabelText('代码差异预览')).toHaveTextContent('print("hello")');
  expect(getDiff).toHaveBeenCalledWith('saved-round', expect.anything());
  expect(screen.getByRole('link', { name: '下载本轮差异' })).toHaveAttribute(
    'href',
    '/api/runs/saved-round/patch',
  );
  expect(screen.queryByRole('button', { name: /丢弃|保留/ })).not.toBeInTheDocument();
});

it('采集失败明确提示，不显示没有变化或请求不存在的补丁', () => {
  setup({
    status: 'error',
    patch_available: false,
    warnings: ['本轮文件超过采集上限，当前目录仍可继续。'],
    files: [],
  });
  expect(screen.getByText('本轮文件超过采集上限，当前目录仍可继续。')).toBeInTheDocument();
  expect(getDiff).not.toHaveBeenCalled();
  expect(screen.queryByText('本轮没有文件变化')).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: '下载本轮差异' })).not.toBeInTheDocument();
});

it('差异刚固化但后台尚未释放时等待终态，再从同一查询身份读取成功', async () => {
  // 接口在活动期返回409；提前读取会把错误留在同一轮差异查询中。
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const changes: LocalChangeView = {
    status: 'ready',
    patch_available: true,
    warnings: [],
    files: [],
  };
  vi.mocked(getDiff).mockResolvedValue({
    content: 'saved current round',
    truncated: false,
    size_bytes: 19,
  });
  /** 保留同一查询客户端与运行编号，只切换真实收尾状态。 */
  function panel(active: boolean) {
    return (
      <QueryClientProvider client={client}>
        <LocalChangesPanel
          runId="finishing-round"
          workspace="D:/自己的项目"
          changes={changes}
          active={active}
        />
      </QueryClientProvider>
    );
  }
  const view = render(panel(true));
  expect(getDiff).not.toHaveBeenCalled();
  view.rerender(panel(false));
  expect(await screen.findByLabelText('代码差异预览')).toHaveTextContent('saved current round');
  expect(getDiff).toHaveBeenCalledOnce();
  expect(getDiff).toHaveBeenCalledWith('finishing-round', expect.anything());
});
