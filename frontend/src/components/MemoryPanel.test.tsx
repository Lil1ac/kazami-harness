import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getMemories, mutateMemory } from '../api';
import type { MemoryEntry } from '../types';
import { MemoryPanel, SaveMemoryButton } from './MemoryPanel';

vi.mock(
  '../api',
  /* 只替换 HTTP；草稿、版本与重试编号使用真实组件。 */ () => ({
    getMemories: vi.fn(),
    mutateMemory: vi.fn(),
    errorMessage: /* 只展示受控错误。 */ (error: unknown) =>
      error instanceof Error ? error.message : '请求失败',
  }),
);

/** 构造一条宿主返回的记忆，override 仅用于准备测试状态，返回独立对象。 */
function item(override: Partial<MemoryEntry> = {}): MemoryEntry {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    revision: 2,
    title: '接口兼容决定',
    content: '保留 legacy_name，客户升级后再删除。',
    type: 'decision',
    status: 'active',
    tags: [],
    branch: null,
    commit: null,
    expires_at: null,
    details: {
      topic: '接口兼容',
      choice: '保留 legacy_name',
      rationale: '客户仍未升级',
      revisit_when: '',
    },
    origin: 'user_confirmation',
    applies_to: { paths: [], phases: [], platforms: [] },
    evidence: [],
    evidence_refs: [],
    source: { kind: 'manual_confirmation', run_id: '22222222-2222-4222-8222-222222222222' },
    replacements: [],
    content_sha256: 'a'.repeat(64),
    created_at: '2026-10-31T00:00:00Z',
    updated_at: '2026-10-31T00:00:00Z',
    ...override,
  };
}

/** 挂载 element 到真实查询和路由环境；返回用户操作器，测试不访问网络或模型。 */
function setup(element: React.ReactNode) {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(
  /* 每个用例清空模拟回执，防止失败重试污染其他场景。 */ () => {
    vi.resetAllMocks();
  },
);

it('人工保存消息可编辑，失败后保留草稿并复用原编号', async () => {
  vi.mocked(mutateMemory).mockRejectedValueOnce(new Error('连接中断')).mockResolvedValue(item());
  const user = setup(<SaveMemoryButton runId="run-one" text={'接口决定\n保留 legacy_name'} />);
  expect(getMemories).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '保存为项目记忆' }));
  await user.type(screen.getByLabelText('事实对象'), '接口');
  await user.type(screen.getByLabelText('对象属性'), '字段');
  await user.type(screen.getByLabelText('属性值'), 'legacy_name');
  await user.click(screen.getByRole('button', { name: /^保\s*存$/ }));
  expect(await screen.findByText('连接中断')).toBeInTheDocument();
  expect(screen.getByLabelText('记忆正文')).toHaveValue('接口决定\n保留 legacy_name');
  const first = vi.mocked(mutateMemory).mock.calls[0];
  await user.click(screen.getByRole('button', { name: /^保\s*存$/ }));
  await waitFor(/* 第二次发送确认同一载荷。 */ () => expect(mutateMemory).toHaveBeenCalledTimes(2));
  expect(vi.mocked(mutateMemory).mock.calls[1]).toEqual(first);
  expect(first[0]).toBe('runs/run-one');
});

it('候选审阅绑定当前版本并明确确认，不在读取时自动保存', async () => {
  vi.mocked(getMemories).mockResolvedValue({
    scope_id: 'project',
    sequence: 4,
    items: [item({ status: 'proposed' })],
  });
  vi.mocked(mutateMemory).mockResolvedValue(item());
  const user = setup(<MemoryPanel runId="run-one" />);
  expect(await screen.findByText('接口兼容决定')).toBeInTheDocument();
  expect(mutateMemory).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: '来源运行' })).toHaveAttribute(
    'href',
    '/runs/22222222-2222-4222-8222-222222222222',
  );
  await user.click(screen.getByRole('button', { name: '保存记忆' }));
  expect(mutateMemory).toHaveBeenCalledWith(
    'runs/run-one',
    item().id,
    expect.objectContaining({ expected_revision: 2, action: 'accept' }),
  );
});

it('项目编辑遇到版本冲突保留草稿，刷新不会自动覆盖', async () => {
  vi.mocked(getMemories).mockResolvedValue({
    scope_id: 'project',
    sequence: 4,
    items: [item()],
    next_offset: null,
  });
  vi.mocked(mutateMemory).mockRejectedValue(new Error('记忆已被更新，请刷新后重新核对。'));
  const user = setup(<MemoryPanel projectId="one" />);
  await user.click(await screen.findByRole('button', { name: /^编\s*辑$/ }));
  await user.clear(screen.getByLabelText('记忆正文'));
  await user.type(screen.getByLabelText('记忆正文'), '用户的新决定');
  await user.click(screen.getByRole('button', { name: /^保\s*存$/ }));
  expect(await screen.findByText('记忆已被更新，请刷新后重新核对。')).toBeInTheDocument();
  expect(screen.getByLabelText('记忆正文')).toHaveValue('用户的新决定');
  expect(mutateMemory).toHaveBeenCalledWith(
    'projects/one',
    item().id,
    expect.objectContaining({
      action: 'edit',
      expected_revision: 2,
      content: expect.objectContaining({ content: '用户的新决定' }),
    }),
  );
});

