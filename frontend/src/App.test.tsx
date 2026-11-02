import { makeConversationManagement } from './test/fixtures';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { App } from './App';
import { ConfigDraftProvider } from './config';
import { options, makeRun } from './test/fixtures';
import {
  getOptions,
  getConversations,
  getConversation,
  getRun,
  createRun,
  createFollowup,
} from './api';
import type { ConversationSummary, ConversationView, LocalProjectOption } from './types';

vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  getOptions: vi.fn(),
  getConversations: vi.fn(),
  getConversation: vi.fn(),
  getRun: vi.fn(),
  createRun: vi.fn(),
  createFollowup: vi.fn(),
  getEvents: vi.fn(
    /* 未活动的详情只需空事件列表。 */ () =>
      Promise.resolve({ items: [], has_more: false, last_seq: 0 }),
  ),
}));
vi.mock(
  './hooks/useRunEvents',
  /* 导航测试不建立真实SSE或重放工具。 */ () => ({
    useRunEvents: () => ({
      events: [],
      connection: 'closed',
      error: null,
      loadingOlder: false,
      loadOlder: vi.fn(),
      canLoadOlder: false,
    }),
  }),
);

const alpha: LocalProjectOption = {
  id: 'alpha',
  kind: 'workspace',
  label: 'Alpha 项目',
  path: '/projects/alpha',
  available: true,
}; // 第一个真实来源身份。
const beta: LocalProjectOption = {
  id: 'beta',
  kind: 'workspace',
  label: 'Beta 项目',
  path: '/projects/beta',
  available: true,
}; // 用于核对项目隔离。
const summary: ConversationSummary = {
  session_id: 'a-1',
  title: '修复导出逻辑',
  management: makeConversationManagement({
    session_id: 'a-1',
    title: '修复导出逻辑',
    assigned_project_id: alpha.id,
    assigned_project: { ...alpha, available: true },
  }),
  entry_run_id: 'a-3',
  latest_run_id: 'a-3',
  turn_count: 3,
  created_at: '2026-10-24T01:00:00Z',
  updated_at: '2026-10-24T02:00:00Z',
  source: { ...alpha, execution_mode: 'local', registered: true },
  latest: {
    execution_status: 'completed',
    validation_status: 'unverified',
    cleanup_status: 'not_needed',
    active: false,
    cancelling: false,
    interrupted: false,
    phase: 'finished',
  },
  error: null,
};
const other: ConversationSummary = {
  ...summary,
  session_id: 'b-1',
  entry_run_id: 'b-1',
  latest_run_id: 'b-1',
  title: '另一个项目的需求',
  management: makeConversationManagement({
    session_id: 'b-1',
    title: '另一个项目的需求',
    assigned_project_id: beta.id,
    assigned_project: { ...beta, available: true },
  }),
  turn_count: 1,
  source: { ...beta, execution_mode: 'local', registered: true },
}; // 不得混入Alpha列表。
const conversation: ConversationView = {
  title: summary.title,
  management: summary.management,
  session_id: 'a-1',
  latest_run_id: 'a-3',
  source: summary.source,
  can_continue: true,
  blocked_code: null,
  blocked_reason: null,
  busy_run_id: null,
  turns: [1, 2, 3].map(
    /* 模拟同一会话的三条已接纳要求。 */ (ordinal) => ({
      run_id: 'a-' + ordinal,
      parent_run_id: ordinal > 1 ? 'a-' + (ordinal - 1) : null,
      turn_index: ordinal,
      prompt: ordinal === 1 ? '修复导出逻辑' : '追加要求 ' + ordinal,
      final_answer: '已完成 ' + ordinal,
      execution_status: 'completed',
      validation_status: 'unverified',
      created_at: '2026-10-24T01:00:00Z',
    }),
  ),
};

/** 在完整壳层与真实路由中打开目标页；API替身只提供数据，不模拟导航逻辑。 */
function show(path: string): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
}

beforeEach(
  /* 每例独立初始化项目、会话和未发送草稿。 */ () => {
    vi.clearAllMocks();
    sessionStorage.clear();
    localStorage.clear();
    vi.mocked(getOptions).mockResolvedValue({ ...options, recent_projects: [alpha, beta] });
    vi.mocked(getConversations).mockImplementation(
      /* 后端已完成聚合和项目分页，前端按真实契约读取。 */ async (filters) => ({
        items:
          filters?.projectId === 'alpha'
            ? [summary]
            : filters?.projectId === 'beta'
              ? [other]
              : [summary, other],
        total: filters?.projectId ? 1 : 2,
        busy_run_id: null,
      }),
    );
    vi.mocked(getRun).mockImplementation(
      /* 旧深链接和最新轮均关联同一个会话。 */ async (id) =>
        makeRun({
          run_id: id,
          session_id: 'a-1',
          parent_run_id: id === 'a-1' ? null : 'a-2',
          turn_index: Number(id.slice(-1)) || 3,
          active: false,
        }),
    );
    vi.mocked(getConversation).mockResolvedValue(conversation);
    vi.mocked(createRun).mockRejectedValue(new Error('离线保留草稿'));
  },
);

