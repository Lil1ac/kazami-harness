import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Input, Modal, Select } from 'antd';
import { Link } from 'react-router';
import { errorMessage, getMemories, getMemoryRevisions, mutateMemory } from '../api';
import type { ApiError } from '../api';
import {
  detailRows,
  emptyDetails,
  emptyMemory,
  memoryFields,
  memoryOrigins,
  memoryPhaseLabels,
  memoryPlatformLabels,
  memoryStatuses,
  memoryTypes,
} from '../memory';
import type { MemoryContent, MemoryEntry, MemoryReplacement } from '../types';
import './MemoryPanel.css';

/** 把 entry 可编辑字段复制成草稿；不修改宿主来源、版本或已捕获证据。 */
function contentOf(entry: MemoryContent): MemoryContent {
  const {
    title,
    content,
    type,
    details,
    origin,
    tags,
    applies_to,
    evidence,
    branch,
    commit,
    expires_at,
  } = entry;
  return structuredClone({
    title,
    content,
    type,
    details,
    origin,
    tags,
    applies_to,
    evidence,
    branch,
    commit,
    expires_at,
  });
}

/** 展示类型结构及补充说明；item 来自当前列表或历史快照，不查询当前库。 */
export function MemoryBody({ item }: { item: MemoryContent }) {
  return (
    <>
      <dl className="memory-details">
        {detailRows(item.type, item.details).map(
          /* 字段语义由类型决定。 */ (row) => (
            <div key={row.label}>
              <dt>{row.label}</dt>
              <dd>{row.value}</dd>
            </div>
          ),
        )}
      </dl>
      {item.content && <p className="memory-body">{item.content}</p>}
    </>
  );
}

