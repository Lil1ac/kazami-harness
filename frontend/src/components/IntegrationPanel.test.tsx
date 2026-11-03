import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import {
  getIntegrationFile,
  getIntegrations,
  getIntegrationTargets,
  integrationAction,
  resolveIntegration,
} from '../api';
import type { IntegrationView } from '../types';
import { IntegrationPanel } from './IntegrationPanel';

vi.mock(
  '../api',
  /* 只替换HTTP传输，保留真实缓存、确认和用户操作。 */ () => ({
    createIntegration: vi.fn(),
    getIntegrationFile: vi.fn(),
    getIntegrations: vi.fn(),
    getIntegrationTargets: vi.fn(),
    integrationAction: vi.fn(),
    resolveIntegration: vi.fn(),
    errorMessage: /* 后端受控文案原样展示。 */ (error: unknown) =>
      error instanceof Error ? error.message : '失败',
  }),
);

/** 生成可审阅的固定集成记录，patch覆盖具体场景，返回独立对象。 */
function record(patch: Partial<IntegrationView> = {}): IntegrationView {
  return {
    id: 'integration-a',
    run_id: 'run-a',
    session_id: 'session-a',
    target_id: 'project-a',
    revision: 2,
    status: 'ready',
    kind: 'apply',
    error: null,
    fingerprint: 'a'.repeat(64),
    candidate_fingerprint: 'd',
    target_label: '目标项目',
    target: {
      path: 'D:\\project',
      repository: 'D:\\project\\.git',
      branch: 'refs/heads/main',
      head: 'b'.repeat(40),
      fingerprint: 'c'.repeat(64),
    },
    conflicts: [],
    files: [{ path: 'app.txt', status: 'modified', binary: false }],
    test_config: { argv: [], timeout_seconds: 10, python_executable: null, python_paths: null },
    checks: [],
    applied_at: null,
    created_at: '2026-11-03T01:00:00Z',
    updated_at: '2026-11-03T01:00:00Z',
    ...patch,
  };
}

/** 在真实查询缓存与路由中展示value；返回用户操作器，不自动提交任何决定。 */
function setup(value = record()) {
  vi.mocked(getIntegrations).mockResolvedValue({ items: [value] });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <IntegrationPanel runId="run-a" sourceTest="passed" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return userEvent.setup();
}

beforeEach(
  /* 每个场景使用独立固定响应，禁止前一条操作污染请求编号和测试事实。 */ () => {
    vi.clearAllMocks();
    vi.mocked(getIntegrationTargets).mockResolvedValue({
      items: [
        {
          id: 'project-a',
          label: '项目',
          path: 'D:\\project',
          repository: 'D:\\project\\.git',
          branch: 'refs/heads/main',
          head: 'b'.repeat(40),
        },
      ],
    });
    vi.mocked(getIntegrationFile).mockResolvedValue({
      base: 'base',
      target: 'target',
      source: 'source',
      candidate: 'merged',
      diff: '-target\n+merged\n',
    });
    vi.mocked(integrationAction).mockResolvedValue(record({ status: 'applied' }));
    vi.mocked(resolveIntegration).mockResolvedValue(record({ revision: 3 }));
  },
);

it('审阅最终差异并确认前不写回，确认绑定当前版本和目标', async () => {
  const user = setup();
  await user.click(await screen.findByRole('button', { name: 'app.txt' }));
  expect(await screen.findByLabelText('最终集成差异')).toHaveTextContent('+merged');
  await user.click(screen.getByRole('button', { name: '审阅并确认采纳' }));
  expect(integrationAction).not.toHaveBeenCalled();
  expect(screen.getByRole('dialog')).toHaveTextContent('D:\\project');
  await user.click(screen.getByRole('button', { name: '确认写回' }));
  await waitFor(
    /* 只有明确确认才提交当前差异。 */ () =>
      expect(integrationAction).toHaveBeenCalledWith(
        expect.objectContaining({ revision: 2, fingerprint: 'a'.repeat(64) }),
        'apply',
        expect.any(String),
        undefined,
      ),
  );
});

it('候选检查失败不能采纳，来源通过不能代替候选通过', async () => {
  setup(
    record({
      test_config: {
        argv: ['python', 'check.py'],
        timeout_seconds: 10,
        python_executable: null,
        python_paths: null,
      },
      checks: [
        {
          scope: 'candidate',
          code_fingerprint: 'd',
          applicable: true,
          created_at: '',
          result: { status: 'failed', reason: 'assertion_failed', stdout: 'failed', stderr: '' },
        },
      ],
    }),
  );
  expect(await screen.findByRole('button', { name: '审阅并确认采纳' })).toBeDisabled();
  expect(screen.getByText(/来源运行测试：通过/)).toBeInTheDocument();
  expect(screen.getByText(/集成候选检查 · 失败/)).toBeInTheDocument();
});

it('冲突先编辑候选，保存正文不直接写目标', async () => {
  const user = setup(record({ status: 'conflicts', conflicts: ['app.txt'] }));
  await user.click(await screen.findByRole('button', { name: /app.txt · 冲突/ }));
  expect(await screen.findByText('目标当前内容')).toBeInTheDocument();
  await user.type(screen.getByLabelText('冲突解决正文'), 'chosen text');
  await user.click(screen.getByRole('button', { name: '保存编辑结果' }));
  await waitFor(
    /* 保存的是明确文本版本，不是隐式批准。 */ () =>
      expect(resolveIntegration).toHaveBeenCalledWith(
        expect.anything(),
        'app.txt',
        'edit',
        'chosen text',
        expect.any(String),
      ),
  );
  expect(integrationAction).not.toHaveBeenCalled();
});

it('写回响应丢失后重试沿用原请求编号', async () => {
  const user = setup();
  vi.mocked(integrationAction)
    .mockRejectedValueOnce(new Error('连接中断'))
    .mockResolvedValueOnce(record({ status: 'applied' }));
  await user.click(await screen.findByRole('button', { name: '审阅并确认采纳' }));
  await user.click(screen.getByRole('button', { name: '确认写回' }));
  expect(await screen.findByText('连接中断')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '确认写回' }));
  await waitFor(
    /* 同内容重试必须落到同一后台作业。 */ () => expect(integrationAction).toHaveBeenCalledTimes(2),
  );
  expect(vi.mocked(integrationAction).mock.calls[0][2]).toBe(
    vi.mocked(integrationAction).mock.calls[1][2],
  );
});

it('检查进程尚待核对时不能批准新检查或写回', async () => {
  setup(
    record({
      last_job: {
        id: 'lost-check',
        operation: 'check_candidate',
        status: 'needs_review',
        recovery_kind: 'process',
        error: '请先核对检查进程',
      },
    }),
  );
  expect(await screen.findByRole('button', { name: '审阅并确认采纳' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '批准运行候选检查' })).toBeDisabled();
  expect(screen.getByRole('link', { name: '后台工作' })).toHaveAttribute('href', '/jobs');
  expect(integrationAction).not.toHaveBeenCalled();
});
