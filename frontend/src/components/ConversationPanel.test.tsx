import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { createFollowup, getConversation, getDiff, reviewRun } from '../api';
import { ConfigDraftProvider } from '../config';
import { makeProject, makeRun } from '../test/fixtures';
import type { ConversationView, RunView } from '../types';
import { ConversationPanel } from './ConversationPanel';
import { ProjectReviewPanel } from './ProjectReviewPanel';

vi.mock(
  '../api',
  /* 只模拟HTTP边界，组件、缓存、输入和幂等状态使用真实实现。 */ () => ({
    getConversation: vi.fn(),
    createFollowup: vi.fn(),
    getEvents: vi.fn(),
    getDiff: vi.fn(),
    getProjectReview: vi.fn(),
    reviewRun: vi.fn(),
    patchUrl: /* 固定附件地址，不触发下载。 */ (id: string) => `/api/runs/${id}/patch`,
    errorMessage: /* 不回显不可信对象。 */ (error: unknown) =>
      error instanceof Error ? error.message : '请求失败',
  }),
);

/** 构造会话响应；覆盖项按字段替换，默认只有一个可继续的可信终态。 */
function conversation(overrides: Partial<ConversationView> = {}): ConversationView {
  return {
    session_id: 'run-a',
    latest_run_id: 'run-a',
    can_continue: true,
    blocked_code: null,
    blocked_reason: null,
    busy_run_id: null,
    turns: [
      {
        run_id: 'run-a',
        parent_run_id: null,
        turn_index: 1,
        prompt: '第一次修复',
        final_answer: '修复已完成。',
        execution_status: 'completed',
        validation_status: 'passed',
        created_at: null,
      },
    ],
    ...overrides,
  };
}

/** 在真实查询、配置与路由环境展示；review为true时同时挂载代码审阅以验证联动。 */
function show(run = makeRun(), review = false) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onConfirmed = vi.fn();
  const onInspect = vi.fn();
  cache.setQueryData(['run', run.run_id], run);
  /** 订阅实际会话与运行缓存；审阅成功后由现有失效机制重新查询可继续条件。 */
  function Host() {
    const view = useQuery({
      queryKey: ['conversation', 'run-a'],
      queryFn: /* 只读会话，不自动发送。 */ () => getConversation('run-a'),
    }).data;
    const current = useQuery<RunView>({
      queryKey: ['run', run.run_id],
      enabled: false,
      queryFn: /* 查询禁用，只订阅审阅更新。 */ async () => run,
    }).data!;
    if (!view) return null;
    return (
      <>
        <ConversationPanel
          run={current}
          conversation={view}
          events={[]}
          onInspect={onInspect}
          onConfirmed={onConfirmed}
        />
        {review && <ProjectReviewPanel project={current.project!} active={current.active} />}
      </>
    );
  }
  const rendered = render(
    <QueryClientProvider client={cache}>
      <ConfigDraftProvider>
        <MemoryRouter>
          <Host />
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  return { ...rendered, onConfirmed, onInspect, cache };
}

beforeEach(
  /* 每个场景独立草稿与HTTP响应，禁止测试间复用未确认编号。 */ () => {
    vi.resetAllMocks();
    sessionStorage.clear();
    vi.mocked(getConversation).mockResolvedValue(conversation());
    vi.mocked(getDiff).mockResolvedValue({ content: '+fixed', truncated: false, size_bytes: 6 });
  },
);

