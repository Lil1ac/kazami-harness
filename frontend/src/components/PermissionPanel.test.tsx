import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { makeRun } from '../test/fixtures';
import type { PermissionAction } from '../types';
import { PermissionPanel } from './PermissionPanel';

vi.mock(
  '../api',
  /* 只替换只读HTTP请求，不自动制造批准。 */ () => ({
    getPermissions: vi.fn(async () => ({ items: [], total: 0 })),
    errorMessage: /* 固定受控错误文字。 */ () => '读取失败',
  }),
);

/** 构造一条固定命令 action；expires为UTC秒，返回值仅用于组件，不产生真实批准。 */
function action(expires = Date.now() / 1000 + 1800): PermissionAction {
  return {
    id: 'action-a',
    source_run_id: 'run-a',
    fingerprint: 'a'.repeat(64),
    name: 'run_command',
    arguments: { argv: ['python', '-m', 'pytest', '-q'] },
    inputs: { cwd: 'D:/project', executable: 'D:/Python/python.exe' },
    workspace: 'D:/project',
    created_at: Date.now() / 1000,
    expires_at: expires,
    status: 'pending',
    execution_run_id: null,
  };
}

/** 在真实查询环境展示待审批动作；historical只读，expired生成已到期快照，返回决定监听器。 */
function show({ historical = false, expired = false } = {}) {
  const run = makeRun();
  run.result.permissions.pending = action(expired ? Date.now() / 1000 - 10 : undefined);
  run.result.execution_status = 'waiting_input';
  const decide = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={cache}>
      <PermissionPanel run={run} historical={historical} onDecision={decide} />
    </QueryClientProvider>,
  );
  return decide;
}

it('展示精确参数与实际程序，只有明确点击才提交单次决定', () => {
  const decide = show();
  expect(screen.getByLabelText('命令参数')).toHaveTextContent('pytest');
  expect(screen.getByText('D:/Python/python.exe')).toBeVisible();
  expect(decide).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '批准本次' }));
  expect(decide).toHaveBeenCalledExactlyOnceWith('approve');
});

it('过期动作不能批准，但允许拒绝或取消等待', () => {
  const decide = show({ expired: true });
  expect(screen.getByRole('button', { name: '批准本次' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '取消等待' }));
  expect(decide).toHaveBeenCalledExactlyOnceWith('cancel');
});

it('历史快照不提供任何批准或拒绝按钮', () => {
  show({ historical: true });
  expect(screen.getByText('当轮快照：待批准')).toBeVisible();
  expect(screen.queryByRole('button', { name: '批准本次' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '拒绝' })).not.toBeInTheDocument();
});
