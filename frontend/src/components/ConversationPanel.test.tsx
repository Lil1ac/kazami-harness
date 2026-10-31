import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getRun, getTasks, createFollowup, getConversation, getDiff, reviewRun } from '../api';
import { ConfigDraftProvider } from '../config';
import { makeProject, makeRun, makeJob, jobRuns } from '../test/fixtures';
import type { ConversationView, RunView, JobView } from '../types';
import { ConversationPanel } from './ConversationPanel';
import { ProjectReviewPanel } from './ProjectReviewPanel';

vi.mock(
  '../api',
  /* 只模拟HTTP边界，组件、缓存、输入和幂等状态使用真实实现。 */ () => ({
    getConversation: vi.fn(),
    createFollowup: vi.fn(),
    getRun: vi.fn(),
    getTasks: vi.fn(),
    getEvents: vi.fn(),
    getDiff: vi.fn(),
    getProjectReview: vi.fn(),
    reviewRun: vi.fn(),
    patchUrl: /* 固定附件地址，不触发下载。 */ (id: string) => `/api/runs/${id}/patch`,
    planMarkdownUrl: /* 本例仅测试审批发送，不触发附件读取。 */ (id: string, version: number) =>
      `/api/runs/${id}/artifacts?location=run&path=plans%2Fv${version}.md`,
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
        {review && (
          <ProjectReviewPanel project={current.project!} active={current.active} canManage />
        )}
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
    vi.mocked(getRun).mockImplementation(async (id) => jobRuns.get(id) || makeRun({ run_id: id }));
    vi.mocked(getTasks).mockResolvedValue({ revision: 0, items: [] });
    sessionStorage.clear();
    vi.mocked(getConversation).mockResolvedValue(conversation());
    vi.mocked(getDiff).mockResolvedValue({ content: '+fixed', truncated: false, size_bytes: 6 });
  },
);

it('批准操作经真实发送Hook绑定当前请求和版本，重复点击不重复提交', async () => {
  const user = userEvent.setup();
  const run = makeRun();
  run.result.execution_status = 'waiting_input';
  run.result.planning = {
    phase: 'planning',
    reason: '先研究',
    // 批准入口必须同时展示与请求匹配的真实方案，不能仅靠一个待批准编号授权。
    plans: [
      {
        version: 3,
        title: '实现登录与退出',
        summary: '前后端联调并核对会话状态。',
        scope: ['登录接口与页面'],
        steps: [{ id: 'step-1', text: '实现并联调', status: 'pending' }],
        verification: ['登录后退出并检查会话'],
        workspace: 'D:/project',
        workspace_identity: [1, 2],
        status: 'pending',
      },
    ],
    decisions: [],
    pending: {
      id: 'current-request',
      kind: 'plan_approval',
      question: '请审阅',
      options: [],
      plan_version: 3,
    },
  };
  let accept!: (view: JobView) => void;
  vi.mocked(createFollowup).mockReturnValue(
    new Promise(
      /* 延迟HTTP确认以验证双击不会重新分配批准请求。 */ (resolve) => {
        accept = resolve;
      },
    ),
  );
  show(run);
  const approve = await screen.findByRole('button', { name: '批准并执行' });
  await user.dblClick(approve);
  expect(createFollowup).toHaveBeenCalledTimes(1);
  expect(vi.mocked(createFollowup).mock.calls[0]).toEqual([
    'run-a',
    expect.objectContaining({
      prompt: '批准执行计划 v3',
      decision: { interaction_id: 'current-request', action: 'approve', plan_version: 3 },
    }),
  ]);
  accept(makeJob(makeRun({ run_id: 'run-b' })));
  await waitFor(
    /* 确认后旧请求不再保留于草稿。 */ () =>
      expect(screen.queryByText('发送中')).not.toBeInTheDocument(),
  );
});

it('发送实际载荷，服务确认前显示发送中，确认后交付真实新轮', async () => {
  // pending用户消息不会被伪装成真实Agent回答。
  const user = userEvent.setup();
  let accept!: (run: JobView) => void;
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
    config: { auto_repair: false, memory: { use: true, capture: true } },
  });
  accept(makeJob(makeRun({ run_id: 'run-b' })));
  await waitFor(
    /* 只有服务确认后才交付新运行。 */ () =>
      expect(host.onConfirmed).toHaveBeenCalledWith(expect.objectContaining({ run_id: 'run-b' })),
  );
  expect(screen.getByLabelText('输入消息')).toHaveValue('');
});

it('下一轮公开测试设置可关闭继承的自动修复，发送时使用当前选择', async () => {
  // 查看旧轮不改变最新父轮，开关只控制下一条明确要求。
  vi.mocked(createFollowup).mockRejectedValue(
    Object.assign(new Error('明确拒绝'), { status: 400 }),
  );
  show(
    makeRun({
      verification_kind: 'project_tests',
      config: { ...makeRun().config, auto_repair: true },
    }),
  );
  const user = userEvent.setup();
  await user.click(await screen.findByText(/下一轮设置/));
  const checkbox = screen.getByRole('checkbox', { name: '测试失败自动修复（最多2次）' });
  expect(checkbox).toBeChecked();
  await user.click(checkbox);
  await user.type(screen.getByLabelText('输入消息'), '只执行一次');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(
    /* 请求载荷必须冻结明确关闭的值。 */ () => expect(createFollowup).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createFollowup).mock.calls[0][1].config?.auto_repair).toBe(false);
});

it('网络失败重试固定父轮、编号与原载荷，发送期间新写的草稿保留', async () => {
  // 即使用户改写了输入，重试确认也只能确认原请求。
  const user = userEvent.setup();
  vi.mocked(createFollowup)
    .mockRejectedValueOnce(new Error('网络断开'))
    .mockResolvedValueOnce(makeJob(makeRun({ run_id: 'run-b' })));
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
  expect(host.onInspect).toHaveBeenCalledWith('run-a', 'result');
  expect(createFollowup).not.toHaveBeenCalled();
});

it('明确拒绝保留输入，核对后重试使用新编号', async () => {
  // 已拒绝请求不会被当成未知执行；修正条件后允许用户主动重新提交。
  const user = userEvent.setup();
  vi.mocked(createFollowup)
    .mockRejectedValueOnce(
      Object.assign(new Error('工作区已变化'), { status: 409, code: 'workspace_snapshot_stale' }),
    )
    .mockResolvedValueOnce(makeJob(makeRun({ run_id: 'run-b' })));
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
