import { useQuery } from '@tanstack/react-query';
import { useLayoutEffect, useRef } from 'react';
import { Alert, Skeleton, Tabs } from 'antd';
import { getRun, errorMessage } from '../api';
import { duration, phaseLabel } from '../format';
import { modelLabel } from '../modelSelection';
import { useRunEvents } from '../hooks/useRunEvents';
import { useRunPlanning } from '../hooks/useRunPlanning';
import { StateBreakdown } from './RunStatus';
import { VerificationPanel } from './VerificationPanel';
import { ProjectReviewPanel } from './ProjectReviewPanel';
import { LocalChangesPanel } from './LocalChangesPanel';
import { ArtifactPanel } from './ArtifactPanel';
import { ContextPanel } from './ContextPanel';
import { EventTrail } from './EventTrail';
import { UsageDisplay } from './UsageDisplay';
import { PlanningPanel } from './PlanningPanel';
import { TasksPanel } from './TasksPanel';
import { TeamPanel } from './TeamPanel';
import { SubagentsPanel } from './SubagentsPanel';
import { PermissionPanel } from './PermissionPanel';
import { RunHooks } from './HooksPanel';
import { McpEvidence } from './McpEvidence';
import { LifecycleDiagnostics } from './LifecycleDiagnostics';
import type { RunView } from '../types';

/** 复用唯一最新轮订阅返回的事件视图，检查器不再为其额外创建SSE。 */
export type RunEventView = ReturnType<typeof useRunEvents>;

/** 检查面板输入；tab 仅控制阅读分类，历史轮不允许管理共享工作树。 */
interface InspectorPanelsProps {
  run: RunView; // 所选轮次的服务端快照。
  events: RunEventView; // 对应该轮次的事件，不重复订阅最新轮。
  tab: string; // result/files/diagnostics 三种查看分类。
  onTab: (value: string) => void; // 只更新查看位置，不改变执行目标。
  historical?: boolean; // true 表示历史只读，即使缓存仍保留过去的最新轮状态。
  readOnly?: boolean; // 归档会话可查看所有内容，不提供任务修改操作。
}

