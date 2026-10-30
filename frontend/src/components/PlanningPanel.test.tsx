import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { PlanningPanel } from './PlanningPanel';
import type { PlanningState } from '../types';

/** 构造一份待批准方案；返回独立状态，测试不根据模型文字推断批准。 */
function pendingPlan(): PlanningState {
  return {
    phase: 'planning',
    reason: '复杂功能',
    decisions: [],
    pending: {
      id: 'request-one',
      kind: 'plan_approval',
      question: '请批准',
      options: [],
      plan_version: 1,
    },
    plans: [
      {
        version: 1,
        title: '实现登录功能',
        summary: '前后端共同实现登录。',
        scope: ['认证接口与登录页'],
        steps: [{ text: '实现并联调', status: 'pending' }],
        verification: ['页面提交登录并核对会话'],
        workspace: 'D:/project',
        workspace_identity: [1, 2],
        status: 'pending',
      },
    ],
  };
}

it('完整展示计划，只有明确点击才批准具体版本', async () => {
  const user = userEvent.setup();
  const onDecision = vi.fn();
  render(
    <PlanningPanel runId="run-a" state={pendingPlan()} disabled={false} onDecision={onDecision} />,
  );
  expect(screen.getByText('认证接口与登录页')).toBeVisible();
  expect(screen.getByText('页面提交登录并核对会话')).toBeVisible();
  expect(onDecision).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '批准并执行' }));
  expect(onDecision).toHaveBeenCalledExactlyOnceWith('approve', '批准执行计划 v1');
});

it('修改表单按需展开，意见与拒绝使用不同动作且不能由文字触发批准', async () => {
  const user = userEvent.setup();
  const onDecision = vi.fn();
  render(
    <PlanningPanel runId="run-a" state={pendingPlan()} disabled={false} onDecision={onDecision} />,
  );
  expect(screen.queryByRole('textbox', { name: '计划反馈' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '要求修改' }));
  expect(onDecision).not.toHaveBeenCalled();
  expect(screen.getByRole('textbox', { name: '计划反馈' })).toHaveFocus();
  expect(screen.getByRole('button', { name: '提交修改意见' })).toBeDisabled();
  await user.type(screen.getByRole('textbox', { name: '计划反馈' }), '补充退出登录流程');
  expect(onDecision).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '提交修改意见' }));
  expect(onDecision).toHaveBeenLastCalledWith('revise', '补充退出登录流程');
  await user.click(screen.getByRole('button', { name: '返回计划' }));
  await user.click(screen.getByRole('button', { name: '拒绝计划' }));
  expect(onDecision).toHaveBeenLastCalledWith('reject', '补充退出登录流程');
});

it('问题允许选项或自由回答，并且不会发送批准操作', async () => {
  const user = userEvent.setup();
  const onDecision = vi.fn();
  const state = pendingPlan();
  state.pending = {
    id: 'question',
    kind: 'question',
    question: '选择登录方式？',
    options: ['密码', '验证码'],
    plan_version: null,
  };
  render(<PlanningPanel runId="run-a" state={state} disabled={false} onDecision={onDecision} />);
  expect(screen.getByRole('button', { name: '回答并继续' })).toBeDisabled();
  await user.click(screen.getByRole('radio', { name: '验证码' }));
  await user.click(screen.getByRole('button', { name: '回答并继续' }));
  expect(onDecision).toHaveBeenCalledExactlyOnceWith('answer', '验证码');
  expect(screen.queryByRole('button', { name: '批准并执行' })).not.toBeInTheDocument();
});

it('历史展示和不确定发送期间均不能批准或拒绝', () => {
  const state = pendingPlan();
  const { rerender } = render(<PlanningPanel runId="run-a" state={state} />);
  expect(screen.getByText('实现登录功能')).toBeVisible();
  expect(screen.queryByRole('button', { name: '批准并执行' })).not.toBeInTheDocument();
  rerender(<PlanningPanel runId="run-a" state={state} disabled onDecision={vi.fn()} />);
  expect(screen.getByRole('button', { name: '批准并执行' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '拒绝计划' })).toBeDisabled();
});

it('批准后收起长文并展示真实步骤进度，展开不会发送请求或推断验收通过', async () => {
  const user = userEvent.setup();
  const state = pendingPlan();
  state.phase = 'execution';
  state.pending = null;
  state.plans[0].status = 'approved';
  state.plans[0].steps = [
    { text: '实现接口', status: 'completed' },
    { text: '联调登录页面', status: 'in_progress' },
  ];
  const onDecision = vi.fn();
  const { rerender } = render(
    <PlanningPanel runId="run-a" state={state} onDecision={onDecision} />,
  );
  expect(screen.getByText('已完成 1 / 2 步')).toBeVisible();
  expect(screen.getByRole('progressbar', { name: '计划步骤进度' })).toHaveAttribute('value', '1');
  expect(screen.getByText('认证接口与登录页')).not.toBeVisible();
  expect(screen.getByText('联调登录页面', { selector: '.plan-current' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: '查看计划' }));
  expect(screen.getByText('认证接口与登录页')).toBeVisible();
  expect(onDecision).not.toHaveBeenCalled();
  // 同一版本的进度更新保留阅读状态，不把“步骤完成”显示成独立验收成功。
  const complete = structuredClone(state);
  complete.plans[0].steps[1].status = 'completed';
  rerender(<PlanningPanel runId="run-a" state={complete} onDecision={onDecision} />);
  expect(screen.getByText('认证接口与登录页')).toBeVisible();
  expect(screen.getByText('已完成 2 / 2 步')).toBeVisible();
  expect(screen.queryByText('已验收')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '批准并执行' })).not.toBeInTheDocument();
});

