import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getRuntime, getCommandOutput, stopCommand, retractRuntimeInput } from '../api';
import type { CommandExecution, RuntimeView } from '../runtimeTypes';
import { RuntimePanel } from './RuntimePanel';

vi.mock(
  '../api',
  /* 仅替换 HTTP，分页、状态和操作仍运行真实组件。 */ () => ({
    getRuntime: vi.fn(),
    getCommandOutput: vi.fn(),
    stopCommand: vi.fn(),
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
  status: 'running',
  pid: 123,
  created_at: '',
  finished_at: null,
  cleanup_status: 'not_needed',
  result: null,
}; // 父视图里的子命令，停止必须带真实 owner。

/** 在独立查询缓存及路由中展示只读/活动面板；返回组件句柄，不请求模型。 */
function show(readOnly = false) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <RuntimePanel runId="run-a" active readOnly={readOnly} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(
  /* 每例独立固定输入和 UTF-8 字节分页，不依赖测试运行顺序。 */ () => {
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
    vi.mocked(getCommandOutput).mockImplementation(
      /* 内容两字六字节，偏移不能按展示字符数累计。 */ async (
        _run,
        _command,
        _owner,
        stream,
        offset,
      ) => ({
        command,
        output: {
          content: offset ? '' : '中文',
          offset,
          next_offset: 6,
          size_bytes: 6,
          eof: true,
          stream,
        },
      }),
    );
    vi.mocked(stopCommand).mockResolvedValue({
      command: { ...command, status: 'cancelled' },
      result: {
        exit_code: 1,
        error_code: 'command_cancelled',
        captured_bytes: 6,
        truncated: false,
      },
    });
  },
);

it('命令输出按原始字节前进，停止子命令携带原 owner 且不会误取消父运行', async () => {
  const user = userEvent.setup();
  show();
  const text = await screen.findByText('python long_test.py');
  expect(getCommandOutput).not.toHaveBeenCalled();
  const details = text.closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  await waitFor(
    /* 输出查询完成后才检查已保存文字。 */ () =>
      expect(screen.getByLabelText('命令输出')).toHaveTextContent('中文'),
  );
  await waitFor(
    /* 第二页必须使用六字节游标，不是两个汉字的字符数。 */ () =>
      expect(getCommandOutput).toHaveBeenCalledWith(
        'run-a',
        'command-a',
        'member-run',
        'stdout',
        6,
      ),
  );
  await user.click(screen.getByRole('button', { name: '停止命令' }));
  expect(stopCommand).toHaveBeenCalledWith('run-a', 'command-a', 'member-run');
  expect(screen.getByText('你 · 本轮补充 · 已接收 · 待处理')).toBeInTheDocument();
});

it('历史和归档展示证据但不提供停止或撤回入口', async () => {
  show(true);
  const text = await screen.findByText('python long_test.py');
  text.closest('details')!.open = true;
  fireEvent(text.closest('details')!, new Event('toggle'));
  await screen.findByLabelText('命令输出');
  expect(screen.queryByRole('button', { name: '停止命令' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '撤回' })).not.toBeInTheDocument();
  expect(stopCommand).not.toHaveBeenCalled();
  expect(retractRuntimeInput).not.toHaveBeenCalled();
});
