import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getRecovery, resumeRun } from '../api';
import { makeRun } from '../test/fixtures';
import type { RecoveryView, RunView } from '../types';
import { RecoveryPanel } from './RecoveryPanel';

vi.mock(
  '../api',
  /* 只替换 HTTP 边界，恢复存储、防重和查询行为使用真实实现。 */ () => ({
    getRecovery: vi.fn(),
    resumeRun: vi.fn(),
    errorMessage: /* 失败只显示受控 message，不展示未知对象。 */ (error: unknown) =>
      error instanceof Error ? error.message : '请求未完成',
  }),
);

/** 构造可恢复状态；overrides 只替换明确字段，检查点编号保持有效 UUID。 */
function available(overrides: Partial<RecoveryView> = {}): RecoveryView {
  return {
    supported: true,
    can_resume: true,
    blocked_code: null,
    blocked_reason: null,
    busy_run_id: null,
    checkpoint: {
      checkpoint_id: '00000000-0000-4000-8000-000000000001',
      created_at: '2026-10-22T10:00:00Z',
      event_seq: 21,
      model_turns: 3,
      tool_calls: 2,
      active_duration_seconds: 12,
      summary_calls: 1,
    },
    ...overrides,
  };
}

/** 在真实查询和路由环境挂载；返回确认回调供测试核对无乐观伪造执行。 */
function show(run: RunView = makeRun()) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onConfirmed = vi.fn();
  const rendered = render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <RecoveryPanel run={run} onConfirmed={onConfirmed} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, onConfirmed };
}

beforeEach(
  /* 不同场景不共享待确认请求或服务端响应。 */ () => {
    vi.resetAllMocks();
    sessionStorage.clear();
    vi.mocked(getRecovery).mockResolvedValue(available());
  },
);

it('读取检查点不会执行，用户明确点击才提交固定检查点', async () => {
  // 显示真实保存位置与消耗；成功前不会提前交付运行。
  const user = userEvent.setup();
  vi.mocked(resumeRun).mockResolvedValue(makeRun({ run_id: 'run-b', attempt_index: 2 }));
  const host = show();
  expect(await screen.findByText('事件 #21')).toBeInTheDocument();
  expect(resumeRun).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(
    /* 仅 POST 确认后的真实运行进入会话。 */ () =>
      expect(host.onConfirmed).toHaveBeenCalledTimes(1),
  );
  expect(resumeRun).toHaveBeenCalledWith('run-a', {
    request_id: expect.stringMatching(/^[a-f0-9-]{36}$/),
    checkpoint_id: available().checkpoint!.checkpoint_id,
  });
  expect(sessionStorage.getItem('harness-resume:run-a')).toBeNull();
});

it('未知响应刷新后不自动 POST，显式确认沿用相同编号与检查点', async () => {
  // 后续 GET 即使发现占槽或不同检查点，确认原请求也不能生成另一任务。
  const user = userEvent.setup();
  vi.mocked(resumeRun).mockRejectedValueOnce(new Error('连接中断'));
  const first = show();
  await user.click(await screen.findByRole('button', { name: '恢复任务' }));
  expect(await screen.findByText('连接中断')).toBeInTheDocument();
  const original = vi.mocked(resumeRun).mock.calls[0];
  expect(sessionStorage.getItem('harness-resume:run-a')).not.toBeNull();
  first.unmount();
  vi.mocked(getRecovery).mockResolvedValue(
    available({
      can_resume: false,
      blocked_code: 'run_busy',
      blocked_reason: '原请求可能已被接纳。',
    }),
  );
  vi.mocked(resumeRun).mockResolvedValue(makeRun({ run_id: 'run-b' }));
  const second = show();
  const confirm = await screen.findByRole('button', { name: '确认原恢复请求' });
  expect(resumeRun).toHaveBeenCalledTimes(1);
  await user.click(confirm);
  await waitFor(
    /* 刷新后只在再次明确点击才确认一次原请求。 */ () =>
      expect(second.onConfirmed).toHaveBeenCalledTimes(1),
  );
  expect(vi.mocked(resumeRun).mock.calls[1]).toEqual(original);
});

