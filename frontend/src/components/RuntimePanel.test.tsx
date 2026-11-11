import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getRuntime, retractRuntimeInput } from '../api';
import type { CommandExecution, RuntimeView } from '../runtimeTypes';
import { RuntimePanel } from './RuntimePanel';

vi.mock(
  '../api',
  /* 仅替换 HTTP，分页、状态和操作仍运行真实组件。 */ () => ({
    getRuntime: vi.fn(),
    retractRuntimeInput: vi.fn(),
    dispatchNextInput: vi.fn(),
    errorMessage: /* 错误正文由固定测试异常提供。 */ (error: Error) => error.message,
  }),
);

const command: CommandExecution = {
  id: 'command-a',
  run_id: 'member-run',
  parent_run_id: 'run-a',
  member_id: 'member-a',
  call_id: 'call-a',
  model_turn: 1,
  workspace: 'D:/sample',
  argv: ['python', 'long_test.py'],
  cwd: '.',
  background: true,
  status: 'failed',
  pid: null,
  created_at: '',
  finished_at: null,
  cleanup_status: 'not_needed',
  result: {
    exit_code: 3,
    error_code: 'command_nonzero_exit',
    captured_bytes: 12,
    truncated: false,
  },
}; // 父会话摘要必须定位此失败命令的实际编号。

/** 在独立缓存展示只读或活动会话摘要，返回输出选择的记录函数。 */
function show(readOnly = false) {
  const openOutput = vi.fn();
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <RuntimePanel runId="run-a" active readOnly={readOnly} onOpenOutput={openOutput} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return openOutput;
}

beforeEach(
  /* 每例固定已接收输入和失败命令，不依赖测试运行顺序。 */ () => {
    vi.resetAllMocks();
    const view: RuntimeView = {
      active: true,
      commands: [command],
      state: {
        run_id: 'run-a',
        session_id: 'run-a',
        status: 'running',
        checkpoint: null,
        finished_at: null,
        messages: [
          {
            id: 'message-a',
            prompt: '先不要改数据库',
            delivery: 'steer',
            status: 'received',
            consumers: {},
            created_at: '',
            job_id: null,
            error: null,
          },
        ],
      },
    };
    vi.mocked(getRuntime).mockResolvedValue(view);
    vi.mocked(retractRuntimeInput).mockResolvedValue({
      ...view.state!.messages[0],
      status: 'retracted',
    });
  },
);

it('会话摘要直达失败命令，并保留补充要求的撤回操作', /* 验证同一轮的输出和消息操作。 */ async () => {
  const user = userEvent.setup();
  const openOutput = show();
  await user.click(await screen.findByRole('button', { name: '查看命令错误' }));
  expect(openOutput).toHaveBeenCalledWith('command:command-a');
  expect(screen.getByText('你 · 本轮补充 · 已接收 · 待处理')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '撤回' }));
  expect(retractRuntimeInput).toHaveBeenCalledWith('run-a', 'message-a');
});

it('历史和归档保留输出入口与输入回执，隐藏撤回操作', /* 旧消息只读。 */ async () => {
  show(true);
  expect(await screen.findByRole('button', { name: '查看命令错误' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '撤回' })).not.toBeInTheDocument();
  expect(retractRuntimeInput).not.toHaveBeenCalled();
});