it('项目列表只有一个三轮入口，进入聊天后可以从全部会话重新进入并保留草稿', async () => {
  const user = userEvent.setup();
  show('/projects/alpha');
  const list = await screen.findByRole('region', { name: '项目会话列表' });
  const entry = await within(list).findByRole('link', { name: '继续对话：修复导出逻辑' });
  expect(within(list).getAllByRole('link', { name: /继续对话/ })).toHaveLength(1);
  expect(within(list).getByText(/3 轮对话/)).toBeInTheDocument();
  expect(within(list).queryByText('另一个项目的需求')).not.toBeInTheDocument();
  expect(entry).toHaveAttribute('href', '/runs/a-3');
  await user.click(entry);
  const input = await screen.findByRole('textbox', { name: '输入消息' });
  expect(screen.getAllByRole('article')).toHaveLength(3);
  await user.click(input);
  await user.paste('稍后继续检查');
  await user.click(screen.getByRole('link', { name: '全部会话' }));
  const history = await screen.findByRole('region', { name: '全部会话列表' });
  await user.click(await within(history).findByRole('link', { name: '继续对话：修复导出逻辑' }));
  expect(await screen.findByRole('textbox', { name: '输入消息' })).toHaveValue('稍后继续检查');
  expect(createRun).not.toHaveBeenCalled();
  expect(createFollowup).not.toHaveBeenCalled();
}, 20000);

it('从另一个项目新建对话固定正确来源，不沿用浏览器上次选中的项目', async () => {
  const user = userEvent.setup();
  localStorage.setItem('harness-selected-project', 'alpha');
  show('/projects/beta');
  const main = screen.getByRole('main');
  await within(main).findByRole('heading', { name: /Beta 项目/ });
  await user.click(within(main).getByRole('link', { name: '新建对话' }));
  const prompt = await screen.findByRole('textbox', { name: '任务描述' });
  expect(
    screen.getByRole('combobox', { name: '选择项目' }).closest('.project-select'),
  ).toHaveTextContent('Beta 项目');
  await user.click(prompt);
  await user.paste('检查Beta的输入');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(
    /* 只有明确发送后才产生请求，来源必须为Beta。 */ () => expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createRun).mock.calls[0][0]).toMatchObject({
    workspace_id: 'beta',
    prompt: '检查Beta的输入',
  });
  await screen.findByText('离线保留草稿');
  await user.click(screen.getByRole('link', { name: '打开项目：Alpha 项目' }));
  await within(main).findByRole('heading', { name: /Alpha 项目/ });
  await user.click(within(main).getByRole('link', { name: '新建对话' }));
  expect(await screen.findByRole('textbox', { name: '任务描述' })).toHaveValue('');
  await user.click(screen.getByRole('link', { name: '打开项目：Beta 项目' }));
  await within(main).findByRole('heading', { name: /Beta 项目/ });
  await user.click(within(main).getByRole('link', { name: '新建对话' }));
  expect(await screen.findByRole('textbox', { name: '任务描述' })).toHaveValue('检查Beta的输入');
}, 20000);

it('旧轮深链接仍显示完整会话，侧栏高亮稳定会话并可进入最新轮', async () => {
  const user = userEvent.setup();
  show('/runs/a-1');
  await screen.findByRole('textbox', { name: '输入消息' });
  const sidebar = screen.getByRole('complementary', { name: '项目与会话导航' });
  // 消息区可先显示；侧栏还要依次读取当前会话和项目列表，独立等待其完成。
  const link = await within(sidebar).findByRole(
    'link',
    { name: '继续对话：修复导出逻辑' },
    { timeout: 5000 },
  );
  expect(link).toHaveAttribute('aria-current', 'page');
  expect(link).toHaveAttribute('href', '/runs/a-3');
  await user.click(link);
  expect(screen.getAllByRole('article')).toHaveLength(3);
  expect(createRun).not.toHaveBeenCalled();
  expect(createFollowup).not.toHaveBeenCalled();
}, 20000);
