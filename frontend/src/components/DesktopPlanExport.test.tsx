/** 桌面下载接管的集成验收，与不依赖原生宿主的计划组件测试分别维护。 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { desktopAction } from '../api';
import { options } from '../test/fixtures';
import type { PlanningState } from '../types';
import { DesktopIntegration } from './DesktopIntegration';
import { PlanningPanel } from './PlanningPanel';
import { MemoryRouter } from 'react-router';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  desktopAction: vi.fn(),
}));

it('计划导出链接由桌面统一下载器接管，并且不发送用户批准', async () => {
  const user = userEvent.setup();
  const onDecision = vi.fn();
  const state: PlanningState = {
    phase: 'planning',
    reason: '审阅计划',
    decisions: [],
    pending: {
      id: 'question',
      kind: 'plan_approval',
      question: '请审阅',
      options: [],
      plan_version: 1,
    },
    plans: [
      {
        version: 1,
        title: '前后端交付',
        summary: '实施并验收',
        scope: ['页面与服务'],
        steps: [{ id: 'step-1', text: '联调', status: 'pending' }],
        verification: ['核对结果'],
        workspace: 'D:/project',
        workspace_identity: [1, 2],
        status: 'pending',
      },
    ],
  }; // 仍待批准的真实结构，下载不得将其当成执行决定。
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  cache.setQueryData(['options'], {
    ...options,
    desktop: {
      downloads_directory: 'D:/Downloads',
      data_directory: 'D:/Harness',
      credential_source: 'electron_safe_storage',
    },
  });
  vi.mocked(desktopAction).mockResolvedValue({ path: 'D:/Downloads/plan-v1.md' });
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <DesktopIntegration />
        <PlanningPanel
          runId="00000000-0000-4000-8000-000000000001"
          state={state}
          disabled={false}
          onDecision={onDecision}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole('link', { name: '导出计划 v1 为 Markdown' }));
  await waitFor(
    /* 验证真实 DOM 事件与宿主接管协作，保留绑定的运行和版本。 */ () =>
      expect(desktopAction).toHaveBeenCalledExactlyOnceWith(
        'download',
        '/api/runs/00000000-0000-4000-8000-000000000001/artifacts?location=run&path=plans%2Fv1.md',
      ),
  );
  expect(onDecision).not.toHaveBeenCalled();
});
