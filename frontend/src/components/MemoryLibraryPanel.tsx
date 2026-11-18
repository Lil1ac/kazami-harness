import { useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Select, Switch } from 'antd';
import { Link } from 'react-router';
import {
  configureMemoryConsolidation,
  errorMessage,
  getMemoryConsolidation,
  getMemoryEvidence,
  getMemoryLibrary,
  getMemoryRevisions,
  jobAction,
  reviewMemoryConsolidation,
  startMemoryConsolidation,
} from '../api';
import type { MemoryConsolidation, MemoryContent, MemoryEvidenceRef } from '../types';

const states: Record<string, string> = {
  queued: '排队中', preparing: '准备中', running: '整理中', draft: '待审阅',
  no_changes: '没有新变更', published: '已发布', rejected: '已拒绝',
  failed: '整理失败', cancelled: '已取消', interrupted: '已中断',
  source_unavailable: '来源不可用',
}; // 展示整理生命周期，独立于普通会话的执行和验收。

const evidenceLabels: Record<MemoryEvidenceRef['kind'], string> = {
  user: '用户要求', assistant: '助手回复', observation: '工具观察', verification: '独立验收',
}; // 证据类别来自原始事件。

/** 按需回读 projectId 下的 ref；展开前只显示事件定位。 */
function EvidenceDetails({ projectId, reference }: { projectId: string; reference: MemoryEvidenceRef }) {
  const [open, setOpen] = useState(false); // 仅当前证据面板展开时读取摘录。
  const evidence = useQuery({
    queryKey: ['memory-evidence', projectId, reference.id],
    queryFn: /* 宿主重新核对原始证据。 */ () => getMemoryEvidence(projectId, reference.id),
    enabled: open,
    staleTime: 0,
    gcTime: 0,
  });
  return (
    <details onToggle={/* 打开时读取可用性。 */ (event) => setOpen(event.currentTarget.open)}>
      <summary>{evidenceLabels[reference.kind]} · 事件 {reference.seq} · {reference.run_id.slice(0, 8)}</summary>
      <Link to={'/runs/' + reference.run_id}>查看来源运行</Link>
      {evidence.error ? <Alert type="error" title={errorMessage(evidence.error)} /> :
        open && evidence.data && <pre>{evidence.data.text}</pre>}
    </details>
  );
}

/** 按主题引用打开当前仍生效的精确修订；版本变化时请刷新主题。 */
function TopicEntry({
  scope, id, revision, title, renderBody,
}: {
  scope: string; id: string; revision: number; title: string;
  renderBody: (item: MemoryContent) => ReactNode; // 复用项目记忆的类型正文。
}) {
  const [open, setOpen] = useState(false); // 逐条读取以减少未展开页面的请求。
  const query = useQuery({
    queryKey: ['memory-revisions', scope, id],
    queryFn: /* 读取条目修订，核对当前版本。 */ () => getMemoryRevisions(scope, id),
    enabled: open,
  });
  const current = query.data?.items[0];
  return (
    <details onToggle={/* 按需读取此条记忆。 */ (event) => setOpen(event.currentTarget.open)}>
      <summary>{title} · v{revision}</summary>
      {query.error && <Alert type="error" title={errorMessage(query.error)} />}
      {open && current && (current.revision === revision && current.status === 'active'
        ? renderBody(current) : <p role="status">条目已更新，请刷新主题。</p>)}
    </details>
  );
}

