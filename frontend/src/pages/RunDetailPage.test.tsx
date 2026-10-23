import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { cancelRun, createFollowup, getConversation, getRun, getRecovery, resumeRun } from '../api';
import { ConfigDraftProvider } from '../config';
import { makeRun } from '../test/fixtures';
import type { ConversationTurn, ConversationView, RunView } from '../types';
import { RunDetailPage } from './RunDetailPage';

vi.mock(
  '../api',
  /* HTTP替身保留真实会话工作区、输入、路由、Markdown和检查器组件。 */ () => ({
    getRun: vi.fn(),
    getConversation: vi.fn(),
    createFollowup: vi.fn(),
    cancelRun: vi.fn(),
    getEvents: vi.fn(),
    getRecovery: vi.fn(),
    resumeRun: vi.fn(),
    errorMessage: /* 错误只显示安全message。 */ (error: unknown) =>
      error instanceof Error ? error.message : '读取失败',
  }),
);
vi.mock(
  '../hooks/useRunEvents',
  /* SSE自身已有独立生命周期回归，这里以已读取事件检查页面组织。 */ () => ({
    useRunEvents: /* 当前测试不打开网络连接。 */ () => ({
      events: [],
      connection: 'closed',
      error: null,
      loadingOlder: false,
      loadOlder: vi.fn(),
      canLoadOlder: false,
    }),
  }),
);

let record: ConversationView; // 模拟服务端当前完整会话，POST确认后才推进最新轮。
let readsFail = false; // 短暂服务错误不会清空此前读取的快照。
const runs = new Map<string, RunView>(); // 各轮快照独立保存，回看旧轮仍读原结果。

/** 将已接纳运行投影到会话响应；不伪造未确认的用户轮次。 */
function turn(run: RunView): ConversationTurn {
  return {
    run_id: run.run_id,
    parent_run_id: run.parent_run_id || null,
    turn_index: run.turn_index || 1,
    prompt: run.prompt,
    final_answer: run.result.final_answer,
    execution_status: run.result.execution_status,
    validation_status: run.result.validation_status,
    created_at: run.created_at,
    interrupted: run.interrupted,
  };
}

/** 构造同一会话的独立运行；id与result.run_id一致，ordinal从1开始。 */
function sessionRun(id: string, ordinal = 1, prompt = '第一次修复'): RunView {
  const base = makeRun();
  return {
    ...base,
    run_id: id,
    session_id: 'run-a',
    parent_run_id: ordinal === 1 ? null : ordinal === 2 ? 'run-a' : 'run-b',
    turn_index: ordinal,
    prompt,
    result: { ...base.result, run_id: id, final_answer: `已处理：${prompt}` },
  };
}

/** 挂载真实路由与会话配置；initialPath支持旧轮或旧标签深链接。 */
function show(initialPath = '/runs/run-a') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(
  /* 每个用例从同一可信首轮开始，不共享草稿或前例的请求。 */ () => {
    vi.resetAllMocks();
    sessionStorage.clear();
    runs.clear();
    readsFail = false;
    vi.mocked(getRecovery).mockResolvedValue({
      supported: false,
      can_resume: false,
      blocked_code: 'project_required',
      blocked_reason: '首版仅支持 Git 代码任务恢复。',
      checkpoint: null,
      busy_run_id: null,
    });
    const root = sessionRun('run-a');
    runs.set(root.run_id, root);
    record = {
      session_id: 'run-a',
      latest_run_id: 'run-a',
      can_continue: true,
      blocked_code: null,
      blocked_reason: null,
      busy_run_id: null,
      turns: [turn(root)],
    };
    vi.mocked(getRun).mockImplementation(
      /* 请求只返回对应运行，模拟服务暂断时保留缓存。 */ async (id) => {
        if (readsFail) throw new Error('本地服务暂时断开');
        const value = runs.get(id);
        if (!value) throw new Error('运行不存在');
        return value;
      },
    );
    vi.mocked(getConversation).mockImplementation(
      /* 会话读取没有写入与执行动作。 */ async () => record,
    );
    vi.mocked(createFollowup).mockImplementation(
      /* 只有成功POST才增加下一轮，明确记录父轮关系。 */ async (parentId, body) => {
        const child = sessionRun(
          record.turns.length === 1 ? 'run-b' : 'run-c',
          record.turns.length + 1,
          body.prompt,
        );
        child.parent_run_id = parentId;
        runs.set(child.run_id, child);
        record = { ...record, latest_run_id: child.run_id, turns: [...record.turns, turn(child)] };
        return child;
      },
    );
  },
);

