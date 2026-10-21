import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makeProject, makeRun } from '../test/fixtures';
import { RunStatus, StateBreakdown, runSummary } from './RunStatus';

describe('执行、验收与清理状态', /* 用户必须能够区分执行完成与内容通过。 */ () => {
  it('completed 且未验收时明确显示未验收', /* 禁止把模型完成当成通过。 */ () => {
    render(
      <>
        <RunStatus run={makeRun()} />
        <StateBreakdown run={makeRun()} />
      </>,
    );
    expect(screen.getByText('完成 · 未验收')).toBeInTheDocument();
    expect(screen.getByText('未验收')).toBeInTheDocument();
  });
  it('只有已结束、验收通过且清理安全才显示通过', /* 三个事实缺一不可。 */ () => {
    const run = makeRun();
    run.result.validation_status = 'passed';
    expect(runSummary(run).label).toBe('验收通过');
    run.active = true;
    run.phase = 'verification';
    expect(runSummary(run).label).toBe('验收中');
    run.active = false;
    run.result.cleanup_status = 'unknown';
    expect(runSummary(run).label).toBe('需检查清理');
  });
  it('中断和取消中的任务不伪装成成功', /* 服务器重启与清理期间需要明确说明。 */ () => {
    expect(runSummary(makeRun({ interrupted: true })).label).toBe('运行已中断');
    expect(runSummary(makeRun({ active: true, cancelling: true })).label).toBe('正在取消');
  });
});

it('项目测试通过后仍明确等待人工审阅', () => {
  // 测试和人工接受独立，不能用测试绿色结果覆盖未审阅状态。
  const run = makeRun({ project: makeProject() });
  run.result.validation_status = 'passed';
  render(
    <>
      <RunStatus run={run} />
      <StateBreakdown run={run} />
    </>,
  );
  expect(screen.getAllByText('测试通过')).toHaveLength(2);
  expect(screen.getByText('等待审阅')).toBeInTheDocument();
  expect(screen.getByText('审阅')).toBeInTheDocument();
  run.active = true;
  run.phase = 'project_snapshot';
  expect(runSummary(run).label).toBe('整理代码改动');
});
