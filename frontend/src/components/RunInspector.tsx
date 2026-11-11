import { useQuery } from '@tanstack/react-query';
import { useLayoutEffect, useRef } from 'react';
import { Alert, Button, Segmented, Skeleton, Tabs } from 'antd';
import { getRun, getRuntime, errorMessage } from '../api';
import { duration, modelFailureLabel, phaseLabel } from '../format';
import { modelLabel } from '../modelSelection';
import { checkAttempts, checkReason, lastFailedCommand, usesProjectChecks } from '../runOutput';
import type { OutputTarget } from '../runOutput';
import { useRunEvents } from '../hooks/useRunEvents';
import { useRunPlanning } from '../hooks/useRunPlanning';
import { StateBreakdown } from './RunStatus';
import { RunOutputPanel } from './RunOutputPanel';
import type { ContinueFromError } from './RunOutputPanel';
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
  tab: string; // 当前查看分类；任务、计划与 Agent 归在工作组。
  onTab: (value: string, output?: OutputTarget) => void; // 更新分类和可选的输出记录定位。
  outputTarget?: OutputTarget; // 输出页的实际命令或检查深链接。
  onContinueFromError?: ContinueFromError; // 最新可继续轮可把失败证据写入草稿。
  onShowDecision?: () => void; // 把阅读焦点交回对话内待审批或待回答区。
  historical?: boolean; // true 表示历史只读，即使缓存仍保留过去的最新轮状态。
  readOnly?: boolean; // 归档会话可查看所有内容，不提供任务修改操作。
}

