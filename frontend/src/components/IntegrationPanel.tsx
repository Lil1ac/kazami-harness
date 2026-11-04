import { useId, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Input, Modal, Select, Tag } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';
import {
  createIntegration,
  errorMessage,
  getIntegrationFile,
  getIntegrations,
  getIntegrationTargets,
  integrationAction,
  resolveIntegration,
} from '../api';
import type { IntegrationView } from '../types';
import './IntegrationPanel.css';
import { readableCommand, ReviewFooter, ReviewHeading, ReviewPatch } from './ReviewSurface';

const labels: Record<IntegrationView['status'], string> = {
  preparing: '准备中',
  conflicts: '待解决冲突',
  ready: '待确认采纳',
  applying: '写回中',
  applied: '已采纳',
  undone: '已撤销',
  already_applied: '该成果已采纳',
  needs_review: '写回待核对',
  failed: '准备失败',
  stale: '目标已变化',
  cancelled: '已取消',
  abandoned: '已放弃原操作',
}; // 只展示实际采纳阶段，测试结果单独展示。

/** 展示runId成果采纳；parentRunId核验子归属，siblings可组成联合候选；sourceTest不代替候选检查。 */
export function IntegrationPanel({ runId, sourceTest, parentRunId, siblings = [] }: { runId: string; sourceTest: string; parentRunId?: string; siblings?: { run_id: string; title: string }[] }) {
  const headingId = useId(); // 父成果和子成果可同时展示，标题关联保持唯一。
  const [includeSiblings, setIncludeSiblings] = useState(true); // 完整功能默认合并其余已完成子成果，再统一检查。
  const sourceIds = includeSiblings ? siblings.map((item) => item.run_id) : []; // 用户选择的同父成果，不包含当前主来源。
  const cache = useQueryClient();
  const [target, setTarget] = useState<string>(); // 用户明确选定的登记目标。
  const createKey = 'create:' + target + ':' + sourceIds.join(','); // 来源集合也是幂等请求内容。
  const [selected, setSelected] = useState<string | undefined>(
    new URLSearchParams(window.location.search).get('integration') || undefined,
  ); // 当前审阅的固定记录，刷新列表不覆盖选中项。
  const requestIds = useRef(new Map<string, string>()); // 网络失败后的同内容操作复用编号，成功后才允许新请求。
  const [path, setPath] = useState<string>(); // 当前展开的文件或冲突。
  const [draft, setDraft] = useState(''); // 冲突文本草稿，保存前不修改候选。
  const [confirm, setConfirm] = useState(false); // 写回确认弹窗，不在打开时发送请求。
  const targets = useQuery({
    queryKey: ['integration-targets', runId, parentRunId],
    queryFn: /* 只读取同仓库登记目标。 */ () => getIntegrationTargets(runId, parentRunId),
    retry: false,
  });
  const history = useQuery({
    queryKey: ['integrations', runId, parentRunId],
    queryFn: /* 轮询真实作业与固定历史，不重新投递。 */ () => getIntegrations(runId, parentRunId),
    refetchInterval: 1000,
  });
  const records = history.data?.items || [];
  const current =
    records.find(/* 按固定编号找当前审阅项。 */ (value) => value.id === selected) || records[0];
  const preview = useQuery({
    queryKey: ['integration-file', current?.id, current?.revision, path],
    queryFn: /* 读取所选版本的固定正文。 */ () => getIntegrationFile(current!.id, path!),
    enabled: Boolean(current && path),
  });
  /** 刷新服务事实；不把HTTP接纳当作写回成功，保留当前选中的记录。 */
  function refresh(value?: IntegrationView) {
    if (value) setSelected(value.id);
    void cache.invalidateQueries({ queryKey: ['integrations', runId, parentRunId] });
    void cache.invalidateQueries({ queryKey: ['jobs'] });
  }
  /** 为同一业务key保留请求号；仅当前操作成功后清除，失败重试不重复接纳。 */
  function requestId(key: string) {
    if (!requestIds.current.has(key)) requestIds.current.set(key, crypto.randomUUID());
    return requestIds.current.get(key)!;
  }
  const create = useMutation({
    mutationFn: /* 明确选择目标后才建立准备作业。 */ () =>
      createIntegration(runId, target!, requestId(createKey), parentRunId, sourceIds),
    onSuccess: /* 接纳成功后刷新服务事实。 */ (value) => {
      requestIds.current.delete(createKey);
      refresh(value);
    },
  });
  const action = useMutation({
    mutationFn: /* 每次操作绑定当前版本与稳定请求号。 */ (value: {
      action: string;
      confirmEffect?: boolean;
    }) =>
      integrationAction(
        current!,
        value.action,
        requestId(JSON.stringify([current?.id, current?.revision, value])),
        value.confirmEffect,
      ),
    onSuccess: /* 清除已确认请求；失败则保留原请求号。 */ (value, variables) => {
      requestIds.current.delete(JSON.stringify([current?.id, current?.revision, variables]));
      setConfirm(false);
      refresh(value);
    },
  });
  const resolution = useMutation({
    mutationFn: /* 仅提交该路径的明确选择。 */ (choice: 'target' | 'source' | 'edit' | 'delete') =>
      resolveIntegration(
        current!,
        path!,
        choice,
        draft,
        requestId(JSON.stringify([current?.id, current?.revision, path, choice, draft])),
      ),
    onSuccess: /* 保存成功才清空文本草稿。 */ (value, choice) => {
      requestIds.current.delete(
        JSON.stringify([current?.id, current?.revision, path, choice, draft]),
      );
      setPath(undefined);
      setDraft('');
      refresh(value);
    },
  });
  const busy =
    create.isPending ||
    action.isPending ||
    resolution.isPending ||
    Boolean(
      current?.last_job && ['queued', 'preparing', 'running'].includes(current.last_job.status),
    ) ||
    Boolean(
      current?.last_job?.status === 'needs_review' && current.last_job.recovery_kind === 'process',
    ) ||
    Boolean(current && ['preparing', 'applying'].includes(current.status));
  const candidateCheck = current?.checks
    .filter(/* 来源通过不能替代候选检查。 */ (check) => check.scope === 'candidate')
    .at(-1);
  const canApply =
    current?.status === 'ready' &&
    !busy &&
    (current.kind === 'undo' ||
      !current.test_config?.argv.length ||
      (candidateCheck?.result.status === 'passed' &&
        candidateCheck.applicable &&
        candidateCheck.code_fingerprint === current.candidate_fingerprint));
  const sourceStatus =
    current?.source_test?.status || (current?.run_id === runId ? sourceTest : 'unknown'); // 历史记录使用其自己的来源证据。
  return (
    <section className="integration-panel" aria-labelledby={headingId}>
      <div className="section-heading">
        <h3 id={headingId}>成果采纳</h3>
        <Link to="/jobs">后台工作</Link>
      </div>
      {(targets.error || history.error || create.error || action.error || resolution.error) && (
        <Alert
          type="error"
          title={errorMessage(
            targets.error || history.error || create.error || action.error || resolution.error,
          )}
        />
      )}
      {current?.last_job?.error && <Alert type="warning" title={current.last_job.error} />}
      {siblings.length > 0 && <Checkbox disabled={busy} checked={includeSiblings} onChange={/* 仅改变准备草稿，不执行集成。 */ (event) => setIncludeSiblings(event.target.checked)}>同时集成：{siblings.map((item) => item.title).join('、')}</Checkbox>}
      <div className="integration-toolbar">
        <Select
          aria-label="采纳目标"
          placeholder="选择目标工作区"
          value={target}
          onChange={setTarget}
          options={targets.data?.items.map(
            /* 显示真实分支与目录，供用户确认目标。 */ (value) => ({
              value: value.id,
              label: `${value.label} · ${value.branch?.replace('refs/heads/', '') || '分离HEAD'} · ${value.path}`,
            }),
          )}
        />
        <Button
          disabled={!target || busy}
          loading={create.isPending}
          onClick={/* 准备只写独立集成目录。 */ () => create.mutate()}
        >
          准备集成预览
        </Button>
      </div>
      {Boolean(records.length) && (
        <Select
          aria-label="采纳历史"
          value={current?.id}
          onChange={
            /* 切换历史时关闭旧文件预览。 */ (id) => {
              setSelected(id);
              setPath(undefined);
            }
          }
          options={records.map(
            /* 用类型、目标和时间区分记录。 */ (value) => ({
              value: value.id,
              label: `${value.kind === 'undo' ? '撤销 · ' : ''}${value.target_label || value.target_id} · ${labels[value.status]} · ${new Date(value.created_at).toLocaleString()}`,
            }),
          )}
        />
      )}
      {current && (
        <>
          <div className="integration-target">
            <Tag>
              {current.status === 'needs_review' && current.last_job?.recovery_kind === 'process'
                ? '检查进程待核对'
                : labels[current.status]}
            </Tag>
            {current.target && (
              <>
                <strong>{current.target.path}</strong>
                <span>
                  {current.target.branch?.replace('refs/heads/', '') || '分离HEAD'} ·{' '}
                  {current.target.head.slice(0, 10)}
                </span>
                <details>
                  <summary>仓库与固定成果</summary>
                  <code>{current.target.repository}</code>
                  <p>来源成果：{current.source_tree}</p>
                  <p>集成检查目录：{current.workspace}</p>
                </details>
              </>
            )}
          </div>
          {current.error && (
            <Alert
              type={current.status === 'needs_review' ? 'warning' : 'info'}
              title={current.error}
            />
          )}
          <div className="integration-columns">
            <ul className="integration-files">
              {[
                ...new Set([
                  ...current.conflicts,
                  ...current.files.map(/* 合并未解决冲突与最终文件清单。 */ (value) => value.path),
                ]),
              ].map(
                /* 每个路径展示一次。 */ (name) => (
                  <li key={name}>
                    <Button
                      type={name === path ? 'primary' : 'text'}
                      onClick={
                        /* 展开文件不默认采用任何一方。 */ () => {
                          setPath(name);
                          setDraft('');
                        }
                      }
                    >
                      {name}
                      {current.conflicts.includes(name) ? ' · 冲突' : ''}
                    </Button>
                  </li>
                ),
              )}
            </ul>
            {path && (
              <div className="integration-preview">
                <h4>{path}</h4>
                {preview.error && <Alert type="error" title={errorMessage(preview.error)} />}
                {preview.data && (
                  <>
                    {current.conflicts.includes(path) ? (
                      <>
                        <div className="integration-sides">
                          <div>
                            <h5>目标当前内容</h5>
                            <pre>{preview.data.target ?? '不存在或为二进制文件'}</pre>
                          </div>
                          <div>
                            <h5>Agent 固定成果</h5>
                            <pre>{preview.data.source ?? '不存在或为二进制文件'}</pre>
                          </div>
                        </div>
                        <details>
                          <summary>共同基线</summary>
                          <pre>{preview.data.base ?? '不存在或为二进制文件'}</pre>
                        </details>
                        <Input.TextArea
                          aria-label="冲突解决正文"
                          value={draft}
                          onChange={
                            /* 修改草稿不发送请求。 */ (event) => setDraft(event.target.value)
                          }
                          autoSize={{ minRows: 5, maxRows: 18 }}
                        />
                        <div className="integration-toolbar">
                          <Button
                            disabled={busy}
                            onClick={/* 选择完整目标版本。 */ () => resolution.mutate('target')}
                          >
                            保留目标
                          </Button>
                          <Button
                            disabled={busy}
                            onClick={
                              /* 选择固定来源版本，删除语义由宿主显示在最终清单。 */ () =>
                                resolution.mutate('source')
                            }
                          >
                            采用成果
                          </Button>
                          <Button
                            disabled={busy}
                            onClick={
                              /* 保存明确文本，可保存空文件。 */ () => resolution.mutate('edit')
                            }
                          >
                            保存编辑结果
                          </Button>
                          <Button
                            danger
                            disabled={busy}
                            onClick={/* 显式删除仅先影响候选。 */ () => resolution.mutate('delete')}
                          >
                            删除此路径
                          </Button>
                        </div>
                      </>
                    ) : (
                      <ReviewPatch
                        label="最终集成差异"
                        content={preview.data.diff || '二进制或文件模式变化，请核对文件清单。'}
                      />
                    )}
                  </>
                )}
              </div>
            )}
          </div>
          <div className="integration-checks">
            {Boolean(current.test_config?.argv.length) && (
              <details>
                <summary>检查命令与执行位置</summary>
                <pre>{readableCommand(current.test_config?.argv)}</pre>
                <details>
                  <summary>完整原始参数</summary>
                  <pre>{JSON.stringify(current.test_config?.argv, null, 2)}</pre>
                </details>
                <p>候选：{current.workspace}</p>
                <p>目标：{current.target?.path}</p>
                <p>本机执行，进程按系统权限访问文件和网络。</p>
              </details>
            )}
            <span>
              来源运行测试：
              {{ passed: '通过', failed: '失败', error: '异常', unverified: '未验收' }[
                sourceStatus
              ] || '未知'}
              {current.source_test?.applicable ? ' · 已绑定来源成果' : ' · 仅保留来源运行记录'}
            </span>
            {current.checks.map(
              /* 各次检查独立保留输出和版本适用性。 */ (check, index) => (
                <details key={index}>
                  <summary>
                    {check.scope === 'candidate' ? '集成候选检查' : '采纳后目标复验'} ·{' '}
                    {
                      { passed: '通过', failed: '失败', error: '异常', unverified: '未配置' }[
                        check.result.status
                      ]
                    }
                    {check.applicable ? '' : ' · 代码已变化，结论不适用'}
                  </summary>
                  <code>{check.cwd}</code>
                  <pre>
                    {check.result.stdout}
                    {check.result.stderr}
                  </pre>
                  {check.result.truncated && <p>输出预览已截断，可下载本次原始输出。</p>}
                  {check.evidence_id &&
                    check.result.artifacts?.map(
                      /* 仅使用该次检查登记的固定输出。 */ (artifact) => (
                        <a
                          key={artifact.path}
                          href={`/api/integrations/${current.id}/checks/${check.evidence_id}/artifacts?path=${encodeURIComponent(artifact.path)}`}
                          download
                        >
                          {artifact.kind === 'stderr' ? '下载错误输出' : '下载标准输出'}{' '}
                        </a>
                      ),
                    )}
                </details>
              ),
            )}
          </div>
          {current.status === 'ready' && (
            <ReviewFooter
              note={
                !current.test_config?.argv.length
                  ? '未配置测试，采纳后保持未验收。'
                  : '先核对候选检查，再确认写回目标。'
              }
            >
              <Button
                disabled={busy}
                onClick={
                  /* 批准宿主登记的当前候选检查。 */ () =>
                    action.mutate({ action: 'check_candidate' })
                }
              >
                批准运行候选检查
              </Button>
              <Button
                type="primary"
                disabled={!canApply}
                onClick={/* 打开确认本身不写目标。 */ () => setConfirm(true)}
              >
                {current.kind === 'undo' ? '审阅并确认撤销' : '审阅并确认采纳'}
              </Button>
            </ReviewFooter>
          )}
          {current.status === 'applied' && (
            <div className="integration-toolbar">
              <Button
                disabled={busy}
                onClick={
                  /* 对实际目标单独批准复验。 */ () => action.mutate({ action: 'check_target' })
                }
              >
                批准复验目标
              </Button>
              {current.kind === 'apply' && (
                <Button
                  disabled={busy}
                  onClick={
                    /* 撤销先准备反向候选，不立即回滚。 */ () => action.mutate({ action: 'undo' })
                  }
                >
                  准备撤销预览
                </Button>
              )}
            </div>
          )}
          {current.status === 'needs_review' && current.last_job?.recovery_kind !== 'process' && (
            <>
              <div className="integration-toolbar">
                <Button
                  disabled={busy}
                  onClick={
                    /* 只核对现场，不重放写入。 */ () => action.mutate({ action: 'reconcile' })
                  }
                >
                  核对逐文件现场
                </Button>
                {current.observations?.every(
                  /* 只有完整达到预期才提供明确确认入口。 */ (item) => item.state === 'after',
                ) && (
                  <Button
                    onClick={
                      /* 用户确认现场已经完整落地，宿主仍再核验。 */ () =>
                        action.mutate({ action: 'reconcile', confirmEffect: true })
                    }
                  >
                    确认预期成果已完整落地
                  </Button>
                )}
                <Button
                  danger
                  onClick={
                    /* 放弃需先确认保留部分现场的后果。 */ () =>
                      Modal.confirm({
                        title: '核对后放弃原操作？',
                        content: '保留当前文件和备份，不回滚、不重放；剩余变化须重新准备并确认。',
                        onOk: /* 解除旧操作，不重放或回滚。 */ () =>
                          action.mutateAsync({ action: 'abandon' }),
                      })
                  }
                >
                  放弃原操作并保留现场
                </Button>
              </div>
              <ul>
                {current.observations?.map(
                  /* 显示前值、后值或外部版本，避免猜测执行次数。 */ (item) => (
                    <li key={item.path}>
                      {item.path} ·{' '}
                      {
                        {
                          before: '写回前版本',
                          after: '预期写回版本',
                          external: '其他版本，需人工核对',
                        }[item.state]
                      }
                    </li>
                  ),
                )}
              </ul>
            </>
          )}
          <Modal
            className="integration-confirm"
            title={
              <ReviewHeading
                title={current.kind === 'undo' ? '确认撤销这些改动' : '确认采纳这些改动'}
                status="尚未写回"
              />
            }
            open={confirm}
            onCancel={/* 取消确认不发送请求。 */ () => setConfirm(false)}
            onOk={/* 明确确认才批准本次固定写回。 */ () => action.mutate({ action: 'apply' })}
            confirmLoading={action.isPending}
            okText="确认写回"
            cancelText="继续审阅"
          >
            <div className="integration-confirm-body">
              <span className="review-note">目标工作区</span>
              <p className="integration-confirm-path">{current.target?.path}</p>
              <p>
                {current.target?.branch || '分离HEAD'} · {current.target?.head}
              </p>
              <ul>
                {current.files.map(
                  /* 完整展示目标实际将发生的变化。 */ (item) => (
                    <li key={item.path}>
                      {{ added: '新增', modified: '修改', deleted: '删除' }[item.status]} ·{' '}
                      {item.path}
                      {item.binary ? ' · 二进制' : ''}
                    </li>
                  ),
                )}
              </ul>
              <p className="review-note">保留原暂存区。本次写回后，目标复验另行明确批准。</p>
              {current.kind === 'undo' && (
                <Alert
                  type="warning"
                  title="撤销恢复采纳前代码，原有测试可能再次失败；请在写回后复验。"
                />
              )}
              {!current.test_config?.argv.length && (
                <Alert type="warning" title="未配置测试，本次成果未验收。" />
              )}
            </div>
          </Modal>
        </>
      )}
    </section>
  );
}