/** 展开一次整理草案并明确勾选发布项；使用已固定的前值和原始来源。 */
function ConsolidationDetail({
  projectId, id, renderBody, onChange,
}: {
  projectId: string; id: string;
  renderBody: (item: MemoryContent) => ReactNode; // 统一呈现五类正文。
  onChange: () => void; // 发布或停止后刷新项目列表和作业。
}) {
  const [selected, setSelected] = useState<number[]>([]); // 默认每一项都须明确勾选。
  const pending = useRef<{ key: string; id: string } | null>(null); // 网络结果未知时复用请求编号。
  const query = useQuery({
    queryKey: ['memory-consolidation', projectId, id],
    queryFn: /* 轮询仅观察，不执行。 */ () => getMemoryConsolidation(projectId, id),
    refetchInterval: /* 排队和运行期间及时显示状态。 */ (entry) =>
      ['queued', 'preparing', 'running'].includes(entry.state.data?.status || '') ? 1000 : false,
  });
  const review = useMutation({
    mutationFn: /* 发送经用户明确选择的变更编号。 */ (values: number[]) => {
      const key = values.join(',');
      if (pending.current?.key !== key) pending.current = { key, id: crypto.randomUUID() };
      return reviewMemoryConsolidation(projectId, id, pending.current.id, values);
    },
    onSuccess: /* 刷新确切发布回执及新主题。 */ () => {
      pending.current = null;
      void query.refetch();
      onChange();
    },
  });
  const stop = useMutation({
    mutationFn: /* 停止此队列作业。 */ () => jobAction(id, 'cancel'),
    onSuccess: /* 重新读取终态和计量。 */ () => {
      void query.refetch();
      onChange();
    },
  });
  const result = query.data;
  const editing = result?.state === 'draft';
  const busy = !!result && ['queued', 'preparing', 'running'].includes(result.status);
  const errors = query.error || review.error || stop.error;
  return (
    <div className="memory-consolidation-detail" aria-label="整理详情">
      {errors && <Alert type="error" title={errorMessage(errors)} />}
      {result && <>
        <div className="memory-actions">
          <strong>{states[result.state] || result.state}</strong>
          {result.model_calls > 0 && <span>{result.model_calls} 次模型调用 · {result.usage_complete ? '' : '至少 '}{(result.usage.total_tokens ?? 0).toLocaleString('zh-CN')} token</span>}
          {busy && <Button loading={stop.isPending} onClick={/* 明确停止活动作业。 */ () => stop.mutate()}>停止整理</Button>}
        </div>
        {result.error && <Alert type="error" title={result.error} />}
        {result.changes?.map(/* 以冻结的草案顺序展示差异。 */ (change, index) => (
          <article className="memory-draft-change" key={index}>
            <div className="memory-actions">
              {editing && <Checkbox
                aria-label={'选择 ' + (change.content?.title || change.topic)}
                checked={selected.includes(index)}
                onChange={/* 勾选不写库，确认发布才生效。 */ (event) =>
                  setSelected(event.target.checked ? [...selected, index] : selected.filter(/* 去掉本项。 */ (value) => value !== index))}
              />}
              <strong>{change.content?.title || change.topic}</strong>
              <span>{change.action === 'add' ? '新增' : change.action === 'retire' ? '停用' : change.before.length > 1 ? '合并' : '修订'}</span>
              {result.receipt && <span>{result.receipt.selected.includes(index) ? '已发布' : '已拒绝'}</span>}
            </div>
            <p>{change.reason}</p>
            {change.before.map(/* 用审阅时所依据的版本显示原值。 */ (item) => (
              <details key={item.id}><summary>原值 · {item.title} v{item.revision}</summary>{renderBody(item)}</details>
            ))}
            {change.content && renderBody(change.content)}
            {result.inputs?.filter(/* 只展示本项引用的证据。 */ (ref) => change.evidence_ids.includes(ref.id)).map(
              /* 每条证据独立回读。 */ (ref) => <EvidenceDetails key={ref.id} projectId={projectId} reference={ref} />,
            )}
          </article>
        ))}
        {editing && <div className="memory-actions">
          <Button type="primary" disabled={!selected.length} loading={review.isPending}
            onClick={/* 按固定顺序提交明确勾选项。 */ () => review.mutate([...selected].sort(/* 按草案顺序。 */ (a, b) => a - b))}>
            发布所选（{selected.length}）
          </Button>
          <Button disabled={review.isPending} onClick={/* 拒绝整批草案。 */ () => review.mutate([])}>拒绝草案</Button>
        </div>}
      </>}
    </div>
  );
}

