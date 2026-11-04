import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { TeamPanel } from './TeamPanel';
import { getTeam, teamAction } from '../api';
import { makeRun } from '../test/fixtures';
import type { TeamState, TeamView } from '../types';

vi.mock('../api', /* 只替换真实HTTP边界，保留错误说明和其他面板的公开接口。 */ async (original) => ({
  ...await original<typeof import('../api')>(), getTeam: vi.fn(), teamAction: vi.fn(),
}));
vi.mock('./IntegrationPanel', /* 以真实输入标识固定成果来源，不请求候选HTTP。 */ () => ({
  IntegrationPanel: ({ runId, parentRunId }: { runId: string; parentRunId: string }) => <div>整体采纳 {runId} → {parentRunId}</div>,
}));

/** 返回独立持久团队夹具；ready只控制整体成果入口，不把成员结束当作最终验收。 */
function state(ready = false): TeamState {
  return {
    id: 'team-1', title: '前后端团队', revision: 9, status: 'active', generation: 2, stage_run_id: 'stage-2', stage_ready: ready, error: null, cleanup_status: 'not_needed',
    members: [{ id: 'member-1', name: '后端成员', write_scope: ['src'], status: 'idle', active_run_id: null, task_id: null, runs: [], summary: '接口已固定', model_turns: 3, tool_calls: 2, total_tokens: 100, usage_complete: false }],
    tasks: [{ task_id: 'task-2', objective: '接入接口', member_id: 'member-1', write_scope: ['src'], depends_on: ['task-1'], dependency_versions: { 'task-1': 'fixed-12345678' }, state: ready ? 'ready' : 'pending', run_id: 'child-1', log_parent_id: 'run-1', error: null }],
    messages: [{ seq: 1, sender: 'user', recipient: 'member-1', content: '请核对已固定接口', created_at: '2026-11-04T00:00:00Z' }],
  };
}

/** 在独立缓存内渲染团队；historical控制历史只读，不提供额外权限。 */
function show(historical = false) {
  const run = makeRun({ run_id: 'run-1', active: false });
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><TeamPanel run={run} historical={historical} /></QueryClientProvider>);
}

beforeEach(/* 各测试独立的HTTP事实，没有自动续轮或副作用。 */ () => {
  vi.clearAllMocks();
  vi.mocked(getTeam).mockResolvedValue({ team: state(), tasks: { session_id: 'session-1', revision: 1, items: [] }, stage: null, historical: false } as TeamView);
  vi.mocked(teamAction).mockResolvedValue({ team: state() });
});

it('展示稳定成员、依赖版本和消息来源，空闲不自动启动执行', async () => {
  show();
  expect(await screen.findByRole('heading', { name: '前后端团队' })).toBeInTheDocument();
  expect(screen.getByText('接口已固定')).toBeInTheDocument();
  expect(screen.getByText('依赖：task-1')).toBeInTheDocument();
  expect(screen.getByText('固定版本：task-1 @ fixed-12')).toBeInTheDocument();
  expect(screen.getByText('你 → 后端成员')).toBeInTheDocument();
  expect(screen.getByText(/100 token（已知部分）/)).toBeInTheDocument();
  expect(screen.queryByText(/整体采纳/)).not.toBeInTheDocument();
  expect(teamAction).not.toHaveBeenCalled();
});

it('发送指示可靠落盘，只有显式发送并继续才要求续轮', async () => {
  const user = userEvent.setup();
  show();
  await user.type(await screen.findByRole('textbox', { name: '团队指示' }), '核对接口');
  await user.click(screen.getByRole('button', { name: /^发送指示$/ }));
  await waitFor(/* 只验证宿主公开请求契约。 */ () => expect(teamAction).toHaveBeenCalledWith('run-1', expect.objectContaining({ action: 'message', recipient: 'lead', content: '核对接口', expected_revision: 9, request_id: expect.any(String) })));
  expect(vi.mocked(teamAction).mock.calls[0][1].wake).toBeUndefined();
  await waitFor(/* 成功后清空草稿，失败保留。 */ () => expect(screen.getByRole('textbox', { name: '团队指示' })).toHaveValue(''));
  await user.type(screen.getByRole('textbox', { name: '团队指示' }), '继续联调');
  await user.click(screen.getByRole('button', { name: '发送并继续' }));
  await waitFor(/* 继续仍由唯一作业队列处理，不调用模型直连。 */ () => expect(teamAction).toHaveBeenLastCalledWith('run-1', expect.objectContaining({ wake: true })));
});

it('网络失败保留草稿并复用请求号，历史只读不提供管理操作', async () => {
  const user = userEvent.setup();
  vi.mocked(teamAction).mockRejectedValueOnce(new Error('网络未确认'));
  show();
  await user.type(await screen.findByRole('textbox', { name: '团队指示' }), '保留证据');
  await user.click(screen.getByRole('button', { name: /^发送指示$/ }));
  expect(await screen.findByText('网络未确认')).toBeInTheDocument();
  const first = vi.mocked(teamAction).mock.calls[0][1].request_id;
  await user.click(screen.getByRole('button', { name: /^发送指示$/ }));
  await waitFor(/* 重试不产生重复消息编号。 */ () => expect(teamAction).toHaveBeenCalledTimes(2));
  expect(vi.mocked(teamAction).mock.calls[1][1].request_id).toBe(first);
});

it('历史固定快照禁止发送、停止和采纳，即使当时全部成果就绪', async () => {
  vi.mocked(getTeam).mockResolvedValue({ team: state(true), tasks: { items: [] }, stage: null, historical: true } as unknown as TeamView);
  show(true);
  expect(await screen.findByText('本轮固定快照')).toBeInTheDocument();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '停止团队' })).not.toBeInTheDocument();
  expect(screen.queryByText(/整体采纳/)).not.toBeInTheDocument();
  expect(getTeam).toHaveBeenCalledWith('run-1', true);
});

it('只对完整汇集成果展示整体采纳入口', async () => {
  vi.mocked(getTeam).mockResolvedValue({ team: state(true), tasks: { items: [] }, stage: null, historical: false } as unknown as TeamView);
  show();
  expect(await screen.findByText('整体采纳 stage-2 → run-1')).toBeInTheDocument();
});