/** 编辑或保存一条知识；scope 由项目/运行路由绑定，initial 不含权限，onSaved 只通知刷新。 */
export function MemoryEditor({
  scope,
  entry,
  initial,
  onClose,
  onSaved,
}: {
  scope: string; // 受控 projects/id 或 runs/id。
  entry?: MemoryEntry; // 省略为新增，有值时按该版本更新。
  initial: MemoryContent; // 打开时冻结的内容副本。
  onClose: () => void; // 关闭不提交。
  onSaved: () => void; // 成功后刷新当前列表。
}) {
  const [draft, setDraft] = useState(() => contentOf(initial)); // 弹窗内临时草稿。
  const [replacements, setReplacements] = useState<MemoryReplacement[]>(
    entry?.status === 'proposed' ? entry.replacements : [],
  ); // 已执行的替代只作历史，不重复执行。
  const [validation, setValidation] = useState(''); // 本地必填校验，草稿保留。
  const receipt = useRef<{ key: string; body: object } | null>(null); // 网络未知重试使用相同编号和载荷。
  const mutation = useMutation({
    mutationFn: /* 明确用户操作才写记忆，不调用模型。 */ (body: object) =>
      mutateMemory(scope, entry?.id || null, body),
    onSuccess: /* 清理当前待确认操作，再交付新修订。 */ () => {
      receipt.current = null;
      onSaved();
      onClose();
    },
  });
  /** 提交当前 draft；相同内容重试复用 request_id，返回 void，校验失败保留草稿。 */
  function save(): void {
    const values = draft.details as unknown as Record<string, string | null>;
    if (
      !draft.title.trim() ||
      memoryFields[draft.type].some(
        /* 校验当前类型必填项。 */ (field) => field.required && !values[field.key]?.trim(),
      )
    ) {
      setValidation('请填写标题和当前类型的必填内容。');
      return;
    }
    setValidation('');
    const key = JSON.stringify({ draft, replacements, revision: entry?.revision });
    if (receipt.current?.key !== key)
      receipt.current = {
        key,
        body: entry
          ? {
              request_id: crypto.randomUUID(),
              expected_revision: entry.revision,
              action: entry.status === 'proposed' ? 'accept' : 'edit',
              content: draft,
              replacements,
            }
          : { ...draft, replacements, request_id: crypto.randomUUID() },
      };
    mutation.mutate(receipt.current.body);
  }
  const conflictItems = (
    (mutation.error as ApiError | null)?.details?.conflicts ??
    entry?.conflicts ??
    []
  ).filter(/* 类型切换后旧提示不冒充新类型冲突。 */ (conflict) => conflict.type === draft.type); // 失败响应可带最新冲突版本。
  return (
    <Modal
      title={
        entry?.status === 'proposed' ? '审阅项目记忆' : entry ? '编辑项目记忆' : '保存为项目记忆'
      }
      open
      onCancel={onClose}
      onOk={save}
      okText="保存"
      confirmLoading={mutation.isPending}
    >
      <div className="memory-editor">
        <label>
          记忆标题
          <Input
            value={draft.title}
            maxLength={160}
            onChange={
              /* 修改临时标题。 */ (event) => setDraft({ ...draft, title: event.target.value })
            }
          />
        </label>
        <label>
          记忆类型
          <Select
            aria-label="记忆类型"
            value={draft.type}
            onChange={
              /* 切换类型重填主值并清理旧替代建议，补充说明保留。 */ (type) => {
                mutation.reset();
                setReplacements([]);
                setDraft({ ...draft, type, details: emptyDetails(type) });
              }
            }
            options={Object.entries(memoryTypes).map(
              /* 固定业务类型。 */ ([value, label]) => ({ value, label }),
            )}
          />
        </label>
        {memoryFields[draft.type].map(
          /* 类型只展示自身需要的字段。 */ (field) => {
            const value =
              (draft.details as unknown as Record<string, string | null>)[field.key] || '';
            /** 更新专属 field 主值；value 为空时间时发送 null，返回 void，不修改其他类型。 */
            function update(value: string): void {
              setDraft({
                ...draft,
                details: {
                  ...draft.details,
                  [field.key]: field.key === 'observed_at' ? value || null : value,
                },
              });
            }
            return (
              <label key={draft.type + field.key}>
                {field.label}
                {field.required ? ' *' : ''}
                {field.multiline ? (
                  <Input.TextArea
                    aria-label={field.label}
                    value={value}
                    autoSize={{ minRows: 2, maxRows: 8 }}
                    onChange={/* 保存当前结构字段。 */ (event) => update(event.target.value)}
                  />
                ) : (
                  <Input
                    aria-label={field.label}
                    value={value}
                    onChange={/* 保存当前结构字段。 */ (event) => update(event.target.value)}
                  />
                )}
              </label>
            );
          },
        )}
        <label>
          形成方式
          <Select
            aria-label="形成方式"
            value={draft.origin}
            onChange={/* 来源分类不改变确认权限。 */ (origin) => setDraft({ ...draft, origin })}
            options={Object.entries(memoryOrigins).map(
              /* 展示形成方式。 */ ([value, label]) => ({ value, label }),
            )}
          />
        </label>
        <label>
          补充说明
          <Input.TextArea
            aria-label="记忆正文"
            value={draft.content}
            autoSize={{ minRows: 2, maxRows: 8 }}
            onChange={
              /* 此处仅补充笔记，主值在专属字段中。 */ (event) =>
                setDraft({ ...draft, content: event.target.value })
            }
          />
        </label>
        <label>
          关键词
          <Input
            value={draft.tags.join('、')}
            onChange={
              /* 保存中文顿号分隔的标签。 */ (event) =>
                setDraft({
                  ...draft,
                  tags: event.target.value
                    .split(/[、,]/)
                    .filter(/* 空标签不提交。 */ (value) => value.trim())
                    .map(/* 清理首尾空白。 */ (value) => value.trim()),
                })
            }
          />
        </label>
        <details>
          <summary>适用条件</summary>
          <label>
            分支
            <Input
              value={draft.branch || ''}
              placeholder="留空不限分支"
              onChange={
                /* 限定实际工作树分支。 */ (event) =>
                  setDraft({ ...draft, branch: event.target.value || null })
              }
            />
          </label>
          <label>
            提交
            <Input
              value={draft.commit || ''}
              placeholder="留空不限提交"
              onChange={
                /* 完整提交哈希由后端校验。 */ (event) =>
                  setDraft({ ...draft, commit: event.target.value || null })
              }
            />
          </label>
          <label>
            到期时间
            <Input
              value={draft.expires_at || ''}
              placeholder="ISO 时间，含时区；留空不过期"
              onChange={
                /* 不转换本机时区或推测日期。 */ (event) =>
                  setDraft({ ...draft, expires_at: event.target.value || null })
              }
            />
          </label>
          <label>
            适用路径
            <Input.TextArea
              aria-label="适用路径"
              value={draft.applies_to.paths.join('\n')}
              placeholder="每行一个相对路径 glob，例如 src/api/**"
              onChange={
                /* 适用范围不捕获文件指纹。 */ (event) =>
                  setDraft({
                    ...draft,
                    applies_to: {
                      ...draft.applies_to,
                      paths: event.target.value
                        .split('\n')
                        .filter(/* 忽略空行。 */ (value) => value.trim()),
                    },
                  })
              }
            />
          </label>
          <label>
            工作阶段
            <Select
              aria-label="工作阶段"
              mode="multiple"
              value={draft.applies_to.phases}
              placeholder="不限阶段"
              options={[
                { value: 'planning', label: '规划' },
                { value: 'execution', label: '执行' },
                { value: 'verification', label: '验收修复' },
              ]}
              onChange={
                /* 宿主阶段决定触发，不猜测任务用词。 */ (phases) =>
                  setDraft({ ...draft, applies_to: { ...draft.applies_to, phases } })
              }
            />
          </label>
          <label>
            执行平台
            <Select
              aria-label="执行平台"
              mode="multiple"
              value={draft.applies_to.platforms}
              placeholder="不限平台"
              options={[
                { value: 'win32', label: 'Windows' },
                { value: 'linux', label: 'Linux' },
                { value: 'darwin', label: 'macOS' },
              ]}
              onChange={
                /* 依据实际运行平台过滤经验和其他知识。 */ (platforms) =>
                  setDraft({ ...draft, applies_to: { ...draft.applies_to, platforms } })
              }
            />
          </label>
          <label>
            证据文件
            <Input.TextArea
              aria-label="证据文件"
              value={draft.evidence.join('\n')}
              placeholder="每行一个相对文件；事实证据变化后需复核"
              onChange={
                /* 保存时由宿主捕获指纹，不能用证据充当适用范围。 */ (event) =>
                  setDraft({
                    ...draft,
                    evidence: event.target.value
                      .split('\n')
                      .filter(/* 忽略空行。 */ (value) => value.trim()),
                  })
              }
            />
          </label>
        </details>
        {conflictItems.length > 0 && (
          <section aria-label="冲突审阅">
            <h4>同主题已有不同内容</h4>
            {conflictItems.map(
              /* 必须明确选择版本化替代，后端再次核对。 */ (conflict) => (
                <div key={conflict.id}>
                  <strong>
                    {conflict.title} · v{conflict.revision}
                  </strong>
                  <dl className="memory-details">
                    {detailRows(conflict.type, conflict.details).map(
                      /* 按旧类型的中文语义显示主张，避免暴露内部JSON键。 */ (row) => (
                        <div key={row.label}>
                          <dt>{row.label}</dt>
                          <dd>{row.value}</dd>
                        </div>
                      ),
                    )}
                  </dl>
                  <p>适用路径：{conflict.applies_to.paths.join('、') || '整个项目'}</p>
                  {conflict.branch && <p>分支：{conflict.branch}</p>}
                  {conflict.commit && <p>提交：{conflict.commit}</p>}
                  {conflict.expires_at && <p>到期：{conflict.expires_at}</p>}
                  <Checkbox
                    checked={replacements.some(
                      /* 匹配审阅版本。 */ (target) =>
                        target.id === conflict.id && target.revision === conflict.revision,
                    )}
                    onChange={
                      /* 只改变本地替代选择，不自动覆盖旧内容。 */ (event) =>
                        setReplacements([
                          ...replacements.filter(
                            /* 清除同编号旧选择。 */ (target) => target.id !== conflict.id,
                          ),
                          ...(event.target.checked
                            ? [{ id: conflict.id, revision: conflict.revision }]
                            : []),
                        ])
                    }
                  >
                    保存时替代这条记忆
                  </Checkbox>
                </div>
              ),
            )}
          </section>
        )}
        {validation && <Alert type="error" title={validation} />}
        {entry && <MemorySource item={entry} />}
        {mutation.error && <Alert type="error" title={errorMessage(mutation.error)} />}
      </div>
    </Modal>
  );
}

