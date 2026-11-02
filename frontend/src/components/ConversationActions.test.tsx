import { render, screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import {
  getConversationManagement,
  getOptions,
  manageConversation,
  manageConversationBatch,
} from '../api';
import { makeConversationManagement, options } from '../test/fixtures';
import type { ConversationManagement, ConversationSummary } from '../types';
import { ConversationActions } from './ConversationActions';
import { ConversationBatchActions } from './ConversationBatchActions';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getConversationManagement: vi.fn(),
  getOptions: vi.fn(),
  manageConversation: vi.fn(),
  manageConversationBatch: vi.fn(),
}));

/** 在真实缓存和路由中挂载children；返回交互器，不发起执行或真实目录选择。 */
function mount(children: React.ReactNode) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.EachTarget });
}

beforeEach(
  /* 每例独立记录管理请求，不让前例失败载荷泄漏。 */ () => {
    vi.resetAllMocks();
    vi.mocked(getOptions).mockResolvedValue(options);
  },
);

it('重命名在保存后提交稳定身份和版本，不更改消息内容', async () => {
  const value = makeConversationManagement({ title: '原名', revision: 3 });
  vi.mocked(manageConversation).mockResolvedValue({ ...value, title: '新名称', revision: 4 });
  const changed = vi.fn();
  const user = mount(<ConversationActions value={value} onChanged={changed} />);
  await user.click(screen.getByRole('button', { name: '管理对话：原名' }));
  await user.click(screen.getByRole('menuitem', { name: '重命名' }));
  await user.clear(screen.getByLabelText('对话名称'));
  await user.paste('  新名称  ');
  expect(manageConversation).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(/* 服务器确认后才通知父页面。 */ () => expect(changed).toHaveBeenCalledOnce());
  expect(manageConversation).toHaveBeenCalledWith(
    'run-a',
    expect.objectContaining({
      action: 'rename',
      title: '新名称',
      expected_revision: 3,
      request_id: expect.any(String),
    }),
    false,
  );
});

it('保存失败保留名称，响应不明时重试沿用同一编号和正文', async () => {
  const value = makeConversationManagement({ title: '保留名称' });
  vi.mocked(manageConversation)
    .mockRejectedValueOnce(new Error('连接暂时中断'))
    .mockResolvedValueOnce(value);
  const user = mount(<ConversationActions value={value} />);
  await user.click(screen.getByRole('button', { name: '管理对话：保留名称' }));
  await user.click(screen.getByRole('menuitem', { name: '重命名' }));
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(await screen.findByText('连接暂时中断')).toBeInTheDocument();
  expect(screen.getByLabelText('对话名称')).toHaveValue('保留名称');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(
    /* 同一未知请求不分配第二个编号。 */ () => expect(manageConversation).toHaveBeenCalledTimes(2),
  );
  expect(vi.mocked(manageConversation).mock.calls[1]).toEqual(
    vi.mocked(manageConversation).mock.calls[0],
  );
});

it('移动展示执行来源约束，并允许明确移至未分组', async () => {
  const value = makeConversationManagement({ title: '项目会话', assigned_project_id: 'alpha' });
  vi.mocked(manageConversation).mockResolvedValue({ ...value, assigned_project_id: null });
  const user = mount(<ConversationActions value={value} />);
  await user.click(screen.getByRole('button', { name: '管理对话：项目会话' }));
  await user.click(screen.getByRole('menuitem', { name: '移动到项目' }));
  expect(screen.getByText(/继续执行仍使用原来的工作目录/)).toBeInTheDocument();
  await user.click(screen.getByRole('combobox', { name: '目标项目' }));
  await user.click(screen.getByRole('option', { name: '未分组' }));
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(
    /* 后端只收到整理项目字段，不接收执行目录。 */ () =>
      expect(manageConversation).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(manageConversation).mock.calls[0][1]).toMatchObject({
    action: 'move',
    project_id: null,
  });
  expect(vi.mocked(manageConversation).mock.calls[0][1]).not.toHaveProperty('workspace');
});

it('冲突后明确刷新保留草稿，并以新版本和新编号重新保存', async () => {
  const value = makeConversationManagement({ title: '原名', revision: 1 });
  vi.mocked(manageConversation)
    .mockRejectedValueOnce(new Error('对话已在其他窗口更新'))
    .mockResolvedValueOnce({ ...value, title: '本地草稿', revision: 3 });
  vi.mocked(getConversationManagement).mockResolvedValue({
    ...value,
    title: '其他窗口名称',
    revision: 2,
  });
  const user = mount(<ConversationActions value={value} />);
  await user.click(screen.getByRole('button', { name: '管理对话：原名' }));
  await user.click(screen.getByRole('menuitem', { name: '重命名' }));
  await user.clear(screen.getByLabelText('对话名称'));
  await user.paste('本地草稿');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await user.click(await screen.findByRole('button', { name: '刷新状态' }));
  await waitFor(
    /* 明确刷新先读取权威版本。 */ () => expect(getConversationManagement).toHaveBeenCalledOnce(),
  );
  expect(screen.getByLabelText('对话名称')).toHaveValue('本地草稿');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(
    /* 用户重新确认后用新版本保存。 */ () => expect(manageConversation).toHaveBeenCalledTimes(2),
  );
  const first = vi.mocked(manageConversation).mock.calls[0][1] as { request_id: string };
  const second = vi.mocked(manageConversation).mock.calls[1][1];
  expect(second).toMatchObject({ title: '本地草稿', expected_revision: 2 });
  expect(second).not.toMatchObject({ request_id: first.request_id });
});