it('连续发送三轮保持同一输入节点与消息区域，各轮追加到最新父轮', async () => {
  // 路由更新不重建会话DOM，旧消息和用户阅读区域持续保留。
  const user = userEvent.setup();
  show();
  const input = await screen.findByLabelText('输入消息');
  const log = screen.getByRole('log', { name: '会话消息' });
  await user.type(input, '补充类型提示');
  await user.keyboard('{Enter}');
  expect(await screen.findByText('已处理：补充类型提示')).toBeInTheDocument();
  expect(screen.getByLabelText('输入消息')).toBe(input);
  await user.type(input, '再补充文档');
  await user.keyboard('{Enter}');
  expect(await screen.findByText('已处理：再补充文档')).toBeInTheDocument();
  expect(screen.getByLabelText('输入消息')).toBe(input);
  expect(screen.getByRole('log', { name: '会话消息' })).toBe(log);
  expect(screen.getByText('已处理：第一次修复')).toBeInTheDocument();
  expect(
    vi
      .mocked(createFollowup)
      .mock.calls.map(/* 核对实际提交父轮，避免从旧轮悄悄产生分支。 */ (call) => call[0]),
  ).toEqual(['run-a', 'run-b']);
});

it('从旧轮深链接回看记录时，发送仍追加到会话最新轮', async () => {
  // 检查旧测试与继续最新代码可以同时进行，界面不会把旧轮变成新分支。
  const user = userEvent.setup();
  const second = sessionRun('run-b', 2, '补充类型提示');
  runs.set(second.run_id, second);
  record = { ...record, latest_run_id: second.run_id, turns: [...record.turns, turn(second)] };
  show('/runs/run-a?tab=overview');
  const input = await screen.findByLabelText('输入消息');
  await user.type(input, '继续完善');
  await user.click(screen.getByRole('button', { name: '查看第 1 轮详情' }));
  expect(screen.getByText('第 1 轮详情')).toBeInTheDocument();
  expect(screen.getByLabelText('输入消息')).toBe(input);
  expect(input).toHaveValue('继续完善');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(
    /* 提交使用最新轮编号，检查器选择不参与执行调度。 */ () =>
      expect(createFollowup).toHaveBeenCalledWith(
        'run-b',
        expect.objectContaining({ prompt: '继续完善' }),
      ),
  );
});

it('后台刷新失败时保留会话、输入和上次快照', async () => {
  // 网络短暂故障不能销毁已读消息与编辑中的内容。
  const user = userEvent.setup();
  const client = show();
  const input = await screen.findByLabelText('输入消息');
  await user.type(input, '尚未发送');
  readsFail = true;
  await act(async () => /* 模拟焦点恢复时重新读取失败。 */ {
    await client.invalidateQueries({ queryKey: ['run', 'run-a'] });
  });
  expect(await screen.findByText('暂时无法刷新，保留已读取内容')).toBeInTheDocument();
  expect(input).toHaveValue('尚未发送');
  expect(screen.getByText('已处理：第一次修复')).toBeInTheDocument();
});

it('旧上下文深链接在检查器显示完整运行快照，消息仍可阅读', async () => {
  // 上下文事实不依赖当前有界SSE窗口，兼容已有收藏链接。
  const root = runs.get('run-a')!;
  root.context = {
    instruction: {
      status: 'loaded',
      path: 'AGENTS.md',
      content: '函数增加中文注释。',
      sha256: 'b'.repeat(64),
      size_bytes: 30,
      error_code: null,
    },
    last_check: {
      model_turn: 4,
      input_bytes: 4096,
      max_input_bytes: 8192,
      message_count: 12,
      allowed: true,
    },
    serialization: 'json_utf8_v1',
  };
  show('/runs/run-a?tab=context');
  expect(await screen.findByLabelText('项目指令内容')).toHaveTextContent('函数增加中文注释。');
  expect(screen.getByText(/拟请求第 4 轮 · 12 条消息/)).toBeInTheDocument();
  expect(screen.getByRole('log', { name: '会话消息' })).toBeInTheDocument();
});

it('查看旧轮时停止按钮仍明确取消最新活动运行', async () => {
  // 取消目标与检查器选择分离，不能取消一个已结束的旧轮。
  const user = userEvent.setup();
  const second = sessionRun('run-b', 2, '继续执行');
  second.active = true;
  second.phase = 'tool';
  runs.set(second.run_id, second);
  record = {
    ...record,
    latest_run_id: second.run_id,
    busy_run_id: second.run_id,
    can_continue: false,
    blocked_code: 'run_busy',
    blocked_reason: '执行中',
    turns: [...record.turns, turn(second)],
  };
  vi.mocked(cancelRun).mockResolvedValue({
    ...second,
    active: false,
    result: { ...second.result, execution_status: 'cancelled' },
  });
  show('/runs/run-a');
  await user.click(await screen.findByRole('button', { name: '停止当前执行' }));
  expect(cancelRun).not.toHaveBeenCalled();
  await user.click(await screen.findByRole('button', { name: '停止执行' }));
  await waitFor(
    /* 明确确认后才发送最新轮取消请求。 */ () => expect(cancelRun).toHaveBeenCalledWith('run-b'),
  );
});