it('新版本恢复审阅状态并清空上一请求的修改草稿，历史版本没有批准入口', async () => {
  const user = userEvent.setup();
  const state = pendingPlan();
  const onDecision = vi.fn();
  const { rerender } = render(
    <PlanningPanel runId="run-a" state={state} disabled={false} onDecision={onDecision} />,
  );
  await user.click(screen.getByRole('button', { name: '收起计划' }));
  await user.click(screen.getByRole('button', { name: '要求修改' }));
  await user.type(screen.getByRole('textbox', { name: '计划反馈' }), '旧版修改意见');
  const next = structuredClone(state);
  next.plans[0].status = 'superseded';
  next.plans.push({ ...structuredClone(state.plans[0]), version: 2, title: '登录与退出登录' });
  next.pending = { ...next.pending!, id: 'request-two', plan_version: 2 };
  rerender(<PlanningPanel runId="run-a" state={next} disabled={false} onDecision={onDecision} />);
  expect(screen.getByRole('button', { name: '收起计划' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.queryByRole('textbox', { name: '计划反馈' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '要求修改' }));
  expect(screen.getByRole('textbox', { name: '计划反馈' })).toHaveValue('');
  await user.click(screen.getByRole('button', { name: '返回计划' }));
  await user.click(screen.getByRole('button', { name: '批准并执行' }));
  expect(onDecision).toHaveBeenCalledExactlyOnceWith('approve', '批准执行计划 v2');
});

it('问题按 Markdown 阅读，发送未确认时锁定选项和答案，新问题不继承旧答案', async () => {
  const user = userEvent.setup();
  const state = pendingPlan();
  state.plans = [];
  state.pending = {
    id: 'question-one',
    kind: 'question',
    question: '请选择方案。\n\n**需要兼顾手机。**',
    options: ['方案 A', '方案 B'],
    plan_version: null,
  };
  const onDecision = vi.fn();
  const { rerender } = render(
    <PlanningPanel runId="run-a" state={state} disabled={false} onDecision={onDecision} />,
  );
  await user.click(screen.getByRole('radio', { name: '方案 A' }));
  rerender(<PlanningPanel runId="run-a" state={state} disabled onDecision={onDecision} />);
  expect(screen.getByRole('textbox', { name: '回答问题' })).toBeDisabled();
  expect(screen.getByRole('radio', { name: '方案 A' })).toBeDisabled();
  expect(screen.getByText('需要兼顾手机。', { selector: 'strong' })).toBeVisible();
  const next = { ...state, pending: { ...state.pending, id: 'question-two' } };
  rerender(<PlanningPanel runId="run-a" state={next} disabled={false} onDecision={onDecision} />);
  expect(screen.getByRole('textbox', { name: '回答问题' })).toHaveValue('');
  expect(screen.getByRole('button', { name: '回答并继续' })).toBeDisabled();
  expect(onDecision).not.toHaveBeenCalled();
});

it('待批准编号与当前展示版本不一致时不提供执行入口', () => {
  const state = pendingPlan();
  state.pending!.plan_version = 2;
  render(<PlanningPanel runId="run-a" state={state} disabled={false} onDecision={vi.fn()} />);
  expect(screen.getByText('实现登录功能')).toBeVisible();
  expect(screen.queryByRole('button', { name: '批准并执行' })).not.toBeInTheDocument();
});

it('当前和历史 Markdown 导出绑定记录及版本，导出不触发审批回调', async () => {
  const user = userEvent.setup();
  const state = pendingPlan();
  state.plans[0].status = 'superseded';
  state.plans.push({
    ...structuredClone(state.plans[0]),
    version: 2,
    status: 'pending',
    title: '第二版登录方案',
  });
  state.pending!.plan_version = 2;
  const onDecision = vi.fn();
  render(
    <PlanningPanel runId="record-two" state={state} disabled={false} onDecision={onDecision} />,
  );
  expect(screen.getByRole('link', { name: '导出计划 v2 为 Markdown' })).toHaveAttribute(
    'href',
    '/api/runs/record-two/artifacts?location=run&path=plans%2Fv2.md',
  );
  await user.click(screen.getByText('历史版本', { selector: 'summary' }));
  await user.click(screen.getByText(/v1 · 实现登录功能/, { selector: 'summary' }));
  expect(screen.getByRole('link', { name: '导出历史计划 v1 为 Markdown' })).toHaveAttribute(
    'href',
    '/api/runs/record-two/artifacts?location=run&path=plans%2Fv1.md',
  );
  expect(onDecision).not.toHaveBeenCalled();
});
