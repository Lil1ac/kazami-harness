import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { getRuntime, getCommandOutput, stopCommand } from '../api';
import type { CommandExecution } from '../runtimeTypes';
import { makeRun, makeVerification } from '../test/fixtures';
import { RunOutputPanel } from './RunOutputPanel';

vi.mock(
  '../api',
  /* 保留真实下载地址，只替换命令列表、日志与停止 HTTP。 */ async (original) => ({
    ...(await original<typeof import('../api')>()),
    getRuntime: vi.fn(),
    getCommandOutput: vi.fn(),
    stopCommand: vi.fn(),
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
}; // 子命令按归属读取和停止，不能误操作父运行。

/** 在独立缓存挂载输出页，overrides 可指定记录和可操作状态。 */
function show(overrides: Partial<React.ComponentProps<typeof RunOutputPanel>> = {}) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <RunOutputPanel
        run={makeRun()}
        active={false}
        readOnly={false}
        onSelect={vi.fn()}
        {...overrides}
      />
    </QueryClientProvider>,
  );
}

beforeEach(
  /* 默认运行没有命令；每个测试明确设定需要的实际记录。 */ () => {
    vi.resetAllMocks();
    vi.mocked(getRuntime).mockResolvedValue({ active: false, commands: [], state: null });
  },
);

it('按宿主字节前进并按真实归属停止子命令', /* 两个中文字符对应六个字节。 */ async () => {
  const user = userEvent.setup();
  vi.mocked(getRuntime).mockResolvedValue({ active: true, commands: [command], state: null });
  vi.mocked(getCommandOutput).mockImplementation(
    /* 提供独立的 UTF-8 字节游标，空的后续页不能重放之前的文本。 */ async (
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
    result: { exit_code: 1, error_code: 'command_cancelled', captured_bytes: 6, truncated: false },
  });
  show({ active: true, target: 'command:command-a' });
  expect(await screen.findByText('中文')).toBeInTheDocument();
  await waitFor(
    /* 已显示的两个汉字不能被当作下一页的字节偏移。 */ () =>
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
});

it('历史快照保留首次失败的错误流和下载，最后一次通过仍可独立选中', /* 固定旧检查。 */ async () => {
  const user = userEvent.setup();
  const onSelect = vi.fn();
  const onContinue = vi.fn();
  const failed = makeVerification('failed', {
    stdout: 'first stdout',
    stderr: 'first failed',
    artifacts: [{ location: 'run', path: 'first/stderr.bin', kind: 'stderr' }],
  });
  const passed = makeVerification('passed', { stdout: 'final passed' });
  const run = makeRun({ verification_kind: 'project_tests' });
  run.result.verification = passed;
  run.result.repair_attempts = 1;
  run.result.verification_attempts = [
    { repair_attempt: 0, result: failed },
    { repair_attempt: 1, result: passed },
  ];
  show({ run, target: 'check:0', onSelect, onContinue });
  expect(screen.getByLabelText('错误输出', { selector: 'pre' })).toHaveTextContent('first failed');
  expect(screen.getByRole('link', { name: '下载错误输出' })).toHaveAttribute(
    'href',
    expect.stringContaining('first%2Fstderr.bin'),
  );
  expect(screen.queryByRole('button', { name: '带错误继续' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('radio', { name: '标准输出' }).closest('label')!);
  expect(screen.getByLabelText('标准输出', { selector: 'pre' })).toHaveTextContent('first stdout');
  await user.click(screen.getByRole('button', { name: /第 1 次修复后项目测试/ }));
  expect(onSelect).toHaveBeenCalledWith('check:1');
  expect(getCommandOutput).not.toHaveBeenCalled();
});

it('失败命令的回执深链接只匹配根运行，确认错误流为空后显示标准输出', /* 子命令同名回执不能抢占。 */ async () => {
  const failed: CommandExecution = {
    ...command,
    id: 'command-root',
    run_id: 'run-a',
    status: 'failed',
    result: {
      exit_code: 4,
      error_code: 'command_nonzero_exit',
      captured_bytes: 12,
      truncated: false,
    },
  };
  vi.mocked(getRuntime).mockResolvedValue({
    active: false,
    commands: [command, failed],
    state: null,
  });
  vi.mocked(getCommandOutput).mockImplementation(
    /* 仅根命令的标准输出有诊断内容。 */ async (_run, _command, _owner, stream, offset) => ({
      command: failed,
      output: {
        content: stream === 'stdout' && offset === 0 ? 'stdout error' : '',
        offset,
        next_offset: stream === 'stdout' ? 12 : 0,
        size_bytes: stream === 'stdout' ? 12 : 0,
        eof: true,
        stream,
      },
    }),
  );
  show({ target: 'call:call-a', readOnly: true });
  expect(await screen.findByText('stdout error')).toBeInTheDocument();
  expect(getCommandOutput).toHaveBeenCalledWith('run-a', 'command-root', 'run-a', 'stderr', 0);
  expect(screen.queryByRole('button', { name: '停止命令' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '带错误继续' })).not.toBeInTheDocument();
});