/** 展示 item 的宿主来源与适用条件；链接只打开真实运行，不重新执行。 */
export function MemorySource({ item }: { item: MemoryEntry }) {
  return (
    <div className="memory-source">
      <span>
        v{item.revision} · {memoryTypes[item.type]} · {memoryStatuses[item.status]}
      </span>
      {item.source.unavailable ? (
        <span>来源对话已删除</span>
      ) : item.source.run_id ? (
        <Link to={'/runs/' + item.source.run_id}>
          来源运行{item.source.seq ? ' · 事件 ' + item.source.seq : ''}
        </Link>
      ) : (
        <span>人工保存</span>
      )}
      {item.replacements.map(
        /* 显示明确的版本化替代。 */ (target) => (
          <span key={target.id}>
            替代：{target.id.slice(0, 8)} · v{target.revision}
          </span>
        ),
      )}
      <span>{memoryOrigins[item.origin]}</span>
      {item.applies_to.paths.length > 0 && <span>适用：{item.applies_to.paths.join('、')}</span>}
      {item.applies_to.phases.length > 0 && (
        <span>
          阶段：
          {item.applies_to.phases
            .map(/* 展示宿主阶段含义。 */ (phase) => memoryPhaseLabels[phase])
            .join('、')}
        </span>
      )}
      {item.applies_to.platforms.length > 0 && (
        <span>
          平台：
          {item.applies_to.platforms
            .map(/* 展示实际执行平台。 */ (platform) => memoryPlatformLabels[platform])
            .join('、')}
        </span>
      )}
      {item.presentation && <span>{item.presentation}</span>}
      {item.branch && <span>分支：{item.branch}</span>}
      {item.commit && <span>提交：{item.commit}</span>}
      {item.expires_at && <span>到期：{item.expires_at}</span>}
      {item.match_reasons?.length ? <span>匹配：{item.match_reasons.join('、')}</span> : null}
      {item.evidence_refs.map(
        /* 每个关联文件指纹可人工核对。 */ (ref) => (
          <code key={ref.path}>
            {ref.path} · {ref.sha256.slice(0, 12)}
          </code>
        ),
      )}
    </div>
  );
}

