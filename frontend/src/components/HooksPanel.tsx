import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Select, Skeleton } from 'antd';
import { errorMessage, getHooks, reviewHook } from '../api';
import type { HookDefinition, HookRecord, HookSnapshot } from '../hookTypes';
import type { RunEvent } from '../types';
import { CodeOutlined, ReloadOutlined } from '@ant-design/icons';
import { IntegrationDrawer, IntegrationRow, IntegrationStatus } from './IntegrationCatalog';
import { readableCommand } from './ReviewSurface';

const statusLabels: Record<string, string> = {
  running: '执行中',
  waiting: '等待批准',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  skipped: '已跳过',
}; // 只翻译真实状态。
const reasons: Record<string, string> = {
  untrusted: '尚未审核',
  disabled: '已禁用',
  permission_denied: '当前权限禁止命令',
  workspace_busy: '工作区忙碌',
  process_or_protocol_error: '进程或返回协议失败',
  cleanup_failed: '进程未完成清理',
}; // 固定原因的中文呈现。

/** 将事件技术名转换为便于扫读的时机；未知事件保留原始标识。 */
function hookEvent(event: string): string {
  const labels: Record<string, string> = {
    RunStart: '运行开始',
    Stop: '运行结束',
    PreToolUse: '工具调用前',
    PostToolUse: '工具调用后',
    PreCompact: '上下文压缩前',
    SubagentStart: '子 Agent 开始',
    SubagentStop: '子 Agent 结束',
  };
  return labels[event] || event;
}

/** 返回规则真实状态；旧指纹被替换时要求重新审核。 */
function hookStatus(item: HookDefinition) {
  if (!item.trusted)
    return (
      <IntegrationStatus label={item.previous_definition ? '待复审' : '待审核'} tone="attention" />
    );
  return (
    <IntegrationStatus
      label={item.enabled ? '已审核 · 已启用' : '已审核 · 已停用'}
      tone={item.enabled ? 'ready' : 'neutral'}
    />
  );
}

/** 展示 definition 的精确审核内容；命令与脚本作为纯文本呈现。 */
function HookDetails({ definition }: { definition: HookDefinition }) {
  const rule = definition.rule;
  return (
    <>
      {!definition.trusted && definition.previous_definition && (
        <Alert type="warning" showIcon title="定义与上次审核的版本不同" />
      )}
      <dl className="integration-fields">
        <dt>触发时机</dt>
        <dd>
          {hookEvent(rule.event)} <code>{rule.event}</code>
        </dd>
        <dt>来源</dt>
        <dd>{definition.key.startsWith('host:') ? '宿主规则' : '项目规则'}</dd>
        <dt>工作目录</dt>
        <dd>
          <code>{definition.cwd}</code>
        </dd>
        <dt>工具</dt>
        <dd>{rule.tools.join('、') || '不限制'}</dd>
        <dt>路径</dt>
        <dd>{rule.paths.join('、') || '不限制'}</dd>
        <dt>Agent 范围</dt>
        <dd>{rule.agent_scopes.join('、') || '不限制'}</dd>
        <dt>超时</dt>
        <dd>{rule.timeout_seconds} 秒</dd>
        <dt>失败处理</dt>
        <dd>
          {rule.on_failure === 'block' ? '阻止后续动作' : '继续执行'}
          {rule.required ? ' · 必须执行' : ''}
        </dd>
      </dl>
      <h3>执行命令</h3>
      <pre>{readableCommand(rule.argv)}</pre>
      {Object.entries(definition.script_previews).map(
        /* 保留可核对的文件名、正文与截断提示。 */ ([path, preview]) => (
          <div className="integration-detail" key={path}>
            <p>
              <code>{path}</code>
            </p>
            <pre>{preview.text}</pre>
            {preview.truncated && <p>源码预览已截断，请在本地查看完整文件。</p>}
          </div>
        ),
      )}
      <details className="integration-disclosure">
        <summary>原始定义与指纹</summary>
        <pre>{JSON.stringify(rule, null, 2)}</pre>
        <pre>{JSON.stringify(definition.scripts, null, 2)}</pre>
        <code>{definition.fingerprint}</code>
      </details>
      {definition.previous_definition != null && (
        <details className="integration-disclosure">
          <summary>上次审核的定义</summary>
          <pre>{JSON.stringify(definition.previous_definition, null, 2)}</pre>
        </details>
      )}
    </>
  );
}

