import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import type { LifecycleStageTrace } from '../lifecycle';
import { LifecycleDiagnostics } from './LifecycleDiagnostics';

it('显示实际执行来源并以最新轨迹覆盖同一阶段，保留窗口外阶段', () => {
  // 启动诊断已离开实时事件窗口，仍可从服务器快照查看。
  const snapshot: LifecycleStageTrace[] = [
    {
      stage: 'start',
      seq: 8,
      components: [
        { name: 'permission.resume', source: 'agent_harness.runtime.resume', outcome: 'proceed' },
      ],
    },
    {
      stage: 'before_finish',
      seq: 12,
      components: [
        { name: 'hooks.stop', source: 'agent_harness.runtime.background', outcome: 'replan' },
      ],
    },
  ];
  const live: LifecycleStageTrace[] = [
    {
      stage: 'before_finish',
      seq: 450,
      components: [
        {
          name: 'hooks.stop',
          source: 'agent_harness.runtime.background',
          outcome: 'wait',
          reason: 'action_approval',
        },
      ],
    },
  ];
  render(<LifecycleDiagnostics snapshot={snapshot} live={live} />);
  expect(screen.getByText('启动与接续 · #8')).toBeInTheDocument();
  expect(screen.getByText('agent_harness.runtime.resume')).toBeInTheDocument();
  expect(screen.getByText('交付前检查 · #450')).toBeInTheDocument();
  expect(screen.getByText(/等待动作审批/)).toBeInTheDocument();
  expect(screen.queryByText(/重新决策/)).not.toBeInTheDocument();
});
