import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { cancelSubagent, getSubagent, getSubagentDiff, getIntegrationTargets, getIntegrations, createIntegration } from '../api';
import type { SubagentDetail, SubagentRecord } from '../types';
import { SubagentsPanel } from './SubagentsPanel';
import { MemoryRouter } from 'react-router';

vi.mock(
  '../api',
  /* 保留真实查询及展示，只替换HTTP读边界。 */ () => ({
    getSubagent: vi.fn(),
    getSubagentDiff: vi.fn(),
    getIntegrationTargets: vi.fn(),
    getIntegrations: vi.fn(),
    createIntegration: vi.fn(),
    discardSubagentWorkspace: vi.fn(),
    // 子补丁链接仅引用父子编号，测试中不执行下载。
    patchUrl: (id: string, parent: string) => `/api/runs/${id}/patch?parent_run_id=${parent}`,
    cancelSubagent: vi.fn(),
    errorMessage: (error: Error) => error.message,
  }),
);

const record: SubagentRecord = {
  run_id: 'child-a',
  title: '检查调用方',
  objective: '只读检查api.py',
  role: 'review',
  model: { provider: 'qwen', requested_model: 'qwen-plus' },
  response_model: 'qwen-plus-fixture',
  cleanup_status: 'not_needed', log_parent_id: null, workspace: null, write_scope: [], changed_paths: [], scope_violations: [], pending_action: null, snapshot_status: 'pending',
  task_id: null,
  status: 'completed',
  stop_reason: null,
  report: {
    summary: 'api.py定义value。',
    findings: [
      {
        category: 'optional',
        title: '补充调用方检查',
        detail: '可选建议，不扩大本轮范围。',
        evidence_call_ids: ['read-1'],
      },
    ],
    limitations: [],
    pending: [],
    evidence: [
      { tool_call_id: 'read-1', tool: 'read_file', path: 'api.py', sha256: null, truncated: false },
    ],
    partial: false,
    reason: null,
  },
  skill_names: ['alpha'],
  parent_reserve: { model_turns: 2, tool_calls: 1, seconds: 10 },
  model_turns: 2,
  tool_calls: 1,
  http_attempts: 2,
  usage: { total_tokens: 80 },
  usage_complete: true,
  elapsed_seconds: 1.5,
  event_count: 18,
  max_model_turns: 4,
  max_tool_calls: 8,
  timeout_seconds: 60,
}; // 固定后端快照，子研究完成不等于父任务完成。

/** 为source构造有界子详情；返回可覆盖的HTTP结构，不模拟执行副作用。 */
function detail(source = record): SubagentDetail {
  return {
    subagent: source,
    artifacts: [], artifact_status: 'complete', artifact_warnings: [],
    report_page: {
      content: JSON.stringify(source.report),
      offset: 0,
      next_offset: null,
      eof: true,
      total_bytes: 30,
    },
    events: { items: [], has_more: false, last_seq: 18 },
    context: null,
    warnings: [],
  };
}

/** 挂载真实查询容器；source为当前快照，active仅控制轮询和中断文案，返回渲染结果。 */
function show(source = record, active = false) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter><SubagentsPanel parentId="parent-a" records={[source]} active={active} /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(
  /* 每例独立读响应，不允许遗留请求或缓存伪造状态。 */ () => {
    vi.resetAllMocks();
    vi.mocked(getSubagent).mockResolvedValue(detail());
  },
);

it('展开后才读取子报告、真实来源和用量，并说明已计入父总数', async () => {
  const user = userEvent.setup();
  show();
  expect(getSubagent).not.toHaveBeenCalled();
  await user.click(screen.getByText('检查调用方'));
  expect(await screen.findByText('api.py定义value。')).toBeInTheDocument();
  expect(screen.getByText('api.py')).toBeInTheDocument();
  expect(screen.getByText('可选建议 · 补充调用方检查')).toBeInTheDocument();
  expect(screen.getByText('本次技能：alpha')).toBeInTheDocument();
  expect(screen.getByText(/已计入父运行/)).toBeInTheDocument();
  expect(screen.getByText('审阅 · 报告已返回')).toBeInTheDocument();
  expect(getSubagent).toHaveBeenCalledWith('parent-a', 'child-a', 0, 0, expect.any(AbortSignal));
});

it('报告分页和事件分页使用各自服务端游标，不重新执行', async () => {
  const first = detail();
  first.report_page.eof = false;
  first.report_page.next_offset = 8190;
  first.events = {
    items: [
      {
        schema_version: 1,
        run_id: 'child-a',
        seq: 5,
        time_utc: '2026-11-01T00:00:00Z',
        elapsed_ms: 0,
        type: 'run_started',
        payload: {},
      },
    ],
    has_more: true,
    last_seq: 18,
  };
  vi.mocked(getSubagent).mockResolvedValue(first);
  const user = userEvent.setup();
  show();
  await user.click(screen.getByText('检查调用方'));
  await user.click(await screen.findByText('完整报告'));
  await user.click(await screen.findByRole('button', { name: '下一页报告' }));
  await waitFor(
    /* 报告仅使用UTF-8安全偏移，不混用事件序号。 */ () =>
      expect(getSubagent).toHaveBeenLastCalledWith(
        'parent-a',
        'child-a',
        0,
        8190,
        expect.any(AbortSignal),
      ),
  );
  await user.click(screen.getByText('子执行轨迹'));
  await user.click(await screen.findByRole('button', { name: '下一页轨迹' }));
  await waitFor(
    /* 事件按本页最后序号前进，last_seq不是本页末尾。 */ () =>
      expect(getSubagent).toHaveBeenLastCalledWith(
        'parent-a',
        'child-a',
        5,
        8190,
        expect.any(AbortSignal),
      ),
  );
});

