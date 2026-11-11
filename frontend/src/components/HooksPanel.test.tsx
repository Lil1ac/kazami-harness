import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { getHooks, reviewHook } from '../api';
import { HookSettings, RunHooks } from './HooksPanel';
import type { HookCatalog, HookRecord } from '../hookTypes';
import type { RunEvent } from '../types';

vi.mock(
  '../api',
  /* 本文件只替换 Hooks 请求，其余导出保留真实纯函数。 */ async (original) => ({
    ...(await original<typeof import('../api')>()),
    getHooks: vi.fn(),
    reviewHook: vi.fn(),
  }),
);

const catalog: HookCatalog = {
  config_path: 'D:/project/.agents/hooks.toml',
  items: [
    {
      key: 'project:format',
      fingerprint: 'a'.repeat(64),
      trusted: false,
      enabled: true,
      workspace: 'D:/project',
      cwd: 'D:/project',
      scripts: { 'D:/project/hook.py': 'b'.repeat(64) },
      script_previews: { 'D:/project/hook.py': { text: 'print("{}")', truncated: false } },
      previous_definition: null,
      rule: {
        id: 'format',
        event: 'PostToolUse',
        argv: ['python', 'hook.py'],
        cwd: '.',
        tools: ['write_file'],
        paths: [],
        agent_scopes: ['root'],
        timeout_seconds: 10,
        on_failure: 'continue',
        required: false,
        enabled: true,
      },
    },
  ],
}; // 可审阅的精确定义。

beforeEach(
  /* 每例独立重置请求和返回值，避免审核状态跨例泄漏。 */ () => {
    vi.clearAllMocks();
    vi.mocked(getHooks).mockResolvedValue(catalog);
  },
);

/** 在独立查询缓存中挂载设置；返回真实用户驱动器，不启动网络和脚本。 */
function setup() {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <HookSettings projectId="sample" />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

it('查看源码后审核精确定义，审核不等于运行脚本', async () => {
  const user = setup();
  await screen.findByText('待审核');
  expect(reviewHook).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'format' }));
  expect(screen.getByText('print("{}")')).toBeVisible();
  vi.mocked(reviewHook).mockResolvedValue({
    ...catalog,
    items: [{ ...catalog.items[0], trusted: true }],
  });
  await user.click(screen.getByRole('button', { name: '信任此定义' }));
  await screen.findByText('已审核 · 已启用');
  expect(reviewHook).toHaveBeenCalledWith('sample', 'project:format', 'a'.repeat(64), 'trust');
});

it('定义变化拒绝审核时保留待审核状态与受控错误', async () => {
  const user = setup();
  await screen.findByText('待审核');
  await user.click(screen.getByRole('button', { name: 'format' }));
  vi.mocked(reviewHook).mockRejectedValue(new Error('定义已变化，请重新加载'));
  await user.click(screen.getByRole('button', { name: '信任此定义' }));
  await screen.findByText('定义已变化，请重新加载');
  expect(screen.getByText('待审核')).toBeVisible();
});

it('历史终态不会被事件窗口中的旧开始记录覆盖', () => {
  const record: HookRecord = {
    id: 'hook-one',
    key: 'project:format',
    event: 'PostToolUse',
    fingerprint: 'a'.repeat(64),
    status: 'completed',
    seconds: 0.3,
  };
  const event = {
    type: 'hook_record',
    payload: { hook: { ...record, status: 'running' } },
  } as unknown as RunEvent;
  render(<RunHooks snapshot={{ records: [record] }} events={[event]} />);
  expect(screen.getByText(/已完成/)).toBeInTheDocument();
  expect(screen.queryByText(/执行中/)).not.toBeInTheDocument();
});