/** 管理 projectId 规则；未固定项目时从 projects 选择，不接受任意宿主路径。 */
export function HookSettings({
  projectId,
  projects = [],
}: {
  projectId?: string; // 项目设置固定项目，缺省时由用户选择。
  projects?: { id: string; label: string }[]; // 已登记项目及其可读名称。
}) {
  const [selected, setSelected] = useState(projects[0]?.id || ''); // 仅选择已登记项目。
  const [reviewing, setReviewing] = useState<HookDefinition | null>(null); // 用户展开并实际审核的定义。
  const drawerTrigger = useRef<HTMLButtonElement | null>(null); // 关闭详情后返回原 Hook 目录行。
  const current =
    projectId || (projects.some((item) => item.id === selected) ? selected : projects[0]?.id || '');
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ['hooks', current],
    queryFn: /* 仅读取当前项目，刷新会重新发现磁盘定义。 */ () => getHooks(current),
    enabled: Boolean(current),
  });
  const mutation = useMutation({
    mutationFn: /* 绑定页面实际显示的指纹，避免配置变化后批准新内容。 */ ({
      item,
      action,
      project,
    }: {
      item: HookDefinition;
      action: 'trust' | 'enable' | 'disable';
      project: string; // 请求提交时固定的项目，切换后响应不能覆盖另一项目。
    }) => reviewHook(project, item.key, item.fingerprint, action),
    onSuccess: /* 后端确认才更新该请求所属项目；不启动运行或脚本。 */ (value, variables) => {
      cache.setQueryData(['hooks', variables.project], value);
      if (variables.project === current) setReviewing(null);
    },
  });
  return (
    <section className="integration-catalog" aria-label="Hooks 设置">
      <header className="integration-toolbar">
        <h2>Hooks 规则</h2>
        <div className="integration-project-controls">
          {!projectId && (
            <Select
              aria-label="Hooks 项目"
              value={current || undefined}
              disabled={mutation.isPending || !projects.length}
              onChange={
                /* 切换到明确登记的项目并清除旧审核。 */ (value) => {
                  setSelected(value);
                  setReviewing(null);
                  mutation.reset();
                }
              }
              options={projects.map(
                /* 仅提供当前可选项目。 */ (item) => ({ value: item.id, label: item.label }),
              )}
              placeholder="选择项目"
            />
          )}
          {current && (
            <Button
              icon={<ReloadOutlined />}
              loading={query.isFetching}
              disabled={mutation.isPending}
              onClick={
                /* 刷新定义，不执行脚本。 */ () => {
                  setReviewing(null);
                  mutation.reset();
                  void query.refetch();
                }
              }
            >
              重新加载
            </Button>
          )}
        </div>
      </header>
      {!current && <p className="integration-empty">选择项目后查看 Hooks</p>}
      {query.isFetching && current && !query.data && <Skeleton active paragraph={{ rows: 2 }} />}
      {query.error && (
        <Alert
          type="error"
          showIcon
          title="无法读取 Hooks"
          description={errorMessage(query.error)}
        />
      )}
      {query.data &&
        (query.data.items.length ? (
          <ul className="integration-list">
            {query.data.items.map(
              /* 相同名称的宿主和项目规则仍用各自的来源编号。 */ (item) => (
                <IntegrationRow
                  key={item.key}
                  name={item.rule.id}
                  icon={<CodeOutlined />}
                  summary={
                    hookEvent(item.rule.event) +
                    ' · ' +
                    (item.key.startsWith('host:') ? '宿主规则' : '项目规则')
                  }
                  status={hookStatus(item)}
                  onOpen={
                    /* 审核必须使用打开时实际看到的版本。 */ (trigger) => {
                      drawerTrigger.current = trigger;
                      mutation.reset();
                      setReviewing(item);
                    }
                  }
                />
              ),
            )}
          </ul>
        ) : (
          <div className="integration-empty">
            <p>这个项目还没有 Hook</p>
            <code>{query.data.config_path}</code>
          </div>
        ))}
      <IntegrationDrawer
        open={Boolean(reviewing)}
        returnFocus={drawerTrigger.current}
        title={reviewing?.rule.id || 'Hook 详情'}
        onClose={
          /* 关闭详情不保存，提交期间保留确认上下文。 */ () => {
            if (!mutation.isPending) setReviewing(null);
          }
        }
        footer={
          reviewing && (
            <div className="integration-actions">
              <Button
                disabled={
                  mutation.isPending ||
                  (reviewing.rule.required && reviewing.key.startsWith('host:'))
                }
                onClick={
                  /* 启停是单独的管理决定，不授予脚本执行权限。 */ () =>
                    mutation.mutate({
                      item: reviewing,
                      action: reviewing.enabled ? 'disable' : 'enable',
                      project: current,
                    })
                }
              >
                {reviewing.enabled ? '停用' : '启用'}
              </Button>
              {!reviewing.trusted && (
                <Button
                  type="primary"
                  loading={mutation.isPending}
                  onClick={
                    /* 审核提交精确指纹，定义变化时由后端拒绝。 */ () =>
                      mutation.mutate({
                        item: reviewing,
                        action: 'trust',
                        project: current,
                      })
                  }
                >
                  信任此定义
                </Button>
              )}
            </div>
          )
        }
      >
        {reviewing && (
          <div className="integration-detail">
            {mutation.error && <Alert type="error" showIcon title={errorMessage(mutation.error)} />}
            {!reviewing.trusted && (
              <Alert
                type="warning"
                showIcon
                title="本机脚本可按系统权限访问文件与网络"
                description="信任此定义后，脚本执行仍遵守当前命令权限。"
              />
            )}
            <HookDetails definition={reviewing} />
            {!reviewing.key.startsWith('host:') && query.data?.config_path && (
              <details className="integration-disclosure">
                <summary>项目配置路径</summary>
                <code>{query.data.config_path}</code>
              </details>
            )}
          </div>
        )}
      </IntegrationDrawer>
    </section>
  );
}

