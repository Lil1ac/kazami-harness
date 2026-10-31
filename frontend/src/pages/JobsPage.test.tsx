import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getJobs, jobAction, resumeQueue } from '../api';
import { makeJob } from '../test/fixtures';
import { JobsPage } from './JobsPage';

vi.mock(
  '../api',
  /* HTTP 替身只提供持久作业快照，页面不调用模型。 */ () => ({
    getJobs: vi.fn(),
    getJob: vi.fn(),
    jobAction: vi.fn(),
    resumeQueue: vi.fn(),
    errorMessage: (error: Error) => error.message,
  }),
);

/** 挂载后台中心；返回真实 React 界面，不提供执行端点。 */
function show() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <JobsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(
  /* 每个用例使用独立队列快照。 */ () => {
    vi.resetAllMocks();
    vi.mocked(getJobs).mockResolvedValue({
      paused: true,
      items: [
        {
          ...makeJob(),
          id: 'job-a',
          title: '实现 CSV 导入',
          status: 'queued',
          execution_status: null,
        },
      ],
    });
  },
);

it('排队项保持可停止，不提供不存在的 Run 链接', async () => {
  const user = userEvent.setup();
  show();
  expect(await screen.findByText('排队中')).toBeInTheDocument();
  expect(screen.getByText(makeJob().project_label)).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: '查看会话与结果' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '停止' }));
  await waitFor(
    /* 取消仅指向已接纳编号。 */ () => expect(jobAction).toHaveBeenCalledWith('job-a', 'cancel'),
  );
});

it('暂停队列只在用户明确点击时恢复', async () => {
  const user = userEvent.setup();
  show();
  await screen.findByText('队列已暂停');
  expect(resumeQueue).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '恢复队列' }));
  await waitFor(/* 不因刷新重复恢复。 */ () => expect(resumeQueue).toHaveBeenCalledOnce());
});