it('部分报告保留证据和未完成事项，执行结束不显示完整报告已返回', async () => {
  const partial = {
    ...record,
    report: {
      ...record.report!,
      partial: true,
      reason: 'subagent_marked_partial',
      pending: ['尚未核对调用方'],
      limitations: ['仅检查了api.py'],
    },
  };
  vi.mocked(getSubagent).mockResolvedValue(detail(partial));
  const user = userEvent.setup();
  show(partial);
  expect(screen.getByText('审阅 · 报告部分返回')).toBeInTheDocument();
  await user.click(screen.getByText('检查调用方'));
  expect(await screen.findByText('研究仅部分完成，请先核对限制和未完成事项。')).toBeInTheDocument();
  expect(screen.getByText('尚未核对调用方')).toBeInTheDocument();
  expect(screen.getByText('api.py')).toBeInTheDocument();
});

it('父运行停止而子缺终态时明确中断，读取失败不会展示成功', async () => {
  vi.mocked(getSubagent).mockRejectedValue(new Error('子执行记录无法核对。'));
  const user = userEvent.setup();
  show({ ...record, status: 'running', report: null });
  expect(screen.getByText(/已中断，结果待核对/)).toBeInTheDocument();
  await user.click(screen.getByText('检查调用方'));
  expect(await screen.findByText('子执行记录无法核对。')).toBeInTheDocument();
  expect(screen.queryByText(/报告已返回/)).not.toBeInTheDocument();
});

it('活动子任务可单独停止，失败保留提示并可重试，历史不提供停止入口', async () => {
  // 单项停止必须绑定父子编号；失败不把页面伪装为已取消。
  const user = userEvent.setup();
  const active = { ...record, status: 'running' as const, report: null };
  vi.mocked(getSubagent).mockResolvedValue({ ...detail(), subagent: active });
  vi.mocked(cancelSubagent).mockRejectedValueOnce(new Error('停止暂未确认'));
  vi.mocked(cancelSubagent).mockResolvedValueOnce({ subagent: { ...active, status: 'cancelled' } });
  show(active, true);
  await user.click(screen.getByText('检查调用方'));
  await user.click(await screen.findByRole('button', { name: '停止此子任务' }));
  expect(await screen.findByText('停止暂未确认')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '停止此子任务' }));
  await waitFor(() => expect(cancelSubagent).toHaveBeenCalledTimes(2));
  expect(cancelSubagent).toHaveBeenLastCalledWith('parent-a', 'child-a');
});


it('开发子成果展示实际范围和固定差异，并限定采纳目标为父工作区', async () => {
  const user = userEvent.setup();
  const source: SubagentRecord = { ...record, role: 'develop', workspace: 'D:/worktrees/child', write_scope: ['src'], changed_paths: ['src/api.py'], snapshot_status: 'ready' };
  vi.mocked(getSubagent).mockResolvedValue(detail(source));
  vi.mocked(getSubagentDiff).mockResolvedValue({ content: '+fixed_api', truncated: false });
  vi.mocked(getIntegrationTargets).mockResolvedValue({ items: [{ id: 'parent-parent-a', label: '父工作区', path: 'D:/project', repository: 'D:/project', head: 'head', branch: null }] });
  vi.mocked(getIntegrations).mockResolvedValue({ items: [] });
  show(source);
  await user.click(screen.getByText(source.title));
  expect(await screen.findByText('修改范围：src')).toBeInTheDocument();
  await user.click(await screen.findByText('子成果差异 · 1 个文件'));
  expect(screen.getByText('+fixed_api')).toBeInTheDocument();
  expect(getSubagentDiff).toHaveBeenCalledWith('parent-a', 'child-a');
  await waitFor(() => expect(getIntegrationTargets).toHaveBeenCalledWith('child-a', 'parent-a'));
  expect(createIntegration).not.toHaveBeenCalled();
});

it('超范围成果明确阻止采纳，读取差异仍保持只读', async () => {
  const user = userEvent.setup();
  const source: SubagentRecord = { ...record, role: 'develop', status: 'failed', workspace: 'D:/worktrees/child', write_scope: ['src'], scope_violations: ['outside.py'], snapshot_status: 'ready' };
  vi.mocked(getSubagent).mockResolvedValue(detail(source));
  vi.mocked(getSubagentDiff).mockResolvedValue({ content: '+outside', truncated: false });
  show(source);
  await user.click(screen.getByText(source.title));
  expect(await screen.findByText('超出委派范围，不能采纳：outside.py')).toBeInTheDocument();
  expect(getIntegrationTargets).not.toHaveBeenCalled();
  expect(createIntegration).not.toHaveBeenCalled();
});


it('审批前段的核验证据显示实际执行来源，不冒充当前段工具回执', async () => {
  const user = userEvent.setup();
  const previous = structuredClone(record);
  previous.report!.evidence[0].run_id = 'prior-execution';
  vi.mocked(getSubagent).mockResolvedValue(detail(previous));
  show(previous);
  await user.click(screen.getByText('检查调用方'));
  expect(await screen.findByText(/此前执行 prior-ex/)).toBeInTheDocument();
});
