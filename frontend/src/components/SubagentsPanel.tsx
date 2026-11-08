import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Drawer, Modal } from 'antd';
import {
  CheckCircleOutlined,
  LoadingOutlined,
  CloseCircleOutlined,
  ArrowRightOutlined,
  SearchOutlined,
  CodeOutlined,
} from '@ant-design/icons';
import { cancelSubagent, errorMessage, getSubagent, getSubagentDiff, discardSubagentWorkspace, patchUrl } from '../api';
import { duration, modelFailureLabel } from '../format';
import { modelLabel } from '../modelSelection';
import { MarkdownMessage } from './MarkdownMessage';
import { EventTrail } from './EventTrail';
import { ContextPanel } from './ContextPanel';
import type { SubagentRecord } from '../types';
import './SubagentsPanel.css';
import { IntegrationPanel } from './IntegrationPanel';
import { ReviewPatch } from './ReviewSurface';
import { ArtifactPanel } from './ArtifactPanel';

const statusLabels: Record<SubagentRecord['status'], string> = {
  running: '研究中',
  completed: '报告已返回',
  waiting_input: '等待批准或接续',
  failed: '研究失败',
  cancelled: '已取消',
  timed_out: '已超时',
  limit_exceeded: '额度已耗尽',
  interrupted: '已中断，结果待核对',
}; // 只描述子研究，不暗示代码任务完成或独立检查通过。

const findingLabels = { required: '必要修复', observation: '观察', optional: '可选建议' }; // 分类不改变父任务授权。