/** 在 projectId 的记忆抽屉管理主题和整理作业；initialId 可来自作业深链接。 */
export function MemoryLibraryPanel({
  projectId, initialId, renderBody,
}: {
  projectId: string; initialId?: string;
  renderBody: (item: MemoryContent) => ReactNode; // 由调用方复用记忆正文组件。
}) {
  const cache = useQueryClient();
  const [sourceIds, setSourceIds] = useState<string[]>([]); // 人工选择的同项目历史。
  const [detailId, setDetailId] = useState(initialId); // 未打开具体草案时为空。
  const pending = useRef<{ key: string; id: string } | null>(null); // 重试同一组来源使用原作业编号。
  const query = useQuery({
    queryKey: ['memory-library', projectId],
    queryFn: /* 读取主题和作业，不启动模型。 */ () => getMemoryLibrary(projectId),
    refetchInterval: 2500,
  });
  /** 刷新本项目主题、作业和记忆正文；不重新执行已提交动作。 */
  function refresh(): void {
    void cache.invalidateQueries({ queryKey: ['memory-library', projectId] });
    void cache.invalidateQueries({ queryKey: ['memories'] });
    void cache.invalidateQueries({ queryKey: ['jobs'] });
  }
  const start = useMutation({
    mutationFn: /* 冻结本次选择，重试不复制作业。 */ () => {
      const key = [...sourceIds].sort().join(',');
      if (pending.current?.key !== key) pending.current = { key, id: crypto.randomUUID() };
      return startMemoryConsolidation(projectId, pending.current.id, sourceIds);
    },
    onSuccess: /* 接纳即展开进度，清除上次选择。 */ (job) => {
      pending.current = null;
      setDetailId(job.id);
      setSourceIds([]);
      refresh();
    },
  });
  const idle = useMutation({
    mutationFn: /* 只有显式开关才授权自动整理。 */ (enabled: boolean) => configureMemoryConsolidation(projectId, enabled),
    onSuccess: refresh,
  });
  const library = query.data;
  const error = query.error || start.error || idle.error;
  return (
    <section className="memory-library" aria-label="整理项目记忆">
      {error && <Alert type="error" title={errorMessage(error)} />}
      {library && <>
        <div className="memory-actions">
          <Select mode="multiple" aria-label="历史来源" placeholder="选择历史来源" value={sourceIds}
            maxCount={12} maxTagCount="responsive" onChange={setSourceIds}
            options={library.sources.map(/* 标题与时间帮助区分同一主题的多轮。 */ (source) => ({
              value: source.run_id, label: source.title + ' · ' + new Date(source.created_at).toLocaleDateString('zh-CN'),
            }))}
          />
          <Button type="primary" disabled={!sourceIds.length} loading={start.isPending}
            onClick={/* 接纳用户明确选择的历史。 */ () => start.mutate()}>开始整理</Button>
        </div>
        {!library.sources.length && <p>完成并允许收集记忆的运行将在这里出现。</p>}
        <label className="memory-idle-setting">
          <Switch aria-label="空闲时整理" checked={library.idle_enabled} loading={idle.isPending}
            onChange={/* 持久化用户明确选择。 */ (enabled) => idle.mutate(enabled)} />
          空闲时整理
        </label>
        {library.consolidations.map(/* 从项目入口打开最近草案或失败回执。 */ (job: MemoryConsolidation) => (
          <Button type="text" key={job.id} onClick={/* 只打开此作业详情。 */ () => setDetailId(job.id)}>
            {new Date(job.created_at).toLocaleString('zh-CN')} · {states[job.state] || job.state}
          </Button>
        ))}
        {library.topics.length > 0 && <details>
          <summary>主题 · v{library.sequence}</summary>
          {library.topics.map(/* 主题只含当时的生效引用。 */ (topic) => (
            <details key={topic.title}>
              <summary>{topic.title} · {topic.count}</summary>
              {topic.items.map(/* 打开精确条目的类型正文。 */ (item) => (
                <TopicEntry key={item.id} scope={'projects/' + encodeURIComponent(projectId)} {...item} renderBody={renderBody} />
              ))}
            </details>
          ))}
        </details>}
      </>}
      {detailId && <ConsolidationDetail key={detailId} id={detailId} projectId={projectId} renderBody={renderBody} onChange={refresh} />}
    </section>
  );
}
