import { render, screen, within } from '@testing-library/react';
import { expect, it } from 'vitest';
import type { VerificationResult } from '../types';
import { VerificationPanel } from './VerificationPanel';

/** 返回有固定流引用的真实形状检查；status决定断言结论，输出仅作文本展示。 */
function detail(status: 'passed' | 'failed'): VerificationResult {
  return {
    status,
    reason: status === 'passed' ? 'checks_passed' : 'assertion_failed',
    exit_code: status === 'passed' ? 0 : 1,
    stdout: status === 'passed' ? 'final passed' : 'first failed',
    stderr: '',
    timed_out: false,
    cancelled: false,
    output_limit_exceeded: false,
    cleanup_status: 'not_needed',
    elapsed_ms: 12,
    truncated: false,
    artifacts: [{ location: 'run', path: status + '/stdout.bin', kind: 'stdout' }],
  };
}

it('最终通过仍可核对首次失败及其独立下载', () => {
  // 当前检查与历史失败分开，不能只留下最后的绿色状态。
  const passed = detail('passed');
  render(
    <VerificationPanel
      runId="run-a"
      detail={passed}
      active={false}
      project
      repairAttempts={1}
      attempts={[
        { repair_attempt: 0, result: detail('failed') },
        { repair_attempt: 1, result: passed },
      ]}
    />,
  );
  expect(screen.getByText('测试通过')).toBeInTheDocument();
  const history = screen.getByRole('region', { name: '历史检查' });
  expect(within(history).getByText('首次检查 · 测试未通过')).toBeInTheDocument();
  expect(within(history).getByText('first failed')).toBeInTheDocument();
  expect(within(history).getByRole('link', { name: '下载标准输出' })).toHaveAttribute(
    'href',
    expect.stringContaining('failed%2Fstdout.bin'),
  );
});

it.each([true, false])('修复中或停止后显示当前尚未检查，旧失败仍保留（active=%s）', (active) => {
  // 没有新测试就不能把旧检查套到当前代码上；停止后也不能声称本轮从未检查。
  render(
    <VerificationPanel
      runId="run-b"
      detail={null}
      active={active}
      project
      repairAttempts={1}
      attempts={[{ repair_attempt: 0, result: detail('failed') }]}
    />,
  );
  expect(screen.getByText('修复后的代码尚未取得新的检查结果。')).toBeInTheDocument();
  expect(
    screen.getByText(`${active ? '正在进行' : '已进行'}第 1 / 2 次自动修复`),
  ).toBeInTheDocument();
  expect(screen.queryByText('测试通过')).not.toBeInTheDocument();
});