/** 展开后分页读取parentId直属record的证据；独立游标避免子轨迹混进父级订阅。 */
function SubagentDetails({
  parentId,
  record,
  active,
  siblings,
}: {
  parentId: string;
  record: SubagentRecord;
  active: boolean;
  siblings: { run_id: string; title: string }[]; // 同父已固定且范围合规的其他开发成果。
}) {
  const client = useQueryClient(); // 停止后刷新父快照及子详情，实际状态由后端决定。
  const [stopping, setStopping] = useState(false); // 请求期间防止重复点击。
  const [stopError, setStopError] = useState<string | null>(null); // 错误保留，允许明确重试。
  /** 停止当前直属子项并刷新结果；不触发父任务停止或重新执行，返回Promise<void>。 */
  async function stopChild() {
    setStopping(true);
    setStopError(null);
    try {
      await cancelSubagent(parentId, record.run_id);
      await Promise.all([
        client.invalidateQueries({ queryKey: ['subagent', parentId, record.run_id] }),
        client.invalidateQueries({ queryKey: ['run', parentId] }),
      ]);
    } catch (error) {
      setStopError(errorMessage(error));
    } finally {
      setStopping(false);
    }
  }
  const [discarding, setDiscarding] = useState(false); // 真实工作树删除请求进行中，固定证据不删除。
  /** 用户明确确认后丢弃工作树；返回Promise<void>，失败显示受控原因并允许重试。 */
  async function discardWorkspace() {
    setDiscarding(true);
    try {
      await discardSubagentWorkspace(parentId, record.run_id);
      await client.invalidateQueries({ queryKey: ['subagent', parentId, record.run_id] });
    } catch (error) {
      setStopError(errorMessage(error));
    } finally {
      setDiscarding(false);
    }
  }
  const [after, setAfter] = useState(0); // 当前子事件页起点，零为首条。
  const [offset, setOffset] = useState(0); // 当前报告页UTF-8字节起点，使用服务端next_offset前进。
  const query = useQuery({
    queryKey: ['subagent', parentId, record.run_id, after, offset],
    queryFn: /* 取消读取不会取消父子执行。 */ ({ signal }) =>
      getSubagent(parentId, record.run_id, after, offset, signal),
    refetchInterval: active && record.status === 'running' ? 1500 : false,
    retry: false,
  });
  const patch = useQuery({
    queryKey: ['subagent-diff', parentId, record.run_id, record.snapshot_status],
    queryFn: /* 固定补丁不读取正在变化的工作区。 */ () => getSubagentDiff(parentId, record.run_id),
    enabled: record.role === 'develop' && record.snapshot_status === 'ready',
    retry: false,
  });
  const data = query.data;
  const report = data?.subagent.report ?? record.report; // 失败或中断也显示宿主保存的部分证据。
  return (
    <div className="subagent-details">
      <p className="agent-objective">{record.objective}</p>
      <div className="agent-detail-meta">
        <span>{record.role === 'develop' ? '开发' : record.role === 'explore' ? '探索' : '审阅'}</span>
        <span title={'请求模型：' + record.model.requested_model}>{modelLabel(record.model, record.response_model, record.provider_name)}</span>
        <span>{duration(record.elapsed_seconds * 1000)}</span>
      </div>
      {active && (data?.subagent.status ?? record.status) === 'running' && (
        <Button danger loading={stopping} onClick={stopChild}>
          停止此子任务
        </Button>
      )}
      {modelFailureLabel(record.stop_reason) && <Alert type="error" title={modelFailureLabel(record.stop_reason)} />}
      {stopError && <Alert type="error" title={stopError} />}
      <details className="agent-runtime">
        <summary>执行信息</summary>
        {record.task_id && (
          <p className="muted">
            关联工作项：<code>{record.task_id}</code>
          </p>
        )}
        <p className="muted">
          {record.model_turns}/{record.max_model_turns} 次模型 · {record.tool_calls}/
          {record.max_tool_calls} 次工具 · {duration(record.elapsed_seconds * 1000)}
        </p>
        {record.skill_names.length > 0 && (
          <p className="muted">本次技能：{record.skill_names.join('、')}</p>
        )}
        {record.parent_reserve.model_turns !== undefined && (
          <p className="muted">
            分配时为父任务保留 {record.parent_reserve.model_turns} 次模型、
            {record.parent_reserve.tool_calls ?? 0} 次工具；均包含在原总预算。
          </p>
        )}
        <p className="muted">
          {record.usage_complete || (record.usage.total_tokens ?? 0) > 0
            ? `${(record.usage.total_tokens ?? 0).toLocaleString()} token${record.usage_complete ? '' : ' · 已知部分'}`
            : '暂无已知用量'}
          ，已计入父运行；子时限 {record.timeout_seconds.toFixed(1)} 秒。
        </p>
        {record.cleanup_status !== 'not_needed' && <p>进程清理：{record.cleanup_status === 'complete' ? '已完成' : record.cleanup_status === 'unknown' ? '结果不明' : '未完成'}</p>}
        {record.stop_reason && (
          <p>
            结束原因：<code>{record.stop_reason}</code>
          </p>
        )}
      </details>
      {query.isPending && <p role="status">正在读取子执行记录…</p>}
      {query.error && <Alert type="error" title={errorMessage(query.error)} />}
      {record.role === 'develop' && (
        <div className="subagent-delivery">
          <p><code>{record.workspace}</code></p>
          <p>修改范围：{record.write_scope.join('、')}</p>
          {record.pending_action && <Alert type="info" title="请在父会话处理此子任务的动作审批。" />}
          {record.scope_violations.length > 0 && <Alert type="error" title={`超出委派范围，不能采纳：${record.scope_violations.join('、')}`} />}
          {record.snapshot_status === 'error' && <Alert type="error" title="代码快照未完整保存，请核对工作区。" />}
          {patch.error && <Alert type="error" title={errorMessage(patch.error)} />}
          {!active && data?.project && data.project.status !== 'discarded' && (
            <Button danger loading={discarding} onClick={/* 丢弃仅由用户明确确认，保留固定成果。 */ () => Modal.confirm({ title: '丢弃此子任务工作树？', content: '工作目录将删除，已保存的报告、补丁和采纳记录保留。', onOk: discardWorkspace })}>丢弃子工作树</Button>
          )}
          {data?.project?.status === 'discarded' && <p>子工作树已丢弃，以下为固定历史证据。</p>}
          {patch.data && <details><summary>子成果差异 · {record.changed_paths.length} 个文件</summary><ReviewPatch content={patch.data.content} label="子成果补丁" /><a href={patchUrl(record.run_id, parentId)} download>下载完整子补丁</a>{patch.data.truncated && <p>预览已截断，完整成果通过集成审阅逐文件查看。</p>}</details>}
          {!active && record.status === 'completed' && record.snapshot_status === 'ready' && !record.scope_violations.length && (
            record.team_id ? <p className="muted">从团队汇集成果统一审阅采纳。</p> : <IntegrationPanel runId={record.run_id} parentRunId={parentId} sourceTest="unverified" siblings={siblings} />
          )}
        </div>
      )}
      {data && (
        <>
          {data.warnings.map(
            /* 历史损坏必须可见，不补造成功。 */ (warning, index) => (
              <Alert key={index} type="warning" title={warning} />
            ),
          )}
          {report ? (
            <div className="subagent-report">
              {report.partial && (
                <Alert type="warning" title="研究仅部分完成，请先核对限制和未完成事项。" />
              )}
              <MarkdownMessage text={report.summary} />
              {report.findings.map(
                /* 分类和证据均来自核验后的报告，不能把可选建议当新增需求。 */ (finding, index) => (
                  <article key={index}>
                    <strong>
                      {findingLabels[finding.category]} · {finding.title}
                    </strong>
                    <MarkdownMessage text={finding.detail} />
                  </article>
                ),
              )}
              {report.limitations.length > 0 && (
                <div>
                  <strong>限制</strong>
                  <ul>
                    {report.limitations.map(
                      /* 每条限制保持原文。 */ (item, index) => (
                        <li key={index}>{item}</li>
                      ),
                    )}
                  </ul>
                </div>
              )}
              {report.pending.length > 0 && (
                <div>
                  <strong>未完成事项</strong>
                  <ul>
                    {report.pending.map(
                      /* 待办仅供父Agent判断，不自动续跑。 */ (item, index) => (
                        <li key={index}>{item}</li>
                      ),
                    )}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <p className="muted">
              {record.status === 'running'
                ? '子 Agent 正在执行，尚无最终报告。'
                : '该子执行没有最终报告。'}
            </p>
          )}
          <details>
            <summary>完整报告</summary>
            <pre>{data.report_page.content || '尚无已固定报告。'}</pre>
            {(offset > 0 || !data.report_page.eof) && (
              <div className="subagent-pagination">
                {offset > 0 && (
                  <Button onClick={/* 返回完整报告第一页，不重新执行。 */ () => setOffset(0)}>
                    报告首页
                  </Button>
                )}
                {!data.report_page.eof && (
                  <Button
                    onClick={
                      /* 只使用服务端返回的UTF-8安全偏移。 */ () =>
                        setOffset(data.report_page.next_offset!)
                    }
                  >
                    下一页报告
                  </Button>
                )}
                <span>
                  报告字节 {offset} / {data.report_page.total_bytes}
                </span>
              </div>
            )}
          </details>
          {report && report.evidence.length > 0 && (
            <div className="subagent-sources">
              <strong>读取依据</strong>
              <ul>
                {report.evidence.map(
                  /* 来源由宿主工具回执产生，不能从模型文字推断。 */ (source, index) => (
                    <li key={`${source.tool_call_id}:${index}`}>
                      {source.run_id && source.run_id !== record.run_id && <small>此前执行 {source.run_id.slice(0, 8)} · </small>}
                      <code>{source.path || source.tool}</code>
                      {source.truncated ? '（读取有截断）' : ''}
                    </li>
                  ),
                )}
              </ul>
            </div>
          )}
          {record.role === 'develop' && (data.artifacts.length > 0 || data.artifact_status === 'incomplete') && (
            <details><summary>子产物与命令输出</summary><ArtifactPanel runId={record.run_id} parentRunId={parentId} artifacts={data.artifacts} status={data.artifact_status} warnings={data.artifact_warnings} /></details>
          )}
          <details>
            <summary>子执行轨迹</summary>
            <EventTrail
              events={data.events.items}
              connection={active && record.status === 'running' ? 'live' : 'closed'}
              error={null}
              canLoadOlder={false}
              loadingOlder={false}
              loadOlder={/* 子事件使用下方独立分页入口；此回调不发请求。 */ async () => undefined}
            />
            <div className="subagent-pagination">
              {after > 0 && (
                <Button onClick={/* 回到该子日志开头。 */ () => setAfter(0)}>轨迹首页</Button>
              )}
              {data.events.has_more && (
                <Button
                  onClick={
                    /* 按本页最后序号前进，避免跳过尚未显示的事件。 */ () =>
                      setAfter(data.events.items.at(-1)!.seq)
                  }
                >
                  下一页轨迹
                </Button>
              )}
            </div>
          </details>
          <details>
            <summary>子执行上下文</summary>
            <ContextPanel context={data.context} stopReason={record.stop_reason} />
          </details>
        </>
      )}
    </div>
  );
}

/** 显示本轮委派、固定成果与交接证据；records取父快照，inactive时缺终态项明确中断。 */
export function SubagentsPanel({
  parentId,
  records,
  active,
}: {
  parentId: string;
  records: SubagentRecord[];
  active: boolean;
}) {
  const [open, setOpen] = useState<string | null>(null); // 一次仅打开一个详情，避免多个高频读取器。
  const normalized = records.map(
    /* 历史缺失终态时保留中断事实。 */ (record): SubagentRecord =>
      !active && record.status === 'running'
        ? { ...record, status: 'interrupted', stop_reason: 'missing_terminal' }
        : record,
  );
  const selected = normalized.find(
    /* 列表和详情共享同一份真实状态。 */ (record) => record.run_id === open,
  );
  if (!records.length) return null;
  return (
    <section className="subagents-panel" aria-label="子 Agent">
      <header className="work-panel-heading">
        <div>
          <h3>
            子 Agent <small>{records.length}</small>
          </h3>
          <span className="muted">
            {normalized.filter((record) => record.status === 'running').length} 个运行中
          </span>
        </div>
      </header>
      <ul className="agent-list">
        {normalized.map(
          /* 主列表保持紧凑，报告及轨迹在独立阅读层。 */ (record) => (
            <li key={record.run_id} className={`agent-row agent-${record.status}`}>
              <button
                type="button"
                className="agent-open"
                onClick={/* 仅打开报告，不调度子任务。 */ () => setOpen(record.run_id)}
              >
                <span className="agent-avatar">
                  {record.role === 'explore' ? <SearchOutlined /> : <CodeOutlined />}
                </span>
                <span className="agent-row-copy">
                  <strong>{record.title}</strong>
                  <small>
                    {record.role === 'develop' ? '开发' : record.role === 'explore' ? '探索' : '审阅'} ·{' '}
                    {record.status === 'completed' && record.report?.partial
                      ? '报告部分返回'
                      : modelFailureLabel(record.stop_reason) || statusLabels[record.status]}
                  </small>
                  <span>{record.objective}</span>
                </span>
                <span className="agent-status-icon">
                  {record.status === 'running' ? (
                    <LoadingOutlined spin />
                  ) : record.status === 'completed' ? (
                    <CheckCircleOutlined />
                  ) : (
                    <CloseCircleOutlined />
                  )}
                </span>
                <ArrowRightOutlined className="agent-row-arrow" />
              </button>
            </li>
          ),
        )}
      </ul>
      <Drawer
        title={selected?.title || 'Agent 详情'}
        open={Boolean(selected)}
        size={560}
        styles={{ wrapper: { maxWidth: '100vw' } }}
        onClose={/* 返回列表只停止读取，不取消执行。 */ () => setOpen(null)}
        className="agent-detail-drawer"
        destroyOnHidden
      >
        {selected && (
          <SubagentDetails
            key={selected.run_id}
            parentId={parentId}
            record={selected}
            active={active}
            siblings={normalized.filter((item) => item.run_id !== selected.run_id && !item.team_id && item.role === 'develop' && item.status === 'completed' && item.snapshot_status === 'ready' && !item.scope_violations.length).map((item) => ({ run_id: item.run_id, title: item.title }))}
          />
        )}
      </Drawer>
    </section>
  );
}
