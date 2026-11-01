import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button } from 'antd';
import { cancelSubagent, errorMessage, getSubagent } from '../api';
import { duration } from '../format';
import { MarkdownMessage } from './MarkdownMessage';
import { EventTrail } from './EventTrail';
import { ContextPanel } from './ContextPanel';
import type { SubagentRecord } from '../types';
import './SubagentsPanel.css';

const statusLabels: Record<SubagentRecord['status'], string> = {
  running: '研究中',
  completed: '报告已返回',
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
}: {
  parentId: string;
  record: SubagentRecord;
  active: boolean;
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
  const [after, setAfter] = useState(0); // 当前子事件页起点，零为首条。
  const [offset, setOffset] = useState(0); // 当前报告页UTF-8字节起点，使用服务端next_offset前进。
  const query = useQuery({
    queryKey: ['subagent', parentId, record.run_id, after, offset],
    queryFn: /* 取消读取不会取消父子执行。 */ ({ signal }) =>
      getSubagent(parentId, record.run_id, after, offset, signal),
    refetchInterval: active && record.status === 'running' ? 1500 : false,
    retry: false,
  });
  const data = query.data;
  const report = data?.subagent.report ?? record.report; // 失败或中断也显示宿主保存的部分证据。
  return (
    <div className="subagent-details">
      <p>{record.objective}</p>
      {active && (data?.subagent.status ?? record.status) === 'running' && (
        <Button danger loading={stopping} onClick={stopChild}>
          停止此子任务
        </Button>
      )}
      {stopError && <Alert type="error" title={stopError} />}
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
        {(record.usage.total_tokens ?? 0).toLocaleString()} token
        {!record.usage_complete || record.status === 'running' ? ' · 已知部分' : ''}
        ，已计入父运行；子时限 {record.timeout_seconds.toFixed(1)} 秒。
      </p>
      {record.stop_reason && (
        <p>
          结束原因：<code>{record.stop_reason}</code>
        </p>
      )}
      {query.isPending && <p role="status">正在读取子执行记录…</p>}
      {query.error && <Alert type="error" title={errorMessage(query.error)} />}
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
                ? '子 Agent 正在研究，尚无最终报告。'
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
                      <code>{source.path || source.tool}</code>
                      {source.truncated ? '（读取有截断）' : ''}
                    </li>
                  ),
                )}
              </ul>
            </div>
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

/** 显示本轮委派及其只读证据；records来自父Run固定/实时快照，inactive时无终态项明确中断。 */
export function SubagentsPanel({
  parentId,
  records,
  active,
}: {
  parentId: string;
  records: SubagentRecord[];
  active: boolean;
}) {
  const [open, setOpen] = useState<string | null>(null); // 一次展开一项，避免创建多个高频读取器。
  if (!records.length) return null;
  return (
    <section className="subagents-panel" aria-label="只读子 Agent">
      <h3>子 Agent</h3>
      <p className="muted">最多两个子任务并发研究；报告供主 Agent 核对，停止主任务会同时停止所有子执行。</p>
      {records.map(
        /* 每项保持真实子编号；历史只读，不提供重新执行按钮。 */ (source) => {
          const record: SubagentRecord =
            !active && source.status === 'running'
              ? { ...source, status: 'interrupted', stop_reason: 'missing_terminal' }
              : source;
          return (
            <details
              key={record.run_id}
              open={open === record.run_id}
              onToggle={
                /* 原生details支持键盘，展开后才读取完整轨迹。 */ (event) => {
                  if (event.currentTarget.open) setOpen(record.run_id);
                  else
                    setOpen(
                      /* 关闭旧项不能误关新展开项。 */ (value) =>
                        value === record.run_id ? null : value,
                    );
                }
              }
            >
              <summary>
                <strong>{record.title}</strong>
                <span>
                  {record.role === 'explore' ? '探索' : '审阅'} ·{' '}
                  {record.status === 'completed' && record.report?.partial
                    ? '报告部分返回'
                    : statusLabels[record.status]}
                </span>
              </summary>
              {open === record.run_id && (
                <SubagentDetails
                  key={record.run_id}
                  parentId={parentId}
                  record={record}
                  active={active}
                />
              )}
            </details>
          );
        },
      )}
    </section>
  );
}
