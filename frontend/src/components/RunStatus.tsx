import { Tag } from 'antd';
import { reviewLabels } from '../format';
import type { CleanupStatus, ExecutionStatus, RunView, ValidationStatus } from '../types';

const executionLabels: Record<ExecutionStatus, string> = {
  created: '待执行',
  running: '执行中',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
  timed_out: '已超时',
  limit_exceeded: '预算耗尽',
};
/** 普通工作区独立验收的稳定状态文案。 */
const validationLabels: Record<ValidationStatus, string> = {
  unverified: '未验收',
  passed: '验收通过',
  failed: '验收未通过',
  error: '验收异常',
};
/** 项目测试的独立文案；通过测试不代表人工已经接受代码改动。 */
const testLabels: Record<ValidationStatus, string> = {
  unverified: '未测试',
  passed: '测试通过',
  failed: '测试未通过',
  error: '测试异常',
};
const cleanupLabels: Record<CleanupStatus, string> = {
  not_needed: '无需清理',
  complete: '清理完成',
  incomplete: '清理不完整',
  unknown: '清理未确认',
};

/** 根据真实终态生成概览，active/中断/清理异常优先，completed 本身不等于成功。 */
export function runSummary(run: RunView): { label: string; color?: string } {
  if (run.interrupted) return { label: '运行已中断', color: 'warning' };
  if (run.cancelling) return { label: '正在取消', color: 'processing' };
  if (run.active)
    return {
      label:
        run.phase === 'project_snapshot'
          ? '整理代码改动'
          : run.phase === 'verification'
            ? run.project
              ? '测试中'
              : '验收中'
            : '运行中',
      color: 'processing',
    };
  if (['unknown', 'incomplete'].includes(run.result.cleanup_status))
    return { label: '需检查清理', color: 'warning' };
  if (run.result.execution_status === 'completed') {
    const status = run.result.validation_status;
    return {
      label:
        status === 'unverified'
          ? run.project
            ? '完成 · 未测试'
            : '完成 · 未验收'
          : run.project
            ? testLabels[status]
            : validationLabels[status],
      color: status === 'passed' ? 'success' : status === 'unverified' ? undefined : 'error',
    };
  }
  return {
    label: executionLabels[run.result.execution_status],
    color: ['failed', 'timed_out', 'limit_exceeded'].includes(run.result.execution_status)
      ? 'error'
      : undefined,
  };
}

/** 展示概览标签；run 为服务端快照，不根据模型最终回答推断成功。 */
export function RunStatus({ run }: { run: RunView }) {
  const status = runSummary(run);
  return (
    <Tag color={status.color} variant="filled">
      {status.label}
    </Tag>
  );
}

/** 分别展示执行、验收或测试、人工审阅和清理；项目审阅不会覆盖执行或测试事实。 */
export function StateBreakdown({ run }: { run: RunView }) {
  return (
    <dl className={`state-breakdown${run.project ? ' project-state' : ''}`}>
      <div>
        <dt>执行</dt>
        <dd>{executionLabels[run.result.execution_status]}</dd>
      </div>
      <div>
        <dt>{run.project ? '测试' : '验收'}</dt>
        <dd>
          {run.project
            ? testLabels[run.result.validation_status]
            : validationLabels[run.result.validation_status]}
        </dd>
      </div>
      {run.project && (
        <div>
          <dt>审阅</dt>
          <dd>{reviewLabels[run.project.status]}</dd>
        </div>
      )}
      <div>
        <dt>清理</dt>
        <dd>{cleanupLabels[run.result.cleanup_status]}</dd>
      </div>
    </dl>
  );
}