/** 分离测试结果、交付文件和内部诊断；参数沿用真实运行事实，返回只读面板及明确的审阅入口。 */
function InspectorPanels({
  run,
  events,
  tab,
  onTab,
  historical = false,
  readOnly = false,
}: InspectorPanelsProps) {
  const { planning } = useRunPlanning(run, historical);
  const elapsed = events.events.at(-1)?.elapsed_ms ?? 0;
  const container = useRef<HTMLDivElement>(null); // 仅定位详情滚动区，不触碰会话消息与草稿。
  useLayoutEffect(
    /* 切换分类或轮次时从顶部阅读；事件刷新保持位置，不把文件区的滚动带入诊断。 */ () => {
      const body = container.current?.querySelector('.ant-tabs-body-holder');
      if (body) body.scrollTop = 0;
    },
    [run.run_id, tab],
  );
  return (
    <div className="inspector-panels" ref={container}>
      <Tabs
        activeKey={
          ['tasks', 'plan', 'agents', 'result', 'files', 'diagnostics'].includes(tab)
            ? tab
            : 'result'
        }
        onChange={onTab}
        destroyOnHidden
        items={[
          {
            key: 'tasks',
            label: '任务',
            children: (
              <TasksPanel
                runId={run.run_id}
                active={run.active && !historical}
                historical={historical}
                readOnly={readOnly}
              />
            ),
          },
          {
            key: 'plan',
            label: '计划',
            children: <PlanningPanel runId={run.run_id} state={planning!} />,
          },
          {
            key: 'agents',
            label: 'Agent',
            children: <><TeamPanel run={run} historical={historical} readOnly={readOnly} />
              {Boolean(run.result.subagents?.some(/* 普通一次性委派仍使用原视图。 */ (record) => !record.team_id)) && <SubagentsPanel parentId={run.run_id} records={(run.result.subagents || []).filter(/* 团队执行由上方按成员分组显示。 */ (record) => !record.team_id)} active={run.active && !historical} />}</>,
          },
          {
            key: 'result',
            label: '结果',
            children: (
              <div className="inspector-overview">
                <StateBreakdown run={run} />
                <PermissionPanel run={run} historical={historical} />
                <p className="muted">{run.interrupted ? '运行中断' : phaseLabel(run.phase)}</p>
                {run.interrupted && (
                  <Alert type="warning" title="该运行缺少可信结束记录，请核对已有输出。" />
                )}
                {run.warnings.map(
                  /* 保留宿主风险提示，不从完成状态推断成功。 */ (warning, index) => (
                    <Alert key={index} type="warning" title={warning} />
                  ),
                )}
                <VerificationPanel
                  detail={run.result.verification}
                  attempts={run.result.verification_attempts}
                  repairAttempts={run.result.repair_attempts}
                  runId={run.run_id}
                  active={run.active}
                  project={
                    run.verification_kind === 'project_tests' ||
                    Boolean(run.project) ||
                    run.source_kind === 'project'
                  }
                />
                {Boolean(run.config.mcp_tools?.length) && <McpEvidence runId={run.run_id} active={run.active && !historical} />}
                <div className="result-usage">
                  <span>已知用量</span>
                  <UsageDisplay run={run} />
                </div>
              </div>
            ),
          },
          {
            key: 'files',
            label: '文件',
            children: (
              <div className="inspector-files">
                {run.local_changes ? (
                  <LocalChangesPanel
                    runId={run.run_id}
                    workspace={run.workspace}
                    changes={run.local_changes}
                    active={run.active}
                  />
                ) : run.project ? (
                  <ProjectReviewPanel
                    project={run.project}
                    canManage={!historical && run.project_workspace?.latest_run_id === run.run_id}
                    active={run.active}
                    parentRunId={run.parent_run_id}
                  />
                ) : null}
                <ArtifactPanel
                  runId={run.run_id}
                  artifacts={run.result.artifacts}
                  status={run.result.artifact_status}
                  warnings={run.result.artifact_warnings}
                />
              </div>
            ),
          },
          {
            key: 'diagnostics',
            label: '诊断',
            children: (
              <div className="inspector-diagnostics">
                <RunHooks snapshot={run.result.hooks} events={events.events} />
                <LifecycleDiagnostics snapshot={run.lifecycle} live={events.lifecycle} />
                <details className="diagnostic-section" open>
                  <summary>执行轨迹</summary>
                  <EventTrail {...events} />
                </details>
                <details className="diagnostic-section">
                  <summary>上下文与指令</summary>
                  <ContextPanel context={run.context} stopReason={run.result.stop_reason} />
                </details>
                <details className="diagnostic-section">
                  <summary>运行配置与记录</summary>
                  {run.interrupted && (
                    <p className="muted">运行已中断；调用次数仅代表已保存记录。</p>
                  )}
                  <dl className="compact-dl">
                    <div>
                      <dt>编号</dt>
                      <dd>
                        <code>{run.run_id}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>实际执行目录</dt>
                      <dd>
                        <code>{run.workspace}</code>
                      </dd>
                    </div>
                    {run.source_path && (
                      <div>
                        <dt>所选项目</dt>
                        <dd>{run.source_path}</dd>
                      </div>
                    )}
                    {run.execution_mode && (
                      <div>
                        <dt>执行方式</dt>
                        <dd>{run.execution_mode === 'local' ? '所选目录' : '独立 Git 工作树'}</dd>
                      </div>
                    )}
                    {run.python_execution && (
                      <>
                        <div>
                          <dt>默认 Python</dt>
                          <dd>{run.python_execution.executable || 'PATH 中未找到 Python'}</dd>
                        </div>
                        <div>
                          <dt>源码导入目录</dt>
                          <dd>{run.python_execution.python_paths.join(' · ')}</dd>
                        </div>
                      </>
                    )}
                    <div>
                      <dt>结束原因</dt>
                      <dd>
                        <code>{run.result.stop_reason || '尚未结束'}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>已记录耗时</dt>
                      <dd>{duration(elapsed)}</dd>
                    </div>
                    <div>
                      <dt>模型调用</dt>
                      <dd>{run.result.model_turns}</dd>
                    </div>
                    <div>
                      <dt>工具调用</dt>
                      <dd>{run.result.tool_calls}</dd>
                    </div>
                    <div>
                      <dt>HTTP 尝试</dt>
                      <dd>{run.result.http_attempts}</dd>
                    </div>
                    <div>
                      <dt>模型</dt>
                      <dd title={'请求模型：' + run.config.requested_model}>{modelLabel(run.config, run.result.response_model, run.config.custom_providers?.[run.config.provider]?.name)}</dd>
                    </div>
                    <div>
                      <dt>总时限</dt>
                      <dd>{run.config.limits.run_timeout_seconds} 秒</dd>
                    </div>
                    <div>
                      <dt>自动修复</dt>
                      <dd>{run.config.auto_repair ? '开启（最多2次）' : '关闭'}</dd>
                    </div>
                    <div>
                      <dt>工具</dt>
                      <dd>
                        {run.config.tools.join(' · ')}
                        {run.config.context.compression !== 'off'
                          ? ' · read_context（自动只读）'
                          : ''}
                      </dd>
                    </div>
                  </dl>
                </details>
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}

/** 历史检查器只读取终态事件；切换历史不会开启旧任务SSE或重放工具。 */
function HistoricalInspector({
  runId,
  tab,
  onTab,
}: {
  runId: string;
  tab: string;
  onTab: (value: string) => void;
}) {
  const query = useQuery({
    queryKey: ['run', runId],
    queryFn: /* 仅读取已保存运行。 */ ({ signal }) => getRun(runId, signal),
    retry: false,
  });
  const events = useRunEvents(runId, false, /* 历史读取不改变会话最新轮。 */ () => undefined);
  if (query.isPending) return <Skeleton active paragraph={{ rows: 8 }} />;
  if (!query.data)
    return <Alert type="error" title="无法读取本轮记录" description={errorMessage(query.error)} />;
  return <InspectorPanels run={query.data} events={events} tab={tab} onTab={onTab} historical />;
}

/** 按选择轮次展示详情；最新轮共享实时事件，历史轮使用独立只读窗口。 */
export function RunInspector({
  selectedId,
  latest,
  liveEvents,
  tab,
  onTab,
  readOnly = false,
}: {
  selectedId: string;
  latest: RunView;
  liveEvents: RunEventView;
  tab: string;
  onTab: (value: string) => void;
  readOnly?: boolean; // 归档会话仅提供查看，不允许任务修改。
}) {
  return selectedId === latest.run_id ? (
    <InspectorPanels run={latest} events={liveEvents} tab={tab} onTab={onTab} readOnly={readOnly} />
  ) : (
    <HistoricalInspector key={selectedId} runId={selectedId} tab={tab} onTab={onTab} />
  );
}
