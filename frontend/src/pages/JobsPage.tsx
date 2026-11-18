import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Select, Skeleton, Switch } from 'antd';
import { Link, useNavigate, useParams } from 'react-router';
import { errorMessage, getJob, getJobs, jobAction, recoverJob, resumeQueue, configureQueue } from '../api';
import { isPendingJob } from '../queue';
import type { JobView } from '../types';
import './JobsPage.css';

const labels: Record<JobView['status'], string> = {
  queued: '排队中',
  preparing: '准备中',
  running: '运行中',
  waiting_input: '等待你的决定',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
  needs_review: '待核对',
  interrupted: '运行中断',
}; // 队列状态来自宿主，验收结论在会话里查看。

const groups: { title: string; statuses: JobView['status'][] }[] = [
  { title: '需要处理', statuses: ['needs_review', 'waiting_input'] },
  { title: '运行中', statuses: ['preparing', 'running'] },
  { title: '排队', statuses: ['queued'] },
]; // 显示顺序优先处理阻塞，不改变调度顺序。

const approvals: Record<string, string> = {
  pending: '等待动作审批',
  approved: '已批准，等待执行',
  executing: '动作执行中',
  unknown: '动作结果待核对',
}; // 等待动作时显示当前审批状态，而非旧轮次的等待快照。

/** 返回作业 job 的当前项目归属；未分组的会话保持未分组，未建立的会话使用冻结来源。 */
function groupFor(job: JobView): { id: string; label: string } {
  if (job.assigned_project !== undefined)
    return job.assigned_project || { id: 'ungrouped', label: '未分组' };
  return { id: job.project_id, label: job.project_label };
}

/** 返回 job 已有的会话或成果入口；未建立运行或需先核对的作业返回空值。 */
function jobDestination(job: JobView): string | null {
  if (job.kind === 'memory_consolidation')
    return '/projects/' + encodeURIComponent(job.project_id) + '?memory=1&consolidation=' + encodeURIComponent(job.id);
  if (job.status === 'interrupted' && job.recovery?.next_job_id)
    return '/jobs/' + job.recovery.next_job_id;
  if (['queued', 'preparing', 'needs_review'].includes(job.status) || job.execution_status === null)
    return null;
  const run = '/runs/' + job.run_id;
  if (job.status === 'waiting_input' && job.wait_reason !== 'user_paused' && job.kind !== 'integration')
    return run + '?decision=' + encodeURIComponent(job.run_id);
  return job.kind === 'integration'
    ? run + '?tab=files&integration=' + (job.result_integration_id || job.integration_id)
    : run;
}

/** 返回 job 的可读状态；动作审批与用户主动暂停单独说明。 */
function jobStatus(job: JobView): string {
  if (job.recovery?.state === 'ready') return '可以接续';
  if (job.recovery?.state === 'queued') return '接续已排队';
  if (job.kind === 'memory_consolidation' && job.status === 'completed')
    return ({ draft: '待审阅', no_changes: '没有新变更', published: '已发布', rejected: '已拒绝' } as Record<string, string>)[job.memory_state || ''] || '整理完成';
  if (job.wait_reason === 'user_paused') return '已暂停';
  if (job.status === 'waiting_input' && job.wait_reason === 'action_approval')
    return approvals[job.approval_status || 'pending'] || '审批已处理';
  return labels[job.status];
}

type QueueAction =
  | { action: 'resume' } // 恢复全局暂停的队列。
  | { action: 'recover'; id: string; revision: string } // 明确接续可核对的中断作业。
  | { action: 'cancel' | 'resolve'; id: string }; // 对一个已接纳的作业停止或确认核对。