it('发送实际载荷，服务确认前显示发送中，确认后交付真实新轮', async () => {
  // pending用户消息不会被伪装成真实Agent回答。
  const user = userEvent.setup();
  let accept!: (run: RunView) => void;
  vi.mocked(createFollowup).mockReturnValue(
    new Promise(
      /* 测试控制HTTP确认时机。 */ (resolve) => {
        accept = resolve;
      },
    ),
  );
  const host = show();
  await screen.findByText('修复已完成。');
  await user.type(screen.getByLabelText('输入消息'), '补充类型提示');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByText('你 · 发送中')).toBeInTheDocument();
  expect(host.onConfirmed).not.toHaveBeenCalled();
  expect(createFollowup).toHaveBeenCalledWith('run-a', {
    request_id: expect.stringMatching(/^[a-f0-9-]{36}$/),
    prompt: '补充类型提示',
    config: {},
  });
  accept(makeRun({ run_id: 'run-b' }));
  await waitFor(
    /* 只有服务确认后才交付新运行。 */ () =>
      expect(host.onConfirmed).toHaveBeenCalledWith(expect.objectContaining({ run_id: 'run-b' })),
  );
  expect(screen.getByLabelText('输入消息')).toHaveValue('');
});

it('网络失败重试固定父轮、编号与原载荷，发送期间新写的草稿保留', async () => {
  // 即使用户改写了输入，重试确认也只能确认原请求。
  const user = userEvent.setup();
  vi.mocked(createFollowup)
    .mockRejectedValueOnce(new Error('网络断开'))
    .mockResolvedValueOnce(makeRun({ run_id: 'run-b' }));
  const host = show();
  await screen.findByText('修复已完成。');
  await user.type(screen.getByLabelText('输入消息'), '原始要求');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByText('网络断开')).toBeInTheDocument();
  const first = vi.mocked(createFollowup).mock.calls[0];
  await user.clear(screen.getByLabelText('输入消息'));
  await user.type(screen.getByLabelText('输入消息'), '下一条草稿');
  await user.click(screen.getByRole('button', { name: '重试确认' }));
  await waitFor(
    /* 重试成功后交付一次确认结果。 */ () => expect(host.onConfirmed).toHaveBeenCalledTimes(1),
  );
  expect(vi.mocked(createFollowup).mock.calls[1]).toEqual(first);
  expect(screen.getByLabelText('输入消息')).toHaveValue('下一条草稿');
});

it('兼容旧版未确认请求，刷新恢复原配置且不自动提交', async () => {
  // 旧键迁移不能生成新请求编号。
  const user = userEvent.setup();
  const body = {
    request_id: '00000000-0000-4000-8000-000000000001',
    prompt: '恢复后继续',
    config: { limits: { max_model_turns: 7 } },
  };
  sessionStorage.setItem('harness-followup:run-a', JSON.stringify(body));
  vi.mocked(createFollowup).mockResolvedValue(makeRun({ run_id: 'run-b' }));
  show();
  expect(await screen.findByLabelText('输入消息')).toHaveValue(body.prompt);
  expect(createFollowup).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '重试确认' }));
  await waitFor(
    /* 请求确认使用固定配置，页面默认配置不能覆盖。 */ () =>
      expect(createFollowup).toHaveBeenCalledWith('run-a', body),
  );
});

it('普通未发送草稿在刷新后恢复，读取不会发请求', async () => {
  // 会话草稿由session保存，回看运行或重新打开均可继续编辑。
  const user = userEvent.setup();
  const first = show();
  await user.type(await screen.findByLabelText('输入消息'), '尚未提交的草稿');
  first.unmount();
  show();
  expect(await screen.findByLabelText('输入消息')).toHaveValue('尚未提交的草稿');
  expect(createFollowup).not.toHaveBeenCalled();
});

it('Enter发送、Shift+Enter换行，输入法选字不触发提交且连续Enter防重', async () => {
  // 以DOM组合事件覆盖中文输入法，并让HTTP保持未决检查快速重复按键。
  const user = userEvent.setup();
  vi.mocked(createFollowup).mockReturnValue(
    new Promise(/* 本例仅验证发送前边界，不确认响应。 */ () => undefined),
  );
  show();
  const input = await screen.findByLabelText('输入消息');
  await user.type(input, '第一行');
  await user.keyboard('{Shift>}{Enter}{/Shift}');
  await user.type(input, '第二行');
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: true });
  expect(createFollowup).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 });
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 });
  await waitFor(
    /* Enter连按只能发出一个完整多行要求。 */ () => expect(createFollowup).toHaveBeenCalledTimes(1),
  );
  expect(vi.mocked(createFollowup).mock.calls[0][1].prompt).toBe('第一行\n第二行');
});