it('编辑弹窗打开后收到其他窗口更新，保存仍使用原始版本以触发冲突', async () => {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const original = makeConversationManagement({ title: '原名称', revision: 1 });
  /** 按value重渲染同一管理实例；保留弹窗草稿以模拟真实SSE更新。 */
  function subject(value: ConversationManagement) {
    return (
      <QueryClientProvider client={cache}>
        <ConversationActions value={value} />
      </QueryClientProvider>
    );
  }
  const { rerender } = render(subject(original));
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '管理对话：原名称' }));
  await user.click(screen.getByRole('menuitem', { name: '重命名' }));
  await user.clear(screen.getByLabelText('对话名称'));
  await user.paste('尚未保存草稿');
  rerender(subject({ ...original, title: '其他窗口名称', revision: 2 }));
  vi.mocked(manageConversation).mockRejectedValue(new Error('版本冲突'));
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(
    /* 实时状态变更不能替用户自动确认新版本。 */ () =>
      expect(manageConversation).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(manageConversation).mock.calls[0][1]).toMatchObject({
    title: '尚未保存草稿',
    expected_revision: 1,
  });
  expect(screen.getByLabelText('对话名称')).toHaveValue('尚未保存草稿');
});

it('活动会话的移动归档删除禁用，命名和置顶仍可用', async () => {
  const value = makeConversationManagement({
    title: '执行中',
    can_organize: false,
    blocked_reason: '作业正在执行',
  });
  const user = mount(<ConversationActions value={value} />);
  await user.click(screen.getByRole('button', { name: '管理对话：执行中' }));
  for (const name of ['移动到项目', '归档', '删除'])
    expect(screen.getByRole('menuitem', { name })).toHaveAttribute('aria-disabled', 'true');
  expect(screen.getByRole('menuitem', { name: '重命名' })).not.toHaveAttribute(
    'aria-disabled',
    'true',
  );
});

it('删除必须确认移入回收站，不直接发起彻底清理', async () => {
  const value = makeConversationManagement({ title: '删除示例', run_count: 3 });
  vi.mocked(manageConversation).mockResolvedValue({ ...value, state: 'trashed' });
  const user = mount(<ConversationActions value={value} />);
  await user.click(screen.getByRole('button', { name: '管理对话：删除示例' }));
  await user.click(screen.getByRole('menuitem', { name: '删除' }));
  expect(screen.getByText(/3 轮对话/)).toBeInTheDocument();
  expect(manageConversation).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '移入回收站' }));
  await waitFor(
    /* 普通删除是可恢复状态操作。 */ () => expect(manageConversation).toHaveBeenCalledOnce(),
  );
  expect(manageConversation).toHaveBeenCalledWith(
    'run-a',
    expect.objectContaining({ action: 'trash' }),
    false,
  );
});

it('彻底删除要求额外确认，并显示独立保留的项目记忆数量', async () => {
  const value: ConversationManagement = makeConversationManagement({
    title: '回收示例',
    state: 'trashed',
    can_purge: true,
    memory_references: 2,
  });
  vi.mocked(manageConversation).mockResolvedValue({ ...value, state: 'purged' });
  const user = mount(<ConversationActions value={value} />);
  await user.click(screen.getByRole('button', { name: '管理对话：回收示例' }));
  await user.click(screen.getByRole('menuitem', { name: '彻底删除' }));
  expect(screen.getByText(/另存项目记忆引用：2 条/)).toBeInTheDocument();
  const button = screen.getByRole('button', { name: '彻底删除' });
  expect(button).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: '确认永久删除这些记录' }));
  await user.click(button);
  await waitFor(
    /* 只有明确确认才发DELETE载荷。 */ () => expect(manageConversation).toHaveBeenCalledOnce(),
  );
  expect(manageConversation).toHaveBeenCalledWith(
    'run-a',
    expect.objectContaining({ confirm: true }),
    true,
  );
});

it('批量部分失败只保留失败会话选择，不把冲突项当成成功', async () => {
  const changed = vi.fn();
  const items = ['a', 'b'].map(
    /* 每项有独立会话身份和版本。 */ (id) =>
      ({
        session_id: id,
        title: id,
        management: makeConversationManagement({ session_id: id, title: id }),
      }) as ConversationSummary,
  );
  vi.mocked(manageConversationBatch).mockResolvedValue({
    items: [
      { session_id: 'a', success: true },
      {
        session_id: 'b',
        success: false,
        error: { code: 'conversation_busy', message: '请先处理作业' },
      },
    ],
  });
  const user = mount(<ConversationBatchActions items={items} state="active" onDone={changed} />);
  await user.click(screen.getByRole('button', { name: '批量删除' }));
  await user.click(screen.getByRole('button', { name: '确认批量操作' }));
  await waitFor(/* 仅取消成功项的选择。 */ () => expect(changed).toHaveBeenCalledWith(['b']));
  expect(await screen.findByText(/请先处理作业/)).toBeInTheDocument();
  expect(manageConversationBatch).toHaveBeenCalledWith(
    expect.objectContaining({
      action: 'trash',
      items: [
        { session_id: 'a', expected_revision: 0 },
        { session_id: 'b', expected_revision: 0 },
      ],
    }),
  );
});