it('删除前说明后续使用和历史快照边界，确认后才发送决定', async () => {
  vi.mocked(getMemories).mockResolvedValue({ scope_id: 'project', sequence: 4, items: [item()] });
  vi.mocked(mutateMemory).mockResolvedValue(item({ status: 'deleted' }));
  const user = setup(<MemoryPanel projectId="one" />);
  await user.click(await screen.findByRole('button', { name: /^删\s*除$/ }));
  expect(await screen.findByText('后续运行不再使用，已有运行快照仍保留。')).toBeInTheDocument();
  expect(mutateMemory).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: /OK|确定/ }));
  await waitFor(
    /* 仅明确确认后发布一次删除。 */ () => expect(mutateMemory).toHaveBeenCalledTimes(1),
  );
  expect(vi.mocked(mutateMemory).mock.calls[0][2]).toEqual(
    expect.objectContaining({ action: 'delete', expected_revision: 2 }),
  );
});

it('分类和文本筛选只影响查询，导出使用宿主附件地址', async () => {
  vi.mocked(getMemories).mockResolvedValue({ scope_id: 'project', sequence: 4, items: [] });
  const user = setup(<MemoryPanel projectId="one" />);
  await user.type(screen.getByRole('textbox', { name: '搜索记忆' }), '接口');
  await waitFor(
    /* 明确文本变化只调用读取。 */ () =>
      expect(getMemories).toHaveBeenLastCalledWith(
        'projects/one',
        expect.stringContaining('query=' + encodeURIComponent('接口')),
      ),
  );
  expect(screen.getByRole('link', { name: '导出 Markdown' })).toHaveAttribute(
    'href',
    '/api/projects/one/memories/export',
  );
  expect(mutateMemory).not.toHaveBeenCalled();
});

it('切换类型重填专属字段，入口和适用路径分开提交', async () => {
  vi.mocked(getMemories).mockResolvedValue({ scope_id: 'project', sequence: 0, items: [] });
  vi.mocked(mutateMemory).mockResolvedValue(item());
  const user = setup(<MemoryPanel projectId="one" />);
  await user.click(screen.getByRole('button', { name: '新增记忆' }));
  await user.type(screen.getByLabelText('记忆标题'), '接口指南');
  await user.click(screen.getByRole('combobox', { name: '记忆类型' }));
  await user.click(screen.getByText('信息入口', { selector: '.ant-select-item-option-content' }));
  expect(screen.queryByLabelText('事实对象')).not.toBeInTheDocument();
  await user.type(screen.getByLabelText('参考入口'), 'docs/api.md');
  await user.type(screen.getByLabelText('入口用途'), '核对外部接口契约');
  await user.click(screen.getByText('适用条件'));
  await user.type(screen.getByLabelText('适用路径'), 'src/api/**');
  await user.click(screen.getByRole('button', { name: /^保\s*存$/ }));
  expect(mutateMemory).toHaveBeenCalledWith(
    'projects/one',
    null,
    expect.objectContaining({
      type: 'reference',
      details: { target: 'docs/api.md', purpose: '核对外部接口契约', consult_when: '' },
      evidence: [],
      applies_to: expect.objectContaining({ paths: ['src/api/**'] }),
    }),
  );
});

it('冲突候选先展示旧值并明确选择替代，不直接确认', async () => {
  const candidate = item({
    status: 'proposed',
    conflicts: [
      {
        id: 'old-one',
        revision: 3,
        type: 'decision',
        branch: null,
        commit: null,
        expires_at: null,
        title: '原接口决定',
        details: { topic: '接口兼容', choice: '新字段', rationale: '旧要求', revisit_when: '' },
        applies_to: { paths: ['src/api/**'], phases: [], platforms: [] },
      },
    ],
  });
  vi.mocked(getMemories).mockResolvedValue({
    scope_id: 'project',
    sequence: 4,
    items: [candidate],
  });
  vi.mocked(mutateMemory).mockResolvedValue(item());
  const user = setup(<MemoryPanel runId="run-one" />);
  await user.click(await screen.findByRole('button', { name: '保存记忆' }));
  expect(mutateMemory).not.toHaveBeenCalled();
  expect(screen.getByRole('region', { name: '冲突审阅' })).toHaveTextContent('原接口决定 · v3');
  await user.click(screen.getByRole('checkbox', { name: '保存时替代这条记忆' }));
  await user.click(screen.getByRole('button', { name: /^保\s*存$/ }));
  expect(mutateMemory).toHaveBeenCalledWith(
    'runs/run-one',
    candidate.id,
    expect.objectContaining({
      action: 'accept',
      expected_revision: 2,
      replacements: [{ id: 'old-one', revision: 3 }],
    }),
  );
});
