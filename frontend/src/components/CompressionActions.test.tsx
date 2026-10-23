import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import type { ContextView } from '../types';
import { CompressionActions } from './CompressionActions';

/** 构造可核对的持久摘要视图，模拟跨轮复用且本轮没有摘要调用。 */
function context(): ContextView {
  return {
    instruction: {
      status: 'missing',
      path: 'AGENTS.md',
      content: null,
      sha256: null,
      size_bytes: null,
      error_code: null,
    },
    last_check: null,
    serialization: 'json_utf8_v1',
    action_count: 1,
    summary_calls: 0,
    actions: [
      {
        kind: 'summary',
        model_turn: 1,
        before_bytes: 8192,
        after_bytes: 2048,
        source_start: 3,
        source_end: 7,
        source_sha256: 'a'.repeat(64),
        reference: 'ctx:00000000-0000-0000-0000-000000000001:' + 'a'.repeat(64),
        reason: 'inherited',
        summary: {
          goal: '修复接口<script>alert(1)</script>',
          decisions: ['保留兼容参数'],
          changes: ['api.py'],
          evidence: ['前轮测试通过，本轮待检查'],
          open_issues: ['边界情况待验证'],
          next_steps: ['重跑宿主测试'],
        },
      },
    ],
  };
}

it('展示真实压缩大小、来源与继承状态，模型摘要仅按纯文本展示', () => {
  // 已知来源可以展示，不把前轮通过结论转换成本轮验收通过。
  const { container } = render(<CompressionActions context={context()} />);
  expect(screen.getByText(/8 KiB → 2 KiB/)).toBeInTheDocument();
  expect(screen.getByText(/摘要调用 0 次/)).toBeInTheDocument();
  expect(screen.getByText(/复用前轮摘要/)).toBeInTheDocument();
  expect(screen.getByText('前轮测试通过，本轮待检查')).toBeInTheDocument();
  expect(screen.getByText('修复接口<script>alert(1)</script>')).toBeInTheDocument();
  expect(container.querySelector('script')).toBeNull();
  expect(screen.getByText(/原始消息范围/)).toHaveTextContent('[3, 7)');
});

it('摘要失败显示原因且不展示伪造摘要，旧记录缺少事件时保持未记录', () => {
  // 同一视图重新渲染，验证失败与历史兼容分支。
  const value = context();
  value.actions![0] = {
    ...value.actions![0],
    kind: 'summary_failed',
    after_bytes: 8192,
    summary: null,
    reason: 'context_summary_invalid',
  };
  const { rerender } = render(<CompressionActions context={value} />);
  expect(screen.getByText('摘要未采用')).toBeInTheDocument();
  expect(screen.getByText(/context_summary_invalid/)).toBeInTheDocument();
  expect(screen.queryByText('已有决策')).not.toBeInTheDocument();
  rerender(
    <CompressionActions
      context={{ ...value, actions: undefined, action_count: undefined, summary_calls: undefined }}
    />,
  );
  expect(screen.getByText('此记录没有可展示的压缩事件。')).toBeInTheDocument();
});