it('明确拒绝展示阻断原因并释放请求，不自动绕过工作区核验', async () => {
  // 409 已知拒绝与未知网络结果不同，用户修复条件之前不可再次提交。
  const user = userEvent.setup();
  vi.mocked(resumeRun).mockRejectedValue(
    Object.assign(new Error('工作树内容已发生变化。'), { status: 409, code: 'workspace_changed' }),
  );
  show();
  await screen.findByRole('button', { name: '恢复任务' });
  vi.mocked(getRecovery).mockResolvedValue(
    available({
      can_resume: false,
      blocked_code: 'workspace_changed',
      blocked_reason: '请先核对原工作树。',
    }),
  );
  await user.click(screen.getByRole('button', { name: '恢复任务' }));
  expect(await screen.findByText('工作树内容已发生变化。')).toBeInTheDocument();
  expect(await screen.findByText('请先核对原工作树。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '恢复任务' })).toBeDisabled();
  expect(sessionStorage.getItem('harness-resume:run-a')).toBeNull();
  expect(resumeRun).toHaveBeenCalledTimes(1);
});

it('普通文件任务明确首版范围，无检查点的旧 Git 记录保留阻断说明', async () => {
  // 不把所有失败运行伪装成可恢复，也不创建检查点。
  vi.mocked(getRecovery).mockResolvedValue(
    available({
      supported: false,
      can_resume: false,
      checkpoint: null,
      blocked_code: 'project_required',
      blocked_reason: '首版仅支持 Git 代码任务恢复。',
    }),
  );
  const first = show();
  expect(await screen.findByText('首版仅支持 Git 代码任务恢复。')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '恢复任务' })).not.toBeInTheDocument();
  first.unmount();
  vi.mocked(getRecovery).mockResolvedValue(
    available({
      can_resume: false,
      checkpoint: null,
      blocked_code: 'checkpoint_missing',
      blocked_reason: '此运行没有已保存的安全检查点。',
    }),
  );
  show();
  expect(await screen.findByText('此运行没有已保存的安全检查点。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '恢复任务' })).toBeDisabled();
  expect(resumeRun).not.toHaveBeenCalled();
});

it('运行槽忙碌或来源仍活动时禁用新恢复', async () => {
  // 读取占槽状态不自动取消其他运行，也不发起恢复。
  vi.mocked(getRecovery).mockResolvedValue(
    available({
      can_resume: false,
      busy_run_id: 'other-run',
      blocked_code: 'run_busy',
      blocked_reason: '另一个运行正在执行。',
    }),
  );
  show(makeRun({ active: true }));
  expect(await screen.findByText('另一个运行正在执行。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '恢复任务' })).toBeDisabled();
  expect(resumeRun).not.toHaveBeenCalled();
});

it('连续点击只发送一次，未收到确认时不交付虚构恢复结果', async () => {
  // 保持 HTTP 未决以覆盖同一事件循环的按钮防重。
  vi.mocked(resumeRun).mockReturnValue(
    new Promise(/* 本场景仅观察未决请求，不制造服务确认。 */ () => undefined),
  );
  const host = show();
  const button = await screen.findByRole('button', { name: '恢复任务' });
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(/* 只能接纳一个请求。 */ () => expect(resumeRun).toHaveBeenCalledTimes(1));
  expect(host.onConfirmed).not.toHaveBeenCalled();
});

it('累计预算加上当前尝试，并明确未知 token 与停机时间语义', async () => {
  // 前序预算不是本次结果，展示累计时不能漏加当前模型和工具消耗。
  const run = makeRun({
    attempt_index: 2,
    resume_source: {
      source_run_id: 'source-run',
      checkpoint_id: available().checkpoint!.checkpoint_id,
      checkpoint_sha256: 'a'.repeat(64),
    },
    context: {
      instruction: {
        status: 'missing',
        path: 'AGENTS.md',
        content: null,
        sha256: null,
        size_bytes: null,
        error_code: null,
      },
      serialization: 'json_utf8_v1',
      last_check: null,
      summary_calls: 1,
    },
  });
  run.result = {
    ...run.result,
    model_turns: 2,
    tool_calls: 1,
    http_attempts: 2,
    active_duration_seconds: 4,
    resume_budget: {
      model_turns: 3,
      tool_calls: 2,
      http_attempts: 4,
      summary_calls: 1,
      prompt_tokens: 40,
      completion_tokens: 20,
      total_tokens: 60,
      usage_complete: false,
      active_duration_seconds: 12,
    },
  };
  show(run);
  expect(await screen.findByText('5 / 20')).toBeInTheDocument();
  expect(screen.getByText('3 / 40')).toBeInTheDocument();
  expect(screen.getByText('6 · 2')).toBeInTheDocument();
  expect(screen.getByText('70（用量不完整）')).toBeInTheDocument();
  expect(screen.getByText('16.0 秒 / 300 秒')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '原执行检查点' })).toHaveAttribute(
    'href',
    '/runs/source-run',
  );
});
