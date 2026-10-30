import { Tag } from 'antd';
import { reviewLabels } from '../format';
import type {
  CleanupStatus,
  ExecutionStatus,
  RunView,
  RunResult,
  ValidationStatus,
} from '../types';

const executionLabels: Record<ExecutionStatus, string> = {
  created: '待执行',
  running: '执行中',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
  timed_out: '已超时',
  limit_exceeded: '预算耗尽',
  waiting_input: '等待你的决定',
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

/** 列表与完整运行共用相同状态规则，项目布尔值仅控制测试文案。 */
export interface RunStatusSnapshot {
  active: boolean; // 服务是否实际持有任务。
  cancelling: boolean; // 是否等待取消收尾。
  interrupted: boolean; // 是否缺少可信终态。
  phase: string; // 最新真实执行阶段。
  project?: RunView['project'] | boolean; // 完整审阅或只读的项目类型标记。
  source_kind?: 'project' | 'workspace' | null; // 原目录Git也显示项目测试，且不伪造待审阅。
  result: Pick<RunResult, 'execution_status' | 'validation_status' | 'cleanup_status'> &
    Partial<Pick<RunResult, 'repair_attempts'>>; // 列表只有三项状态，完整快照还可报告修复次数。
}

/** 根据真实终态生成概览，active/中断/清理异常优先，completed 本身不等于成功。 */
export function runSummary(run: RunStatusSnapshot): { label: string; color?: string } {
  const isProject = Boolean(run.project) || run.source_kind === 'project'; // 测试类型独立于工作树审阅。
  if (run.interrupted) return { label: '运行已中断', color: 'warning' };
  if (run.cancelling) return { label: '正在取消', color: 'processing' };
  if (run.active)
    return {
      label:
        run.phase === 'project_snapshot'
          ? '整理代码改动'
          : run.phase === 'verification'
            ? isProject
              ? '测试中'
              : '验收中'
            : run.result.execution_status === 'running' && run.result.repair_attempts
              ? `正在修复 ${run.result.repair_attempts}/2`
              : '运行中',
      color: 'processing',
    };
  if (['unknown', 'incomplete'].includes(run.result.cleanup_status))
    return { label: '需检查清理', color: 'warning' };
  if (run.phase === 'planning_complete') return { label: '规划已结束' };
  if (run.phase === 'plan_rejected') return { label: '已拒绝计划' };
  if (run.phase === 'user_question') return { label: '等待回答', color: 'warning' };
  if (run.phase === 'plan_approval') return { label: '等待批准', color: 'warning' };
  if (run.result.execution_status === 'completed') {
    const status = run.result.validation_status;
    return {
      label:
        status === 'unverified'
          ? isProject
            ? '完成 · 未测试'
            : '完成 · 未验收'
          : isProject
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
export function RunStatus({ run }: { run: RunStatusSnapshot }) {
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
        <dd>{run.interrupted ? '运行已中断' : executionLabels[run.result.execution_status]}</dd>
      </div>
      <div>
        <dt>{run.project || run.source_kind === 'project' ? '测试' : '验收'}</dt>
        <dd>
          {run.project || run.source_kind === 'project'
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
