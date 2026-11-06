import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { desktopAction, getOptions, readAutomationNotification } from '../api';
import { options } from '../test/fixtures';
import { DesktopIntegration } from './DesktopIntegration';
import { MemoryRouter, useLocation } from 'react-router';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getOptions: vi.fn(),
  desktopAction: vi.fn(),
  readAutomationNotification: vi.fn(),
}));

beforeEach(
  /* 每个测试隔离宿主调用与原生订阅，避免历史事件干扰当前断言。 */ () => {
    vi.clearAllMocks();
    delete window.agentHarnessDesktop;
  },
);

/** 展示当前路由以核对通知实际导航，不用回调调用次数代替界面结果。 */
function CurrentLocation() {
  return <output data-testid="desktop-location">{useLocation().pathname}</output>;
}

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
      <MemoryRouter>
        <CurrentLocation />
        <DesktopIntegration />
        <a
          href="/api/runs/00000000-0000-4000-8000-000000000001/artifacts?location=workspace&path=result.txt"
          download
        >
          下载结果
        </a>
        <a href="https://example.com/docs">外部文档</a>
        <a href="/runs">内部页面</a>
        <a href="/api/projects/sample/memories/export" download>
          导出项目记忆
        </a>
      </MemoryRouter>
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
      expect(desktopAction).toHaveBeenCalledWith(
        'download',
        '/api/projects/sample/memories/export',
      ),
  );
  expect(desktopAction).toHaveBeenCalledTimes(1);
});

it('桌面通知点击标记已读并进入合并后的最新作业，不重新启动任务', async () => {
  let clicked: ((value: { id: string; jobId: string | null }) => void) | undefined; // 当前原生桥订阅。
  window.agentHarnessDesktop = {
    quit: vi.fn(),
    onServiceError: /* 此例不模拟服务失联，保留正常取消订阅契约。 */ () => () => undefined,
    onAutomationNotification: /* 保存受信回调，测试模拟一次操作系统通知点击。 */ (callback) => {
      clicked = callback;
      return /* 卸载时不再接收旧通知。 */ () => {
        clicked = undefined;
      };
    },
  };
  vi.mocked(readAutomationNotification).mockResolvedValue({
    id: 'notification',
    rule_id: 'rule',
    name: '巡检',
    state: 'waiting_input',
    message: '需要处理',
    read: true,
    count: 1,
    job_id: 'latest-job',
    run_id: null,
  });
  setup(true);
  await waitFor(/* 订阅完成后才模拟用户点击。 */ () => expect(clicked).toBeDefined());
  act(
    /* 通知仅携带记录编号，不提供可执行命令或任意URL。 */ () =>
      clicked!({ id: 'notification', jobId: 'original-job' }),
  );
  await waitFor(
    /* 原弹窗属于旧故障，但收件箱已合并到最新待处理项。 */ () =>
      expect(screen.getByTestId('desktop-location')).toHaveTextContent('/jobs/latest-job'),
  );
  expect(readAutomationNotification).toHaveBeenCalledWith('notification');
});