/** 合并 snapshot 和当前已落盘 events 的最新调用；返回独立数组，不读取当前项目配置。 */
function hookRecords(snapshot: HookSnapshot, events: RunEvent[]): HookRecord[] {
  const records = new Map(
    (snapshot.records || []).map(/* 运行快照先建立基线。 */ (item) => [item.id, item]),
  );
  for (const event of events) {
    if (event.type === 'hook_record') {
      const record = event.payload.hook as HookRecord;
      const rank: Record<string, number> = {
        pending: 0,
        waiting: 1,
        running: 2,
        completed: 3,
        failed: 3,
        cancelled: 3,
        skipped: 3,
      }; // 终态快照不被历史窗口中的旧开始事件降级。
      if (
        record?.id &&
        (rank[record.status] ?? 0) >= (rank[records.get(record.id)?.status || 'pending'] ?? 0)
      )
        records.set(record.id, record);
    }
  }
  return [...records.values()];
}

/** 展示当轮 snapshot 与 events 的实际执行证据；历史轮无审核或重跑操作。 */
export function RunHooks({ snapshot, events }: { snapshot: HookSnapshot; events: RunEvent[] }) {
  const records = hookRecords(snapshot, events);
  if (!records.length && !snapshot.definitions?.length) return null;
  return (
    <details className="diagnostic-section">
      <summary>Hooks · {records.length}</summary>
      {records.map(
        /* 原始进程回执与脚本决策分开，失败不掩盖原工具事实。 */ (item) => (
          <article key={item.id}>
            <p>
              <strong>{item.key}</strong> · {item.event} ·{' '}
              {statusLabels[item.status] || item.status}
              {item.seconds !== undefined && ` · ${item.seconds.toFixed(2)} 秒`}
            </p>
            {item.reason && <p>{reasons[item.reason] || item.reason}</p>}
            {item.output?.reason && <p>{item.output.reason}</p>}
            <details>
              <summary>执行证据</summary>
              <pre>{JSON.stringify(item, null, 2)}</pre>
            </details>
          </article>
        ),
      )}
      <details>
        <summary>当轮定义快照</summary>
        <pre>{JSON.stringify(snapshot.definitions || [], null, 2)}</pre>
      </details>
    </details>
  );
}