/** 分离测试结果、交付文件和内部诊断；参数沿用真实运行事实，返回只读面板及明确的审阅入口。 */
function InspectorPanels({
  run,
  events,
  tab,
  onTab,
  outputTarget,
  onContinueFromError,
  onShowDecision,
  historical = false,
  readOnly = false,
}: InspectorPanelsProps) {
  const { planning } = useRunPlanning(run, historical);
  const elapsed = events.events.at(-1)?.elapsed_ms ?? 0;
  const workSection = ['tasks', 'plan', 'agents'].includes(tab) ? tab : null; // URL 保持细分入口，顶部只显示一个工作标签。
  const showArtifacts = tab === 'artifacts'; // 文件与产物共用分类，差异视图有自己的阅读空间。
  const pendingPlan = !historical && planning?.pending; // 历史等待状态属于旧快照，不能跳到当前审批。
  const pendingAction = !historical && run.result.permissions.pending;
  const { execution_status, verification, repair_attempts, stop_reason } = run.result;
  const checks = checkAttempts(run.result);
  const lastCheck = checks.at(-1);
  const projectChecks = usesProjectChecks(run);
  const showVerification = Boolean(
    checks.length || repair_attempts || run.phase === 'verification',
  );
  const failedExecution = ['failed', 'timed_out', 'limit_exceeded'].includes(execution_status);
  const runtime = useQuery({
    queryKey: ['runtime', run.run_id],
    queryFn: /* 结果页只用已保存的命令状态提供错误入口。 */ () => getRuntime(run.run_id),
    enabled: tab === 'result',
    retry: false,
  });
  const failedCommand = runtime.data && lastFailedCommand(runtime.data.commands);
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
        activeKey={workSection ? 'work' : showArtifacts ? 'files' : tab}
        onChange={
          /* 工作组默认从任务列表开始，其余入口保持原分类。 */ (value) =>
            onTab(value === 'work' ? 'tasks' : value)
        }
        destroyOnHidden
        items={[
          {
            key: 'result',
            label: '结果',
            children: (
              <div className="inspector-overview">
                <StateBreakdown run={run} />
                {run.active && <p className="muted">{phaseLabel(run.phase)}</p>}
                {(pendingPlan || pendingAction) && onShowDecision && (
                  <div className="inspector-decision">
                    <span>
                      {pendingAction
                        ? '等待动作审批'
                        : pendingPlan && pendingPlan.kind === 'question'
                          ? '等待回答'
                          : '等待计划批准'}
                    </span>
                    <Button size="small" onClick={onShowDecision}>
                      前往对话处理
                    </Button>
                  </div>
                )}
                {run.interrupted && (
                  <Alert type="warning" title="该运行缺少可信结束记录，请核对已有输出。" />
                )}
                {run.warnings.map(
                  /* 保留宿主风险提示，不从完成状态推断成功。 */ (warning, index) => (
                    <Alert key={index} type="warning" title={warning} />
                  ),
                )}
                {failedExecution && (
                  <section className="run-check-summary" aria-label="执行错误">
                    {modelFailureLabel(stop_reason) && <p>{modelFailureLabel(stop_reason)}</p>}
                    <Button
                      size="small"
                      onClick={/* 执行结束原因与实际事件保存在诊断。 */ () => onTab('diagnostics')}
                    >
                      查看执行错误
                    </Button>
                  </section>
                )}
                {failedCommand && (
                  <section className="run-check-summary" aria-label="命令错误">
                    <h2>命令失败</h2>
                    <p>
                      <code title={failedCommand.argv.join(' ')}>
                        {failedCommand.argv.join(' ')}
                      </code>
                    </p>
                    <Button
                      size="small"
                      onClick={
                        /* 选择实际失败的命令。 */ () =>
                          onTab('output', `command:${failedCommand.id}`)
                      }
                    >
                      查看命令错误
                    </Button>
                  </section>
                )}
                {showVerification && (
                  <section className="run-check-summary">
                    <h2>{projectChecks ? '项目测试' : '独立验收'}</h2>
                    {repair_attempts > 0 && (
                      <p className="muted" role="status">
                        {run.active && !verification ? '正在进行' : '已进行'}第 {repair_attempts} /
                        2 次自动修复
                      </p>
                    )}
                    {verification && checkReason(verification) && (
                      <p>{checkReason(verification)}</p>
                    )}
                    {!verification && (
                      <p className="muted">
                        {run.phase === 'verification' ? '正在检查' : '当前代码尚无检查结果'}
                      </p>
                    )}
                    {lastCheck && (
                      <Button
                        size="small"
                        onClick={
                          /* 固定到已选检查，后台得到新结果时保留当前阅读。 */ () =>
                            onTab('output', `check:${lastCheck.repair_attempt}`)
                        }
                      >
                        {verification && verification.status !== 'passed'
                          ? '查看错误'
                          : verification
                            ? '查看检查输出'
                            : '查看历史检查'}
                      </Button>
                    )}
                  </section>
                )}
                {Boolean(run.config.mcp_tools?.length) && (
                  <McpEvidence runId={run.run_id} active={run.active && !historical} />
                )}
              </div>
            ),
          },
          {
            key: 'output',
            label: '输出',
            children: (
              <RunOutputPanel
                run={run}
                active={run.active && !historical}
                readOnly={readOnly || historical}
                target={outputTarget}
                onSelect={/* 记录选择可随 URL 保留。 */ (target) => onTab('output', target)}
                onContinue={onContinueFromError}
              />
            ),
          },
          {
            key: 'files',
            label: '文件',
            children: (
              <div className="inspector-files">
                <Segmented
                  aria-label="文件内容"
                  value={showArtifacts ? 'artifacts' : 'files'}
                  onChange={onTab}
                  options={[
                    { label: '代码改动', value: 'files' },
                    { label: '产物', value: 'artifacts' },
                  ]}
                />
                {showArtifacts ? (
                  <ArtifactPanel
                    runId={run.run_id}
                    artifacts={run.result.artifacts}
                    status={run.result.artifact_status}
                    warnings={run.result.artifact_warnings}
                  />
                ) : run.local_changes ? (
                  <LocalChangesPanel
                    runId={run.run_id}
                    workspace={run.workspace}
                    changes={run.local_changes}
                    active={run.active}
                  />
                ) : run.project ? (
                  <ProjectReviewPanel
                    project={run.project}
                    canManage={
                      !historical &&
                      !readOnly &&
                      run.project_workspace?.latest_run_id === run.run_id
                    }
                    active={run.active}
                    parentRunId={run.parent_run_id}
                  />
                ) : (
                  <p className="muted">本轮没有保存代码差异。</p>
                )}
              </div>
            ),
          },
          {
            key: 'work',
            label: '工作',
            children: (
              <div className="inspector-work">
                <Segmented
                  aria-label="工作详情分类"
                  options={[
                    { label: '任务', value: 'tasks' },
                    { label: '计划', value: 'plan' },
                    { label: 'Agent', value: 'agents' },
                  ]}
                  value={workSection || 'tasks'}
                  onChange={onTab}
                />
                {workSection === 'tasks' && (
                  <TasksPanel
                    runId={run.run_id}
                    active={run.active && !historical}
                    historical={historical}
                    readOnly={readOnly}
                  />
                )}
                {workSection === 'plan' &&
                  (pendingPlan && onShowDecision ? (
                    <div className="inspector-decision">
                      <span>{pendingPlan.kind === 'question' ? '等待回答' : '等待计划批准'}</span>
                      <Button size="small" onClick={onShowDecision}>
                        前往对话处理
                      </Button>
                    </div>
                  ) : (
                    <PlanningPanel runId={run.run_id} state={planning!} />
                  ))}
                {workSection === 'agents' && (
                  <>
                    <TeamPanel run={run} historical={historical} readOnly={readOnly} />
                    {Boolean(
                      run.result.subagents?.some(
                        /* 团队成员按独立记录展示。 */ (record) => !record.team_id,
                      ),
                    ) && (
                      <SubagentsPanel
                        parentId={run.run_id}
                        records={(run.result.subagents || []).filter(
                          /* 排除已在团队中展示的成员。 */ (record) => !record.team_id,
                        )}
                        active={run.active && !historical}
                      />
                    )}
                  </>
                )}
              </div>
            ),
          },
          {
            key: 'diagnostics',
            label: '诊断',
            children: (
              <div className="inspector-diagnostics">
                <PermissionPanel
                  run={run}
                  historical={historical}
                  hidePending={Boolean(pendingAction && onShowDecision)}
                />
                <RunHooks snapshot={run.result.hooks} events={events.events} />
                <LifecycleDiagnostics snapshot={run.lifecycle} live={events.lifecycle} />
                {checks.length > 0 && (
                  <details className="diagnostic-section">
                    <summary>检查原因与清理记录</summary>
                    <dl className="compact-dl">
                      {checks.map(
                        /* 诊断保留每次检查的原始原因码及进程清理状态。 */ ({
                          repair_attempt,
                          result,
                        }) => (
                          <div key={repair_attempt}>
                            <dt>
                              {repair_attempt ? '第 ' + repair_attempt + ' 次修复后' : '首次检查'}
                            </dt>
                            <dd>
                              <code>
                                {result.reason} · {result.cleanup_status}
                              </code>
                            </dd>
                          </div>
                        ),
                      )}
                    </dl>
                  </details>
                )}
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
                  <div className="result-usage">
                    <span>已知用量</span>
                    <UsageDisplay run={run} />
                  </div>
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
                      <dd title={run.config.requested_model ?? undefined}>
                        {modelLabel(
                          run.config,
                          run.result.response_model,
                          run.config.provider
                            ? run.config.custom_providers?.[run.config.provider]?.name
                            : undefined,
                        )}
                      </dd>
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
  outputTarget,
}: {
  runId: string; // 已接纳的历史运行编号。
  tab: string; // 历史检查器的当前分类。
  onTab: InspectorPanelsProps['onTab']; // 切换分类，不改变继续发送目标。
  outputTarget?: OutputTarget; // 查看历史日志时定位当轮实际记录。
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
  return (
    <InspectorPanels
      run={query.data}
      events={events}
      tab={tab}
      onTab={onTab}
      outputTarget={outputTarget}
      historical
    />
  );
}

/** 按选择轮次展示详情；最新轮共享实时事件，历史轮使用独立只读窗口。 */
export function RunInspector({
  selectedId,
  latest,
  liveEvents,
  tab,
  onTab,
  readOnly = false,
  outputTarget,
  onContinueFromError,
  onShowDecision,
}: {
  selectedId: string; // 所选运行；与最新轮不同时只读固定快照。
  latest: RunView; // 最新轮快照，保留运行控制的归属。
  liveEvents: RunEventView; // 对话已建立的唯一实时事件视图。
  tab: string; // 当前分类。
  onTab: InspectorPanelsProps['onTab']; // 更新详情分类及对应输出，不执行任务。
  readOnly?: boolean; // 归档会话仅提供查看，不允许任务修改。
  outputTarget?: OutputTarget; // 当前轮的命令、工具调用或检查次数。
  onContinueFromError?: ContinueFromError; // 仅最新可继续轮提供，历史面板不接收。
  onShowDecision?: () => void; // 当前审批或问题跳回对话中的处理区。
}) {
  return selectedId === latest.run_id ? (
    <InspectorPanels
      key={latest.run_id}
      run={latest}
      events={liveEvents}
      tab={tab}
      onTab={onTab}
      readOnly={readOnly}
      outputTarget={outputTarget}
      onContinueFromError={onContinueFromError}
      onShowDecision={onShowDecision}
    />
  ) : (
    <HistoricalInspector
      key={selectedId}
      runId={selectedId}
      tab={tab}
      onTab={onTab}
      outputTarget={outputTarget}
    />
  );
}
