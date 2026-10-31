import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { desktopAction, getOptions } from '../api';
import { options } from '../test/fixtures';
import { DesktopIntegration } from './DesktopIntegration';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getOptions: vi.fn(),
  desktopAction: vi.fn(),
}));

beforeEach(/* 每个测试隔离宿主调用，避免历史下载事件干扰当前断言。 */ () => vi.clearAllMocks());

/** 挂载桌面能力和真实链接；desktop 决定是否启用原生接管，返回查询缓存供断言。 */
function setup(desktop: boolean) {
  const value = desktop
    ? {
        ...options,
        desktop: {
          downloads_directory: 'D:/Downloads',
          data_directory: 'D:/Harness',
          credential_source: 'electron_safe_storage' as const,
        },
      }
    : options;
  vi.mocked(getOptions).mockResolvedValue(value);
  vi.mocked(desktopAction).mockResolvedValue({
    filename: 'result.txt',
    path: 'D:/Downloads/result.txt',
  });
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  cache.setQueryData(['options'], value);
  render(
    <QueryClientProvider client={cache}>
      <DesktopIntegration />
      <a
        href="/api/runs/00000000-0000-4000-8000-000000000001/artifacts?location=workspace&path=result.txt"
        download
      >
        下载结果
      </a>
      <a href="https://example.com/docs">外部文档</a>
      <a href="/runs">内部页面</a>
      <a href="/api/projects/sample/memories/export" download>导出项目记忆</a>
    </QueryClientProvider>,
  );
  return cache;
}

it('桌面附件点击交给宿主保存，外链交给默认浏览器，内部路由保持原行为', async () => {
  setup(true);
  fireEvent.click(screen.getByText('下载结果'));
  await waitFor(
    /* 断言真实 DOM 点击只产生一次原生下载请求。 */ () =>
      expect(desktopAction).toHaveBeenCalledWith(
        'download',
        '/api/runs/00000000-0000-4000-8000-000000000001/artifacts?location=workspace&path=result.txt',
      ),
  );
  expect(await screen.findByText('已保存到 D:/Downloads/result.txt')).toBeInTheDocument();
  fireEvent.click(screen.getByText('外部文档'));
  expect(desktopAction).toHaveBeenLastCalledWith('external', 'https://example.com/docs');
  const event = new MouseEvent('click', { bubbles: true, cancelable: true });
  screen.getByText('内部页面').dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
  expect(desktopAction).toHaveBeenCalledTimes(2);
});

it('网页模式不触发原生接口', () => {
  setup(false);
  const event = new MouseEvent('click', { bubbles: true, cancelable: true });
  screen.getByText('下载结果').dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
  expect(desktopAction).not.toHaveBeenCalled();
});

it('项目记忆导出复用原生下载入口', async () => {
  setup(true);
  fireEvent.click(screen.getByText('导出项目记忆'));
  await waitFor(
    /* 实际 DOM 点击由统一附件协议承接，正文权限由宿主重新核验。 */ () =>
      expect(desktopAction).toHaveBeenCalledWith('download', '/api/projects/sample/memories/export'),
  );
  expect(desktopAction).toHaveBeenCalledTimes(1);
});
