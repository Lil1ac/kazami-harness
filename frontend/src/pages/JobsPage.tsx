import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Select } from 'antd';
import { Link, useNavigate, useParams } from 'react-router';
import { errorMessage, getJob, getJobs, jobAction, resumeQueue } from '../api';
import type { JobView } from '../types';
import '../components/TasksPanel.css';

const labels: Record<JobView['status'], string> = {
  queued: '排队中',
  preparing: '准备中',
  running: '运行中',
  waiting_input: '等待你的决定',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
  needs_review: '待核对',
}; // 执行与验收分别展示。

/** 返回job当前整理归属；首轮未建会话时使用冻结来源，移至未分组时不回退原项目。 */
function groupFor(job: JobView): { id: string; label: string } {
  if (job.assigned_project !== undefined)
    return job.assigned_project || { id: 'ungrouped', label: '未分组' };
  return { id: job.project_id, label: job.project_label };
}

/** 后台中心及作业深链接；只有已建立 Run 才跳转，排队中保持可取消入口。 */
export function JobsPage() {
  const { jobId } = useParams(); // 单个作业入口为空时展示全部。
  const navigate = useNavigate();
  const cache = useQueryClient();
  const [project, setProject] = useState<string | undefined>(); // 项目筛选，不改变队列顺序。
  const query = useQuery({ queryKey: ['jobs'], queryFn: getJobs, refetchInterval: 1000 });
  const detail = useQuery({
    queryKey: ['job', jobId],
    queryFn: /* 深链接不要求位于最近五百项内。 */ () => getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: 1000,
  });
  const selected = detail.data;
  useEffect(
    /* 真实 Run 建立后切入原会话界面，不重新投递。 */ () => {
      if (
        selected &&
        !['queued', 'preparing', 'needs_review'].includes(selected.status) &&
        selected.execution_status !== null
      )
        void navigate('/runs/' + selected.run_id, { replace: true });
    },
    [selected, navigate],
  );
  const mutation = useMutation({
    mutationFn: /* 每个按钮只执行明确动作。 */ async (value: {
      id?: string;
      action: 'cancel' | 'resolve' | 'resume';
    }) => {
      if (value.action === 'resume') await resumeQueue();
      else await jobAction(value.id!, value.action);
    },
    onSuccess: /* 以宿主快照重新渲染。 */ () => {
      void cache.invalidateQueries({ queryKey: ['jobs'] });
      void cache.invalidateQueries({ queryKey: ['job'] });
    },
  });
  const jobs = jobId ? (selected ? [selected] : []) : query.data?.items || [];
  return (
    <section className="page-section">
      <h1>后台工作</h1>
      {(query.error || detail.error || mutation.error) && (
        <Alert type="error" title={errorMessage(query.error || detail.error || mutation.error)} />
      )}
      {query.data?.paused && (
        <Alert
          type="info"
          title="队列已暂停"
          description="核对中断作业后，可恢复尚未执行的请求。"
          action={
            <Button
              onClick={/* 用户明确恢复排队工作。 */ () => mutation.mutate({ action: 'resume' })}
            >
              恢复队列
            </Button>
          }
        />
      )}
      {!jobId && (
        <Select
          allowClear
          placeholder="全部项目"
          aria-label="筛选项目"
          value={project}
          onChange={setProject}
          style={{ minWidth: 180 }}
          options={[
            ...new Set(jobs.map(/* 按会话当前整理归属筛选。 */ (job) => groupFor(job).id)),
          ].map(
            /* 同编号只展示一个分组，原始执行项目仍保存在job.project_id。 */ (id) => ({
              value: id,
              label: groupFor(jobs.find((job) => groupFor(job).id === id)!).label,
            }),
          )}
        />
      )}
      <ul className="jobs-list">
        {jobs
          .filter(/* 筛选不改变执行队列。 */ (job) => !project || groupFor(job).id === project)
          .map(
            /* 作业编号用于去重及深链接。 */ (job) => (
              <li key={job.id}>
                <strong>{job.conversation_title || job.title}</strong>
                <div>
                  <span>{groupFor(job).label}</span>
                  <span>
                    {job.status === 'waiting_input' && job.wait_reason === 'action_approval'
                      ? (
                          {
                            pending: '等待动作审批',
                            approved: '已批准，等待执行',
                            executing: '动作执行中',
                            unknown: '动作结果待核对',
                          } as Record<string, string>
                        )[job.approval_status || 'pending'] || '审批已处理'
                      : labels[job.status]}
                  </span>
                  <span>
                    {
                      {
                        unverified: '未测试',
                        passed: '检查通过',
                        failed: '检查失败',
                        error: '检查异常',
                      }[job.validation_status]
                    }
                  </span>
                </div>
                {job.error && <p role="status">{job.error}</p>}
                <div>
                  {!['queued', 'preparing', 'needs_review'].includes(job.status) &&
                    job.execution_status !== null && (
                      <Link to={'/runs/' + job.run_id}>查看会话与结果</Link>
                    )}
                  {['queued', 'running'].includes(job.status) && (
                    <Button
                      aria-label="停止"
                      loading={mutation.isPending}
                      onClick={
                        /* 停止排队或真实执行。 */ () =>
                          mutation.mutate({ id: job.id, action: 'cancel' })
                      }
                    >
                      停止
                    </Button>
                  )}
                  {job.status === 'needs_review' && (
                    <Button
                      onClick={
                        /* 按钮本身明确声明用户已完成核对；原请求不会重放。 */ () =>
                          mutation.mutate({ id: job.id, action: 'resolve' })
                      }
                    >
                      确认进程已停止且工作区已核对
                    </Button>
                  )}
                </div>
              </li>
            ),
          )}
      </ul>
      {!jobs.length && <p>暂无后台作业</p>}
    </section>
  );
}