it('运行中可编辑下一条，但禁止新提交', async () => {
  // 活动任务不会被编辑或Enter打断。
  const user = userEvent.setup();
  vi.mocked(getConversation).mockResolvedValue(
    conversation({ can_continue: false, blocked_code: 'run_busy', blocked_reason: '等待执行结束' }),
  );
  show(makeRun({ active: true, phase: 'model' }));
  await user.type(await screen.findByLabelText('输入消息'), '下一条要求');
  await user.keyboard('{Enter}');
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
  expect(screen.getByLabelText('输入消息')).toHaveValue('下一条要求');
  expect(createFollowup).not.toHaveBeenCalled();
});

it('回看旧轮只选择详情，消息纯文本输入保持原样', async () => {
  // 用户正文不解析HTML，运行选择通过回调而非整页导航完成。
  const user = userEvent.setup();
  vi.mocked(getConversation).mockResolvedValue(
    conversation({ turns: [{ ...conversation().turns[0], prompt: '<script>hack()</script>' }] }),
  );
  const host = show();
  expect(await screen.findByText('<script>hack()</script>')).toBeInTheDocument();
  expect(document.querySelector('script')).toBeNull();
  await user.click(screen.getByRole('button', { name: '查看第 1 轮详情' }));
  expect(host.onInspect).toHaveBeenCalledWith('run-a', 'overview');
  expect(createFollowup).not.toHaveBeenCalled();
});

it('明确拒绝保留输入，核对后重试使用新编号', async () => {
  // 已拒绝请求不会被当成未知执行；修正条件后允许用户主动重新提交。
  const user = userEvent.setup();
  vi.mocked(createFollowup)
    .mockRejectedValueOnce(
      Object.assign(new Error('工作区已变化'), { status: 409, code: 'workspace_snapshot_stale' }),
    )
    .mockResolvedValueOnce(makeRun({ run_id: 'run-b' }));
  show();
  await user.type(await screen.findByLabelText('输入消息'), '继续调整');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByText('工作区已变化')).toBeInTheDocument();
  const id = vi.mocked(createFollowup).mock.calls[0][1].request_id;
  expect(screen.getByLabelText('输入消息')).toHaveValue('继续调整');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(
    /* 明确的新提交只在用户第二次点击后发生。 */ () =>
      expect(createFollowup).toHaveBeenCalledTimes(2),
  );
  expect(vi.mocked(createFollowup).mock.calls[1][1].request_id).not.toBe(id);
});

it('丢弃最新工作树后及时禁止发送，保留已编辑草稿', async () => {
  // 两个真实组件共享会话缓存，防止审阅后输入栏继续显示可提交。
  const user = userEvent.setup();
  const reason = '最新一轮工作树已经丢弃，不能继续修改。';
  vi.mocked(getConversation)
    .mockResolvedValueOnce(conversation())
    .mockResolvedValue(
      conversation({
        can_continue: false,
        blocked_code: 'project_already_decided',
        blocked_reason: reason,
      }),
    );
  vi.mocked(reviewRun).mockResolvedValue(makeProject({ status: 'discarded' }));
  show(makeRun({ project: makeProject() }), true);
  await user.type(await screen.findByLabelText('输入消息'), '保留草稿');
  await user.click(screen.getByRole('button', { name: '丢弃工作树' }));
  await user.click(await screen.findByRole('button', { name: '确认丢弃' }));
  expect(await screen.findByText(reason)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
  expect(screen.getByLabelText('输入消息')).toHaveValue('保留草稿');
  expect(createFollowup).not.toHaveBeenCalled();
});