/** 人工按需展开 scope/id 的修订；未展开时不发请求，旧版本仅供阅读。 */
function MemoryHistory({ scope, id }: { scope: string; id: string }) {
  const [open, setOpen] = useState(false); // 历史按需读取，避免每行重复加载正文。
  const history = useQuery({
    queryKey: ['memory-revisions', scope, id],
    queryFn: /* 精确读取当前项目条目。 */ () => getMemoryRevisions(scope, id),
    enabled: open,
  });
  return (
    <details
      onToggle={/* 用户展开后读取，不恢复执行。 */ (event) => setOpen(event.currentTarget.open)}
    >
      <summary>修订历史</summary>
      {history.error && <Alert type="error" title={errorMessage(history.error)} />}
      {history.data?.items.map(
        /* 每版来源和正文保持独立。 */ (item) => (
          <div key={item.revision}>
            <MemorySource item={item} />
            <MemoryBody item={item} />
          </div>
        ),
      )}
    </details>
  );
}

/** 管理项目列表或运行候选；projectId 与 runId 必须有一个，运行模式只显示本轮待确认候选。 */
export function MemoryPanel({
  projectId,
  runId,
  readOnly = false,
}: {
  projectId?: string;
  runId?: string;
  readOnly?: boolean; /* 归档会话仅查看候选，不接受或修改项目记忆。 */
}) {
  const scope = projectId
    ? 'projects/' + encodeURIComponent(projectId)
    : 'runs/' + encodeURIComponent(runId!);
  const [query, setQuery] = useState(''); // 显式内容过滤。
  const [type, setType] = useState<string | undefined>(); // 仅分类筛选。
  const [status, setStatus] = useState<string | undefined>('active'); // 项目默认有效条目，候选模式由后端固定。
  const [offset, setOffset] = useState(0); // 项目列表分页位置。
  const [editing, setEditing] = useState<MemoryEntry | 'new' | null>(null); // null 不显示编辑弹窗。
  const cache = useQueryClient();
  const params =
    '?' +
    new URLSearchParams({
      query,
      offset: String(offset),
      ...(type ? { type } : {}),
      ...(status ? { status } : {}),
    });
  const page = useQuery({
    queryKey: ['memories', scope, params],
    queryFn: /* 读取不发起模型任务。 */ () => getMemories(scope, projectId ? params : ''),
    refetchInterval: 5000,
  });
  const pending = useRef<{ key: string; body: object } | null>(null); // 状态决定失败时保留原编号。
  /** 刷新所有相关列表；历史快照只读，因此不根据当前库改写详情。 */
  function refresh(): void {
    void cache.invalidateQueries({ queryKey: ['memories'] });
    void cache.invalidateQueries({ queryKey: ['memory-revisions'] });
  }
  const mutation = useMutation({
    mutationFn: /* 仅发送明确决定。 */ ({ id, body }: { id: string; body: object }) =>
      mutateMemory(scope, id, body),
    onSuccess: /* 已确认操作后重读。 */ () => {
      pending.current = null;
      refresh();
    },
  });
  /** 按 action 处理 item；相同条目和版本重试沿用编号，返回 void。 */
  function decide(item: MemoryEntry, action: 'accept' | 'reject' | 'delete'): void {
    const key = item.id + ':' + item.revision + ':' + action;
    if (pending.current?.key !== key)
      pending.current = {
        key,
        body: { request_id: crypto.randomUUID(), expected_revision: item.revision, action },
      };
    mutation.mutate({ id: item.id, body: pending.current.body });
  }
  return (
    <section className="memory-panel" aria-label={projectId ? '项目记忆管理' : '记忆候选'}>
      {projectId && (
        <div className="memory-toolbar">
          <Input
            aria-label="搜索记忆"
            placeholder="搜索项目记忆"
            value={query}
            onChange={
              /* 搜索变化回到第一页。 */ (event) => {
                setQuery(event.target.value);
                setOffset(0);
              }
            }
          />
          <Select
            aria-label="记忆状态"
            value={status}
            allowClear
            placeholder="全部状态"
            onChange={
              /* 切换生命周期。 */ (value) => {
                setStatus(value);
                setOffset(0);
              }
            }
            options={Object.entries(memoryStatuses).map(
              /* 状态仅影响列表。 */ ([value, label]) => ({ value, label }),
            )}
          />
          <Select
            aria-label="记忆分类"
            value={type}
            allowClear
            placeholder="全部分类"
            onChange={
              /* 分类只作筛选。 */ (value) => {
                setType(value);
                setOffset(0);
              }
            }
            options={Object.entries(memoryTypes).map(
              /* 显示固定中文标签。 */ ([value, label]) => ({ value, label }),
            )}
          />
          <Button onClick={/* 打开空草稿。 */ () => setEditing('new')}>新增记忆</Button>
          <a href={'/api/' + scope + '/memories/export'} download>
            导出 Markdown
          </a>
        </div>
      )}
      {(page.error || mutation.error) && (
        <Alert
          type="error"
          title={errorMessage(page.error || mutation.error)}
          action={
            <Button
              onClick={
                /* 冲突后明确刷新，不自动覆盖旧草稿。 */ () => {
                  mutation.reset();
                  refresh();
                }
              }
            >
              刷新
            </Button>
          }
        />
      )}
      {page.isPending && <p>正在读取记忆…</p>}
      {page.data?.items.length === 0 && (
        <p>{projectId ? '没有符合条件的记忆' : '本轮没有待保存候选'}</p>
      )}
      {page.data?.items.map(
        /* 以条目编号定位用户决定。 */ (item) => (
          <article className="memory-row" key={item.id}>
            <h3>{item.title}</h3>
            <MemoryBody item={item} />
            <MemorySource item={item} />
            {item.unavailable_reason && <p role="status">{item.unavailable_reason}</p>}
            {projectId && <MemoryHistory scope={scope} id={item.id} />}
            {!readOnly && (
              <div className="memory-actions">
                {item.status === 'proposed' && (
                  <>
                    <Button
                      disabled={mutation.isPending}
                      onClick={
                        /* 明确接受后才跨会话生效。 */ () =>
                          item.conflicts?.length ? setEditing(item) : decide(item, 'accept')
                      }
                    >
                      保存记忆
                    </Button>
                    <Button
                      disabled={mutation.isPending}
                      onClick={/* 记录拒绝防止同源候选反复出现。 */ () => decide(item, 'reject')}
                    >
                      拒绝
                    </Button>
                  </>
                )}
                {['active', 'proposed'].includes(item.status) && (
                  <Button onClick={/* 来源和版本只读，正文按副本编辑。 */ () => setEditing(item)}>
                    编辑
                  </Button>
                )}
                {item.status !== 'deleted' && (
                  <Button
                    danger
                    disabled={mutation.isPending}
                    onClick={
                      /* 删除须明确说明历史快照仍保留。 */ () =>
                        Modal.confirm({
                          title: '删除这条项目记忆？',
                          content: '后续运行不再使用，已有运行快照仍保留。',
                          onOk: /* 决定使用当前版本。 */ () => decide(item, 'delete'),
                        })
                    }
                  >
                    删除
                  </Button>
                )}
              </div>
            )}
          </article>
        ),
      )}
      {projectId && (
        <div className="memory-actions">
          <Button
            disabled={offset === 0}
            onClick={/* 回到上一页。 */ () => setOffset(Math.max(0, offset - 20))}
          >
            上一页
          </Button>
          <Button
            disabled={page.data?.next_offset == null}
            onClick={/* 使用宿主下一页游标。 */ () => setOffset(page.data!.next_offset!)}
          >
            下一页
          </Button>
        </div>
      )}
      {editing && (
        <MemoryEditor
          scope={scope}
          entry={editing === 'new' ? undefined : editing}
          initial={editing === 'new' ? emptyMemory() : editing}
          onClose={/* 关闭不保存草稿。 */ () => setEditing(null)}
          onSaved={refresh}
        />
      )}
    </section>
  );
}

/** 从消息明确保存项目知识；runId 绑定来源，text 仅作可编辑初稿，点击前不请求接口。 */
export function SaveMemoryButton({ runId, text }: { runId: string; text: string }) {
  const [open, setOpen] = useState(false); // 用户明确打开才挂载编辑器。
  const cache = useQueryClient();
  return (
    <>
      <button type="button" onClick={/* 用户仍可精简消息正文后保存。 */ () => setOpen(true)}>
        保存为项目记忆
      </button>
      {open && (
        <MemoryEditor
          scope={'runs/' + encodeURIComponent(runId)}
          initial={{ ...emptyMemory(), title: text.split('\n')[0].slice(0, 80), content: text }}
          onClose={/* 放弃不落盘。 */ () => setOpen(false)}
          onSaved={
            /* 刷新管理页，不启动运行。 */ () => {
              void cache.invalidateQueries({ queryKey: ['memories'] });
            }
          }
        />
      )}
    </>
  );
}