it('GET发现已接纳新轮但快照尚未返回时保留输入节点和草稿', async () => {
  // 覆盖POST响应丢失后的会话补读；核对最新快照前禁止新提交，但不卸载对话。
  const user = userEvent.setup();
  const client = show();
  const input = await screen.findByLabelText('输入消息');
  await user.type(input, '正在编辑的下一条');
  const next = sessionRun('run-b', 2, '服务已接纳的要求');
  let resolveLatest!: (view: RunView) => void;
  const waiting = new Promise<RunView>(
    /* 精确控制最新运行读取完成时机。 */ (resolve) => {
      resolveLatest = resolve;
    },
  );
  vi.mocked(getRun).mockImplementation(
    /* 新轮快照等待中，旧轮已知事实仍可读取。 */ async (id) =>
      id === next.run_id ? waiting : runs.get(id)!,
  );
  record = { ...record, latest_run_id: next.run_id, turns: [...record.turns, turn(next)] };
  await act(async () => /* 模拟未知POST结果后补读到新的会话头。 */ {
    await client.invalidateQueries({ queryKey: ['conversation', 'run-a'] });
  });
  expect(screen.getByLabelText('输入消息')).toBe(input);
  await screen.findByText('已处理：服务已接纳的要求');
  expect(input).toHaveValue('正在编辑的下一条');
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
  await act(async () => /* 真正拿到新快照后才恢复新提交条件。 */ {
    resolveLatest(next);
  });
  await waitFor(
    /* 完成核对后无需重建输入节点。 */ () =>
      expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled(),
  );
  expect(screen.getByLabelText('输入消息')).toBe(input);
});

it('手机直接打开旧上下文链接时自动显示对应详情抽屉', async () => {
  // 深链接应保留查看意图，不能被手机默认收起逻辑覆盖。
  const original = vi.mocked(window.matchMedia).getMockImplementation()!;
  vi.mocked(window.matchMedia).mockImplementation(
    /* 只模拟会话断点，保留Ant Design其他媒体查询。 */ (query) => ({
      ...original(query),
      matches: query === '(max-width: 1100px)',
    }),
  );
  try {
    show('/runs/run-a?tab=context');
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(await screen.findByRole('tab', { name: '上下文', selected: true })).toBeInTheDocument();
  } finally {
    vi.mocked(window.matchMedia).mockImplementation(original);
  }
});

it('中断后直接发送继续，沿用输入节点并保留旧记录，不出现独立恢复入口', async () => {
  // 用原发送链路覆盖此前草稿、路由与历史回看要求，只有被接纳的新消息增加一轮。
  const user = userEvent.setup();
  const root = runs.get('run-a')!;
  root.interrupted = true;
  root.result = { ...root.result, final_answer: null, execution_status: 'running', validation_status: 'unverified' };
  record = {
    ...record, can_continue: true, blocked_code: null,
    continuation_notice: '前轮中断，可直接发送“继续”；系统会核对当前代码。',
    turns: [turn(root)],
  };
  const client = show();
  const input = await screen.findByLabelText('输入消息');
  expect(await screen.findByText('前轮中断，可直接发送“继续”；系统会核对当前代码。')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '恢复任务' })).not.toBeInTheDocument();
  await user.type(input, '继续');
  await user.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByText('已处理：继续')).toBeInTheDocument();
  expect(screen.getByLabelText('输入消息')).toBe(input);
  await user.type(input, '保留下一条草稿');
  expect(document.querySelectorAll('.user-message-body')).toHaveLength(2);
  expect(document.querySelector('.session-heading p')).toHaveTextContent('2 轮对话');
  await user.click(screen.getByRole('button', { name: '查看第 1 轮详情' }));
  expect(screen.getByText('第 1 轮详情')).toBeInTheDocument();
  expect(input).toHaveValue('保留下一条草稿');
  await act(async () => {
    // 刷新历史仅读取，不能再发送一次继续或走已退役恢复接口。
    await client.invalidateQueries({ queryKey: ['conversation', 'run-a'] });
  });
  expect(createFollowup).toHaveBeenCalledTimes(1);
  expect(vi.mocked(createFollowup).mock.calls[0][1].prompt).toBe('继续');
  expect(resumeRun).not.toHaveBeenCalled();
  expect(getRecovery).not.toHaveBeenCalled();
});