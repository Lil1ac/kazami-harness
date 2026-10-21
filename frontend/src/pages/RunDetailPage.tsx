import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Popconfirm, Skeleton, Tabs } from 'antd';
import { ArrowLeftOutlined, StopOutlined } from '@ant-design/icons';
import { Link, useParams, useSearchParams } from 'react-router';
import { cancelRun, errorMessage, getRun } from '../api';
import { duration, phaseLabel } from '../format';
import { useRunEvents } from '../hooks/useRunEvents';
import { RunStatus, StateBreakdown } from '../components/RunStatus';
import { EventTrail } from '../components/EventTrail';
import { ArtifactPanel } from '../components/ArtifactPanel';
import { VerificationPanel } from '../components/VerificationPanel';
import { UsageDisplay } from '../components/UsageDisplay';
import { ProjectReviewPanel } from '../components/ProjectReviewPanel';
import type { RunView } from '../types';

/** 任务详情路由入口；不存在或不可读的记录明确报错，不创建虚构结果。 */
export function RunDetailPage() {
  const { runId = '' } = useParams();
  const query = useQuery({
    queryKey: ['run', runId],
    queryFn: /* 使用单次快照读取，刷新网页不会启动任务。 */ ({ signal }) => getRun(runId, signal),
    retry: false,
    refetchInterval: /* 有活动任务时更新阶段和计数，终态停止轮询。 */ (state) =>
      state.state.data?.active ? 1500 : false,
  });
  if (query.isPending) return <Skeleton active paragraph={{ rows: 12 }} />;
  if (!query.data)
    return (
      <>
        <Link to="/runs">返回任务列表</Link>
        <Alert type="error" showIcon title="无法打开任务" description={errorMessage(query.error)} />
      </>
    );
  return (
    <>
      {query.error && (
        <Alert
          type="warning"
          showIcon
          title="暂时无法刷新任务，保留上次快照"
          description={errorMessage(query.error)}
        />
      )}
      <RunDetailContent key={runId} run={query.data} />
    </>
  );
}