/** 返回一行作业摘要；停止与核对操作通过 onAction 提交当前编号。 */
function JobRow({
  job,
  busy,
  onAction,
}: {
  job: JobView; // 宿主的公开作业快照。
  busy: boolean; // 当前行是否在提交操作。
  onAction: (value: QueueAction) => void; // 操作入口，点击后才写入。
}) {
  const destination = jobDestination(job);
  const canStop = job.status === 'queued' || job.status === 'running' || (job.status === 'preparing' && job.kind === 'memory_consolidation');
  const needsReview = job.status === 'needs_review';
  const reviewFiles = needsReview && job.kind === 'integration' && job.recovery_kind !== 'process';
  const recovery = job.recovery;
  return (
    <li className="queue-row">
      <div className="queue-row-main">
        <div className="queue-row-heading">
          <strong>{job.conversation_title || job.title}</strong>
          <span className="queue-state" data-status={job.status}>
            {jobStatus(job)}
          </span>
        </div>
        <div className="queue-row-meta">
          <span>{groupFor(job).label}</span>
          {job.automation && <Link to="/automations">来自自动化</Link>}
          {job.kind === 'memory_consolidation' && job.memory_usage?.total_tokens != null &&
            <span>{job.memory_usage_complete ? '' : '至少 '}{job.memory_usage.total_tokens.toLocaleString('zh-CN')} token</span>}
        </div>
        {(recovery?.reason || job.error) && (
          <p className="queue-row-note" role="status">
            {recovery?.reason || job.error}
          </p>
        )}
        {recovery?.state === 'ready' && (
          <p className="queue-row-note">
            {recovery.recovered_writes ? `已核对 ${recovery.recovered_writes} 次文件写入 · ` : ''}
            剩余 {recovery.remaining_model_turns} 轮模型、{recovery.remaining_tool_calls} 次工具、
            {Math.floor(recovery.remaining_seconds ?? 0)} 秒
            {recovery.usage_complete === false ? ' · 原用量不完整' : ''}
          </p>
        )}
        {job.status === 'queued' && job.blocked_reason && (
          <p className="queue-row-note" role="status">
            {job.blocked_reason}
            {job.blocked_by && (
              <>
                {' '}
                · <Link to={'/jobs/' + job.blocked_by}>查看占用工作</Link>
              </>
            )}
          </p>
        )}
      </div>
      {(destination || needsReview || canStop || recovery?.next_job_id) && (
        <div className="queue-row-actions">
          {destination && (
            <Link to={destination}>
              {job.kind === 'memory_consolidation' ? '查看记忆整理' : job.kind === 'integration'
                ? '查看成果采纳'
                : job.status === 'waiting_input' && job.wait_reason !== 'user_paused'
                  ? '前往会话处理'
                  : '查看会话与结果'}
            </Link>
          )}
          {needsReview && recovery && (
            <Link to={'/runs/' + job.run_id + '?inspect=' + job.run_id}>查看中断记录</Link>
          )}
          {recovery?.state === 'ready' && recovery.revision && (
            <Button
              size="small"
              type="primary"
              loading={busy}
              onClick={/* 仅明确点击才向宿主提交该版本。 */ () =>
                onAction({ id: job.id, action: 'recover', revision: recovery.revision! })}
            >
              继续中断的运行
            </Button>
          )}
          {needsReview && recovery?.next_job_id && (
            <Link to={'/jobs/' + recovery.next_job_id}>查看接续作业</Link>
          )}
          {canStop && (
            <Button
              size="small"
              autoInsertSpace={false}
              loading={busy}
              onClick={/* 停止排队或真实执行。 */ () => onAction({ id: job.id, action: 'cancel' })}
            >
              停止
            </Button>
          )}
          {reviewFiles && (
            <Link to={'/runs/' + job.run_id + '?tab=files&integration=' + job.integration_id}>
              核对逐文件写回结果
            </Link>
          )}
          {needsReview && !reviewFiles && recovery?.state !== 'queued' && (
            <Button
              size="small"
              loading={busy}
              onClick={
                /* 显式确认核对后解除阻塞，原请求不会重放。 */ () =>
                  onAction({ id: job.id, action: 'resolve' })
              }
            >
              确认进程已停止且工作区已核对
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

/** 显示运行队列或单个作业；已建立运行的深链接交给会话，保留队列取消与异常核对入口。 */
export function JobsPage() {
  const { jobId } = useParams(); // 单个作业入口为空时浏览队列。
  const navigate = useNavigate();
  const cache = useQueryClient();
  const recoveryRequests = useRef(new Map<string, string>()); // 网络重试沿用同作业和恢复版本的请求编号。
  const [project, setProject] = useState<string | undefined>(); // 仅筛选列表，不改变队列顺序。
  const query = useQuery({
    queryKey: ['jobs'],
    queryFn: getJobs,
    refetchInterval: jobId ? false : 1000,
  });
  const detail = useQuery({
    queryKey: ['job', jobId],
    queryFn: /* 深链接不要求位于最近五百项内。 */ () => getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: 1000,
  });
  const destination = detail.data && jobDestination(detail.data);
  useEffect(
    /* 真实运行建立后进入相应会话或成果，不重新投递。 */ () => {
      if (jobId && destination) void navigate(destination, { replace: true });
    },
    [jobId, destination, navigate],
  );
  const mutation = useMutation({
    mutationFn: /* 每个按钮只执行其明确的动作。 */ async (value: QueueAction) => {
      if (value.action === 'resume') {
        await resumeQueue();
      } else if (value.action === 'recover') {
        const key = value.id + ':' + value.revision;
        const id = recoveryRequests.current.get(key) || crypto.randomUUID();
        recoveryRequests.current.set(key, id);
        return recoverJob(value.id, id, value.revision);
      } else {
        await jobAction(value.id, value.action);
      }
    },
    onSuccess: /* 接纳成功先打开后继作业，Run 建立后由既有详情逻辑跳转。 */ (job) => {
      if (job) {
        recoveryRequests.current.clear();
        void navigate('/jobs/' + job.id);
      }
      void cache.invalidateQueries({ queryKey: ['jobs'] });
      void cache.invalidateQueries({ queryKey: ['job'] });
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
    onError: /* 状态可能已被另一个页面接纳，刷新宿主证据。 */ () => {
      void cache.invalidateQueries({ queryKey: ['jobs'] });
      void cache.invalidateQueries({ queryKey: ['job'] });
    },
  });
  const setting = useMutation({
    mutationFn: configureQueue,
    onSuccess: /* 保存成功后读取宿主持久状态。 */ () => {
      void cache.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
  const items = query.data?.items || [];
  const standalone = items.filter(
    /* 有会话或成果详情的终态结果归回原页面，保留没有运行的历史。 */ (job) =>
      isPendingJob(job.status) || job.execution_status === null,
  );
  const projects = new Map(
    standalone.map(
      /* 相同编号只展示一个项目。 */ (job) => {
        const { id, label } = groupFor(job);
        return [id, label];
      },
    ),
  );
  const visible = standalone.filter(
    /* 项目已离开队列时自动回到全部范围。 */ (job) =>
      !project || !projects.has(project) || groupFor(job).id === project,
  );
  const pending = visible.filter(/* 优先列出需要跟进的作业。 */ (job) => isPendingJob(job.status));
  const history = visible.filter(
    /* 只有没有运行详情的终态作业进入折叠区。 */ (job) => !isPendingJob(job.status),
  );
  const error = detail.error || query.error || mutation.error || setting.error;

  /** 渲染 row 的操作与状态；仅本次变更指向该编号时显示提交状态。 */
  function renderJob(row: JobView) {
    return (
      <JobRow
        key={row.id}
        job={row}
        busy={
          mutation.isPending &&
          mutation.variables?.action !== 'resume' &&
          mutation.variables?.id === row.id
        }
        onAction={mutation.mutate}
      />
    );
  }

  return (
    <section className="queue-page">
      <header className="queue-heading">
        <div>
          {jobId && (
            <Link className="queue-back" to="/jobs">
              返回运行队列
            </Link>
          )}
          <h1>{jobId ? '作业详情' : '运行队列'}</h1>
        </div>
        {!jobId && projects.size > 1 && (
          <Select
            allowClear
            placeholder="全部项目"
            aria-label="筛选项目"
            value={project && projects.has(project) ? project : undefined}
            onChange={setProject}
            options={Array.from(
              projects,
              /* 项目编号用于筛选，标签供用户识别。 */ ([value, label]) => ({ value, label }),
            )}
          />
        )}
      </header>
      {error && <Alert type="error" showIcon title={errorMessage(error)} />}
      {query.data?.paused && (
        <Alert
          type="warning"
          showIcon
          title="队列已暂停"
          action={
            <Button
              size="small"
              loading={mutation.isPending && mutation.variables?.action === 'resume'}
              onClick={/* 用户明确恢复排队工作。 */ () => mutation.mutate({ action: 'resume' })}
            >
              恢复队列
            </Button>
          }
        />
      )}
      {jobId ? (
        detail.data ? (
          <ul className="queue-list">{renderJob(detail.data)}</ul>
        ) : (
          detail.isLoading && <Skeleton active paragraph={{ rows: 2 }} />
        )
      ) : (
        <>
          {query.isPending && <Skeleton active paragraph={{ rows: 2 }} />}
          {query.data && !pending.length && <p className="queue-empty">暂无运行或待处理的作业</p>}
          {groups.map(
            /* 只渲染当前范围内有作业的组。 */ ({ title, statuses }) => {
              const jobs = pending.filter(
                /* 每种状态只属于一个组。 */ (job) => statuses.includes(job.status),
              );
              return (
                jobs.length > 0 && (
                  <section className="queue-group" key={title}>
                    <h2>
                      {title} <span>{jobs.length}</span>
                    </h2>
                    <ul className="queue-list">{jobs.map(renderJob)}</ul>
                  </section>
                )
              );
            },
          )}
          {history.length > 0 && (
            <details className="queue-disclosure">
              <summary>
                启动前记录 <span>{history.length}</span>
              </summary>
              <ul className="queue-list">{history.map(renderJob)}</ul>
            </details>
          )}
          {query.data?.scheduler && (
            <details className="queue-disclosure">
              <summary>队列设置</summary>
              <div className="queue-settings">
                <span>
                  运行 {query.data.scheduler.active_jobs.length} /{' '}
                  {query.data.scheduler.max_concurrent_runs}
                </span>
                <label>
                  <Switch
                    size="small"
                    aria-label="启动后继续未开始的队列"
                    checked={query.data.scheduler.resume_on_start}
                    loading={setting.isPending}
                    onChange={/* 只保存用户明确选择。 */ (checked) => setting.mutate(checked)}
                  />
                  启动后继续未开始的队列
                </label>
              </div>
            </details>
          )}
        </>
      )}
    </section>
  );
}
