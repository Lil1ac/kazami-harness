import { useQuery } from '@tanstack/react-query';
import { Alert, Skeleton, Tabs } from 'antd';
import { getRun, errorMessage } from '../api';
import { duration, phaseLabel } from '../format';
import { useRunEvents } from '../hooks/useRunEvents';
import { StateBreakdown } from './RunStatus';
import { VerificationPanel } from './VerificationPanel';
import { ProjectReviewPanel } from './ProjectReviewPanel';
import { LocalChangesPanel } from './LocalChangesPanel';
import { ArtifactPanel } from './ArtifactPanel';
import { ContextPanel } from './ContextPanel';
import { EventTrail } from './EventTrail';
import { UsageDisplay } from './UsageDisplay';
import type { RunView } from '../types';

/** 复用唯一最新轮订阅返回的事件视图，检查器不再为其额外创建SSE。 */
export type RunEventView = ReturnType<typeof useRunEvents>;

/** 检查面板输入；tab仅表示查看分类，不改变运行或会话状态。 */
interface InspectorPanelsProps {
  run: RunView; // 当前选择的实际运行。
  events: RunEventView; // 对应该运行的事件窗口。
  tab: string; // overview/code/artifacts/context/trace，只选择检查器分类。
  onTab: (value: string) => void; // 把查看分类同步到URL。
  historical?: boolean; // 旧轮固定只读，即使缓存中仍留有曾经的最新轮状态。
}

/** 独立显示选中运行的预算、测试、代码与轨迹；所有审阅决定仍由现有后端校验。 */
function InspectorPanels({ run, events, tab, onTab, historical = false }: InspectorPanelsProps) {
  const elapsed = events.events.at(-1)?.elapsed_ms ?? 0;
  const keys = [
    'overview',
    'artifacts',
    'context',
    'trace',
    ...(run.project || run.local_changes ? ['code'] : []),
  ];
  return (
    <div className="inspector-panels">
      <Tabs
        activeKey={keys.includes(tab) ? tab : 'overview'}
        onChange={onTab}
        destroyOnHidden
        items={[
          {
            key: 'overview',
            label: '概况',
            children: (
              <div className="inspector-overview">
                <section>
                  <StateBreakdown run={run} />
                  <p className="muted">{run.interrupted ? '运行中断' : phaseLabel(run.phase)}</p>
                </section>
                {run.interrupted && (
                  <Alert type="warning" title="该运行缺少可信结束记录，请核对已有输出。" />
                )}
                {run.warnings.map(
                  /* 宿主警告只按受控文本显示。 */ (warning, index) => (
                    <Alert key={index} type="warning" title={warning} />
                  ),
                )}
                <VerificationPanel
                  detail={run.result.verification}
                  active={run.active}
                  project={Boolean(run.project) || run.source_kind === 'project'}
                />
                <section>
                  <h3>运行记录</h3>
                  {run.interrupted && (
                    <p className="muted">
                      本次执行中断，以下调用次数来自已保存事件；发送新消息会开始独立记录并重新测试。
                    </p>
                  )}
                  <dl className="compact-dl">
                    <div>
                      <dt>编号</dt>
                      <dd>
                        <code>{run.run_id}</code>
                      </dd>
                    </div>
                    {run.execution_mode && (
                      <div>
                        <dt>执行方式</dt>
                        <dd>{run.execution_mode === 'local' ? '所选目录' : '独立 Git 工作树'}</dd>
                      </div>
                    )}
                    {run.source_path && (
                      <div>
                        <dt>所选项目</dt>
                        <dd>{run.source_path}</dd>
                      </div>
                    )}
                    <div>
                      <dt>实际执行目录</dt>
                      <dd>{run.workspace}</dd>
                    </div>
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
                      <dt>本次模型调用</dt>
                      <dd>
                        {run.result.model_turns}
                        {run.interrupted && '（已记录）'}
                      </dd>
                    </div>
                    <div>
                      <dt>本次工具调用</dt>
                      <dd>
                        {run.result.tool_calls}
                        {run.interrupted && '（已记录）'}
                      </dd>
                    </div>
                    <div>
                      <dt>本次 HTTP 尝试</dt>
                      <dd>
                        {run.result.http_attempts}
                        {run.interrupted && '（已记录）'}
                      </dd>
                    </div>
                  </dl>
                </section>
                <section>
                  <h3>本轮配置</h3>
                  <dl className="compact-dl">
                    <div>
                      <dt>模型</dt>
                      <dd>{run.config.requested_model}</dd>
                    </div>
                    <div>
                      <dt>总时限</dt>
                      <dd>{run.config.limits.run_timeout_seconds} 秒</dd>
                    </div>
                    <div>
                      <dt>工具</dt>
                      <dd>
                        {run.config.tools.join(' · ')}
                        {run.config.context?.compression && run.config.context.compression !== 'off'
                          ? ' · read_context（自动只读）'
                          : ''}
                      </dd>
                    </div>
                    <div>
                      <dt>已知用量</dt>
                      <dd>
                        <UsageDisplay run={run} />
                      </dd>
                    </div>
                  </dl>
                </section>
              </div>
            ),
          },
          ...(run.local_changes
            ? [
                {
                  key: 'code',
                  label: '代码',
                  children: (
                    <LocalChangesPanel
                      runId={run.run_id}
                      workspace={run.workspace}
                      changes={run.local_changes}
                      active={run.active}
                    />
                  ),
                },
              ]
            : run.project
              ? [
                  {
                    key: 'code',
                    label: '代码',
                    children: (
                      <ProjectReviewPanel
                        project={run.project}
                        canManage={
                          !historical && run.project_workspace?.latest_run_id === run.run_id
                        }
                        active={run.active}
                        parentRunId={run.parent_run_id}
                      />
                    ),
                  },
                ]
              : []),
          {
            key: 'artifacts',
            label: '产物',
            children: (
              <ArtifactPanel
                runId={run.run_id}
                artifacts={run.result.artifacts}
                missing={run.result.missing_artifacts}
              />
            ),
          },
          {
            key: 'context',
            label: '上下文',
            children: <ContextPanel context={run.context} stopReason={run.result.stop_reason} />,
          },
          { key: 'trace', label: '轨迹', children: <EventTrail {...events} /> },
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
}: {
  selectedId: string;
  latest: RunView;
  liveEvents: RunEventView;
  tab: string;
  onTab: (value: string) => void;
}) {
  return selectedId === latest.run_id ? (
    <InspectorPanels run={latest} events={liveEvents} tab={tab} onTab={onTab} />
  ) : (
    <HistoricalInspector key={selectedId} runId={selectedId} tab={tab} onTab={onTab} />
  );
}