/** 根据任务快照显示轨迹和检查器；切换任务通过 key 完全隔离预览与订阅状态。 */
function RunDetailContent({ run }: { run: RunView }) {
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const tab = ['trace', 'answer', 'artifacts', ...(run.project ? ['code'] : [])].includes(
    params.get('tab') || '',
  )
    ? params.get('tab')!
    : 'trace';
  const [now, setNow] = useState(Date.now()); // 仅用于活动任务耗时展示，不推断终态。
  const events = useRunEvents(
    run.run_id,
    run.active,
    /* 终态通知后重新查询持久化结果并刷新列表。 */ () => {
      void cache.invalidateQueries({ queryKey: ['run', run.run_id] });
      void cache.invalidateQueries({ queryKey: ['runs'] });
      void cache.invalidateQueries({ queryKey: ['options'] });
    },
  );
  const cancellation = useMutation({
    mutationFn: /* 明确的用户操作才发送取消请求。 */ () => cancelRun(run.run_id),
    onSuccess: /* 返回值可能仍在清理，继续等待真实终态。 */ (view) =>
      cache.setQueryData(['run', run.run_id], view),
  });
  useEffect(
    /* 活动阶段每秒更新展示，页面离开后停止定时器。 */ () => {
      if (!run.active) return;
      const timer = setInterval(
        /* 只更新浏览器时钟，不改变运行状态。 */ () => setNow(Date.now()),
        1000,
      );
      return /* effect 清理不取消后端任务。 */ () => clearInterval(timer);
    },
    [run.active],
  );
  const eventElapsed = events.events.at(-1)?.elapsed_ms ?? 0;
  const elapsed = run.active
    ? Math.max(eventElapsed, now - Date.parse(run.created_at))
    : eventElapsed;
  const limits = run.config.limits;
  const cancelling = run.cancelling || cancellation.isPending;

  /** 将当前详情标签写入 URL，事件订阅始终只有上面的一个实例。 */
  function changeTab(value: string): void {
    const next = new URLSearchParams(params);
    next.set('tab', value);
    setParams(next, { replace: true });
  }

  return (
    <>
      <Link to="/runs" className="back-link">
        <ArrowLeftOutlined /> 任务列表
      </Link>
      <header className="page-heading detail-heading">
        <div>
          <span className="eyebrow">RUN / {run.run_id.slice(0, 8)}</span>
          <h1>
            运行详情 <RunStatus run={run} />
          </h1>
          <p className="run-full-id">{run.run_id}</p>
        </div>
        <Popconfirm
          title="取消当前任务？"
          description="后端会停止执行并完成进程清理，已有记录仍会保留。"
          okText="取消任务"
          cancelText="继续运行"
          onConfirm={/* 用户确认后触发幂等取消接口。 */ () => cancellation.mutate()}
          disabled={!run.active || cancelling}
        >
          <Button
            danger
            disabled={!run.active || cancelling}
            loading={cancelling}
            icon={<StopOutlined />}
          >
            {cancelling ? '正在取消' : '取消任务'}
          </Button>
        </Popconfirm>
      </header>
      {cancellation.error && (
        <Alert
          type="error"
          showIcon
          title="取消请求未完成"
          description={errorMessage(cancellation.error)}
        />
      )}
      {run.interrupted && (
        <Alert
          type="warning"
          showIcon
          title="任务运行已中断"
          description="后台没有对应的活动任务，当前记录不代表执行成功。请检查已有产物和事件，再决定是否重新提交。"
        />
      )}
      {run.warnings.map(
        /* 宿主提示按安全文本显示。 */ (warning, index) => (
          <Alert key={index} type="warning" showIcon title={warning} />
        ),
      )}
      <div className="task-prompt">
        <span className="eyebrow">TASK</span>
        <p>{run.prompt}</p>
        <span className="workspace-path">{run.workspace}</span>
      </div>
      <div className="run-metrics">
        <div>
          <span>当前阶段</span>
          <strong>
            {run.interrupted ? '运行中断' : cancelling ? '正在取消与清理' : phaseLabel(run.phase)}
          </strong>
        </div>
        <div>
          <span>耗时 / 总预算</span>
          <strong>
            {duration(elapsed)} <small>/ {limits.run_timeout_seconds} 秒</small>
          </strong>
        </div>
        <div>
          <span>模型轮次 / 上限</span>
          <strong>
            {run.result.model_turns} <small>/ {limits.max_model_turns}</small>
          </strong>
        </div>
        <div>
          <span>HTTP 尝试</span>
          <strong>{run.result.http_attempts}</strong>
        </div>
        <div>
          <span>工具调用 / 上限</span>
          <strong>
            {run.result.tool_calls} <small>/ {limits.max_tool_calls}</small>
          </strong>
        </div>
      </div>
      <div className="detail-layout">
        <div className="detail-main">
          <Tabs
            activeKey={tab}
            onChange={changeTab}
            items={[
              ...(run.project
                ? [
                    {
                      key: 'code',
                      label: `代码改动 · ${run.project.files.length}`,
                      children: <ProjectReviewPanel project={run.project} active={run.active} />,
                    },
                  ]
                : []),
              {
                key: 'trace',
                label: `执行轨迹 · ${run.event_count}`,
                children: <EventTrail {...events} />,
              },
              {
                key: 'answer',
                label: '最终回答',
                children: (
                  <section>
                    <h2>最终回答</h2>
                    {run.result.final_answer ? (
                      <pre className="answer-text">{run.result.final_answer}</pre>
                    ) : (
                      <p className="muted">尚无最终回答。执行中的模型消息可在轨迹查看。</p>
                    )}
                  </section>
                ),
              },
              {
                key: 'artifacts',
                label: `产物 · ${run.result.artifacts.length}`,
                children: (
                  <ArtifactPanel
                    runId={run.run_id}
                    artifacts={run.result.artifacts}
                    missing={run.result.missing_artifacts}
                    workspaceUnavailable={run.project?.status === 'discarded'}
                  />
                ),
              },
            ]}
          />
        </div>
        <aside className="run-inspector">
          <section>
            <span className="eyebrow">RUN STATE</span>
            <h2>运行状态</h2>
            <StateBreakdown run={run} />
            {run.result.stop_reason && (
              <p className="stop-reason">
                结束原因 <code>{run.result.stop_reason}</code>
              </p>
            )}
          </section>
          <section>
            <span className="eyebrow">REMAINING BUDGET</span>
            <h2>剩余预算</h2>
            <dl className="compact-dl">
              <div>
                <dt>模型轮次</dt>
                <dd>{Math.max(0, limits.max_model_turns - run.result.model_turns)}</dd>
              </div>
              <div>
                <dt>工具调用</dt>
                <dd>{Math.max(0, limits.max_tool_calls - run.result.tool_calls)}</dd>
              </div>
              <div>
                <dt>总时长</dt>
                <dd>
                  {run.active
                    ? `约 ${Math.max(0, Math.ceil(limits.run_timeout_seconds - elapsed / 1000))} 秒`
                    : '运行已结束'}
                </dd>
              </div>
            </dl>
          </section>
          <VerificationPanel
            detail={run.result.verification}
            active={run.active}
            project={Boolean(run.project)}
          />
          <section>
            <span className="eyebrow">CONFIGURATION</span>
            <h2>本次配置</h2>
            <dl className="compact-dl">
              <div>
                <dt>模型</dt>
                <dd>{run.config.requested_model}</dd>
              </div>
              <div>
                <dt>工具</dt>
                <dd>{run.config.tools.join(' · ')}</dd>
              </div>
              <div>
                <dt>已知 token</dt>
                <dd>
                  <UsageDisplay run={run} />
                </dd>
              </div>
            </dl>
            <p className="muted">运行配置已固定。离开页面或关闭浏览器不会取消后台任务。</p>
          </section>
        </aside>
      </div>
    </>
  );
}
