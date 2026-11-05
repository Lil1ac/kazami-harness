import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Empty, Select, Skeleton, Space } from 'antd';
import { errorMessage, getHooks, reviewHook } from '../api';
import type { HookDefinition, HookRecord, HookSnapshot } from '../hookTypes';
import type { RunEvent } from '../types';

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

/** 展示一条 rule 的精确定义；返回只读命令、匹配条件及脚本内容摘要。 */
function HookDetails({ definition }: { definition: HookDefinition }) {
  return (
    <div>
      <pre>{JSON.stringify(definition.rule, null, 2)}</pre>
      <p>
        工作目录：<code>{definition.cwd}</code>
      </p>
      {Object.entries(definition.script_previews).map(
        /* 源码作为纯文本显示，审核不会执行。 */ ([path, preview]) => (
          <div key={path}>
            <p>
              <code>{path}</code>
            </p>
            <pre>{preview.text}</pre>
            {preview.truncated && <p>源码预览已截断，请在本地查看完整文件。</p>}
          </div>
        ),
      )}
      <details>
        <summary>入口摘要与指纹</summary>
        <pre>{JSON.stringify(definition.scripts, null, 2)}</pre>
        <code>{definition.fingerprint}</code>
      </details>
      {definition.previous_definition != null && (
        <details>
          <summary>上次审核的定义</summary>
          <pre>{JSON.stringify(definition.previous_definition, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

/** 管理 projectId 规则；未固定项目时从 projects 选择，不接受任意宿主路径。 */
export function HookSettings({
  projectId,
  projects = [],
}: {
  projectId?: string;
  projects?: { id: string; label: string }[];
}) {
  const [selected, setSelected] = useState(projects[0]?.id || ''); // 仅选择已登记项目。
  const [reviewing, setReviewing] = useState<HookDefinition | null>(null); // 用户展开并实际审核的定义。
  const current = projectId || selected;
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
    <section className="settings-section" aria-label="Hooks 设置">
      <h2>Hooks</h2>
      {!projectId && (
        <Select
          aria-label="Hooks 项目"
          value={selected || undefined}
          onChange={
            /* 切换项目清除旧审核草稿。 */ (value) => {
              setSelected(value);
              setReviewing(null);
            }
          }
          options={projects.map(
            /* 项目登记名称用于选择。 */ (item) => ({ value: item.id, label: item.label }),
          )}
          placeholder="选择项目"
          style={{ minWidth: 200 }}
        />
      )}
      {current && (
        <Button
          onClick={
            /* 只重读配置，不执行脚本。 */ () => {
              setReviewing(null);
              void query.refetch();
            }
          }
        >
          重新加载
        </Button>
      )}
      {!current && <p>打开项目后可查看和审核生命周期脚本。</p>}
      {query.isFetching && current && !query.data && <Skeleton active />}
      {(query.error || mutation.error) && (
        <Alert
          type="error"
          title="Hooks 操作失败"
          description={errorMessage(query.error || mutation.error)}
        />
      )}
      {query.data && (
        <>
          <p>
            <code>{query.data.config_path}</code>
          </p>
          {!query.data.items.length && (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未配置 Hook" />
          )}
          {query.data.items.map(
            /* 每条规则使用来源编号，宿主与项目同名也独立显示。 */ (item) => (
              <article key={item.key} className="diagnostic-section">
                <Space wrap>
                  <strong>{item.key}</strong>
                  <span>{item.rule.event}</span>
                  <span>
                    {!item.trusted
                      ? '待审核'
                      : item.enabled
                        ? '已审核 · 已启用'
                        : '已审核 · 已禁用'}
                  </span>
                </Space>
                <pre>{item.rule.argv.join('\n')}</pre>
                <Space>
                  <Button
                    disabled={mutation.isPending}
                    onClick={/* 展示完整定义后才允许确认审核。 */ () => setReviewing(item)}
                  >
                    审核定义
                  </Button>
                  <Button
                    disabled={
                      mutation.isPending || (item.rule.required && item.key.startsWith('host:'))
                    }
                    onClick={
                      /* 只修改人工开关，不获得命令执行许可。 */ () =>
                        mutation.mutate({
                          item,
                          action: item.enabled ? 'disable' : 'enable',
                          project: current,
                        })
                    }
                  >
                    {item.enabled ? '禁用' : '启用'}
                  </Button>
                </Space>
                {reviewing?.key === item.key && (
                  <div>
                    <HookDetails definition={reviewing} />
                    <p>本机脚本可按系统权限访问文件与网络。审核后，执行仍遵守当前命令权限。</p>
                    <Button
                      type="primary"
                      loading={mutation.isPending}
                      onClick={
                        /* 提交用户已查看定义的精确指纹。 */ () =>
                          mutation.mutate({ item: reviewing, action: 'trust', project: current })
                      }
                    >
                      信任此定义
                    </Button>
                  </div>
                )}
              </article>
            ),
          )}
        </>
      )}
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
