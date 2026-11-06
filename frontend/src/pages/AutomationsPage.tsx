import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Input, InputNumber, Modal, Select, Switch } from 'antd';
import { Link } from 'react-router';
import {
  automationAction,
  errorMessage,
  getAutomationHistory,
  getAutomationNotifications,
  getAutomationReview,
  getAutomations,
  getOptions,
  grantAutomation,
  previewAutomation,
  readAutomationNotification,
  saveAutomation,
} from '../api';
import type { AutomationDefinition, AutomationRule, AutomationSchedule } from '../automationTypes';
import type { LocalProjectOption } from '../types';
import './AutomationsPage.css';

const statusLabels: Record<string, string> = {
  pending: '等待投递',
  dispatching: '正在接纳',
  queued: '排队中',
  preparing: '准备中',
  running: '运行中',
  waiting_input: '需要你的决定',
  needs_review: '待核对',
  completed: '执行完成',
  failed: '失败',
  cancelled: '已取消',
  skipped: '已跳过',
  coalesced: '已合并等待',
}; // 保留执行与验收的独立语义。

/** 将value按显式timezone展示；timezone为空保留原偏移，无值表示没有后续时间。 */
function timeLabel(value: string | null, timezone?: string): string {
  if (!value) return '无后续执行时间';
  return timezone
    ? new Date(value).toLocaleString('zh-CN', { timeZone: timezone, hour12: false })
    : value.replace('T', ' ');
}

/** 创建默认工作日09:00触发；无共享对象，编辑不会改变列表里的已保存规则。 */
function defaultSchedule(): AutomationSchedule {
  return {
    kind: 'cron',
    timezone: 'Asia/Shanghai',
    cron: '0 9 * * mon-fri',
    at: null,
    interval_minutes: null,
    event: null,
    catch_up: false,
    catch_up_minutes: 60,
  };
}

/** 规则编辑对话框；rule为空新建，projects仅提供宿主登记来源，onSaved在保存成功后关闭。 */
function RuleEditor({
  rule,
  projects,
  onClose,
  onSaved,
}: {
  rule: AutomationRule | null;
  projects: LocalProjectOption[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(rule?.definition.name || ''); // 当前表单名称。
  const [prompt, setPrompt] = useState(rule?.definition.task.prompt || ''); // 当前任务模板。
  const [projectId, setProjectId] = useState(
    rule?.definition.task.project_id || rule?.definition.task.workspace_id || projects[0]?.id,
  ); // 实际执行来源。
  const [schedule, setSchedule] = useState<AutomationSchedule>(
    rule?.definition.schedule || defaultSchedule(),
  ); // 互斥的时间/事件条件。
  const [isolated, setIsolated] = useState(rule?.definition.task.execution_mode !== 'local'); // Git新规则默认工作树。
  const [overlap, setOverlap] = useState<'skip' | 'coalesce'>(rule?.definition.overlap || 'skip'); // 不影响原项目资源锁。
  const [permissionMode, setPermissionMode] = useState(
    rule?.definition.task.config.permission_mode || 'workspace_edit',
  ); // 不允许页面修改宿主上限。
  const [configuration, setConfiguration] = useState(
    JSON.stringify(rule?.definition.task.config || {}, null, 2),
  ); // 高级公开配置，后端拒绝内部字段。
  const [advancedError, setAdvancedError] = useState(''); // JSON解析错误在表单内解释。
  const project = projects.find(
    /* 按登记编号找实际类型，不按路径文本猜测。 */ (item) => item.id === projectId,
  );
  const preview = useMutation({
    mutationFn: /* 只传公开时间条件，不将查询库上下文混入API参数。 */ (value: AutomationSchedule) =>
      previewAutomation(value),
  });
  const save = useMutation({
    mutationFn: /* 将表单完整定义送至同源接口，保存不启用。 */ (definition: AutomationDefinition) =>
      saveAutomation(definition, rule || undefined),
    onSuccess: onSaved,
  });

  /** 切换kind时清除其他类型字段，避免隐藏旧值影响实际触发。 */
  function changeKind(kind: AutomationSchedule['kind']): void {
    setSchedule({
      ...schedule,
      kind,
      cron: kind === 'cron' ? '0 9 * * mon-fri' : null,
      at: kind === 'once' ? new Date(Date.now() + 3600000).toISOString() : null,
      interval_minutes: kind === 'interval' ? 60 : null,
      event: kind === 'event' ? 'verification_failed' : null,
    });
    preview.reset();
  }

  /** 校验公开JSON配置并提交；无项目或无效JSON只显示错误，不发送任何任务。 */
  function submit(): void {
    if (!project) return;
    try {
      const config = JSON.parse(configuration);
      if (!config || Array.isArray(config) || typeof config !== 'object')
        throw new Error('配置必须是JSON对象');
      const base = {
        prompt,
        config: { ...config, permission_mode: permissionMode },
        mode: rule?.definition.task.mode || ('auto' as const),
        execution_mode:
          project.kind === 'project' && isolated ? ('isolated' as const) : ('local' as const),
      };
      save.mutate({
        name,
        schedule,
        overlap,
        task:
          project.kind === 'project'
            ? { ...base, project_id: project.id, base_ref: 'HEAD' }
            : {
                ...base,
                workspace_id: project.id,
                verifier_id: rule?.definition.task.verifier_id || null,
              },
      });
      setAdvancedError('');
    } catch {
      setAdvancedError('高级运行配置需要有效的JSON对象。');
    }
  }

  return (
    <Modal
      title={rule ? '编辑自动化' : '新建自动化'}
      open
      onCancel={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            loading={save.isPending}
            disabled={!project || !name.trim() || !prompt.trim()}
            onClick={submit}
          >
            保存并保持暂停
          </Button>
        </>
      }
      width={680}
    >
      <div className="automation-form">
        {(save.error || advancedError) && (
          <Alert type="error" showIcon title={advancedError || errorMessage(save.error)} />
        )}
        <label>
          名称
          <Input
            value={name}
            maxLength={100}
            onChange={/* 名称仅改变草稿。 */ (event) => setName(event.target.value)}
          />
        </label>
        <label>
          项目
          <Select
            aria-label="自动化项目"
            value={projectId}
            onChange={setProjectId}
            options={projects.map(
              /* 仅可用登记项目进入选择器。 */ (item) => ({
                value: item.id,
                label: item.label,
                disabled: !item.available,
              }),
            )}
          />
        </label>
        <label>
          任务
          <Input.TextArea
            rows={4}
            value={prompt}
            onChange={/* 提示词不作为宿主授权。 */ (event) => setPrompt(event.target.value)}
          />
        </label>
        {project?.kind === 'project' && (
          <label className="automation-inline">
            <Switch checked={isolated} onChange={setIsolated} />
            在独立工作树修改
          </label>
        )}
        <div className="automation-form-row">
          <label>
            触发
            <Select
              aria-label="触发方式"
              value={schedule.kind}
              onChange={changeKind}
              options={[
                { value: 'cron', label: '每天 / 每周 / Cron' },
                { value: 'interval', label: '固定间隔' },
                { value: 'once', label: '一次性' },
                { value: 'event', label: '本机运行事件' },
              ]}
            />
          </label>
          <label>
            时区
            <Input
              value={schedule.timezone}
              onChange={
                /* 显式保存IANA时区，不跟随宿主时区。 */ (event) =>
                  setSchedule({ ...schedule, timezone: event.target.value })
              }
            />
          </label>
        </div>
        {schedule.kind === 'cron' && (
          <>
            <Select
              aria-label="常用时间"
              value={schedule.cron}
              onChange={
                /* 预设统一落为同一Cron定义。 */ (cron) => setSchedule({ ...schedule, cron })
              }
              options={[
                { value: '0 9 * * mon-fri', label: '工作日 09:00' },
                { value: '0 9 * * *', label: '每天 09:00' },
                { value: '0 9 * * mon', label: '每周一 09:00' },
                { value: '0 * * * *', label: '每小时' },
              ]}
            />
            <label>
              Cron
              <Input
                value={schedule.cron || ''}
                onChange={
                  /* 高级表达式由后端成熟触发器严格校验。 */ (event) =>
                    setSchedule({ ...schedule, cron: event.target.value })
                }
              />
            </label>
            <small>五段表达式；星期推荐 mon–sun，数字 0 表示周一。</small>
          </>
        )}
        {schedule.kind === 'interval' && (
          <label>
            间隔（分钟）
            <InputNumber
              min={1}
              max={525600}
              value={schedule.interval_minutes}
              onChange={
                /* 空输入恢复一分钟，避免隐式零间隔循环。 */ (value) =>
                  setSchedule({ ...schedule, interval_minutes: value || 1 })
              }
            />
          </label>
        )}
        {schedule.kind === 'once' && (
          <label>
            执行时间（包含偏移）
            <Input
              value={schedule.at || ''}
              placeholder="2026-11-09T09:00:00+08:00"
              onChange={
                /* 使用带偏移ISO时间，避免浏览器时区歧义。 */ (event) =>
                  setSchedule({ ...schedule, at: event.target.value })
              }
            />
          </label>
        )}
        {schedule.kind === 'event' && (
          <label>
            同项目普通任务
            <Select
              aria-label="事件类型"
              value={schedule.event}
              onChange={
                /* 自动化自身事件在后端过滤，不能组成递归链。 */ (event) =>
                  setSchedule({ ...schedule, event })
              }
              options={[
                { value: 'verification_failed', label: '执行结束且验收失败 / 异常' },
                { value: 'completed', label: '执行完成' },
              ]}
            />
          </label>
        )}
        {schedule.kind !== 'event' && (
          <>
            <label className="automation-inline">
              <Switch
                checked={schedule.catch_up}
                onChange={
                  /* 用户明确选择才补跑。 */ (catch_up) => setSchedule({ ...schedule, catch_up })
                }
              />
              漏跑时在窗口内补最近一次
            </label>
            {schedule.catch_up && (
              <label>
                补跑窗口（分钟）
                <InputNumber
                  min={1}
                  max={10080}
                  value={schedule.catch_up_minutes}
                  onChange={
                    /* 补跑窗口不扩展任务预算。 */ (value) =>
                      setSchedule({ ...schedule, catch_up_minutes: value || 60 })
                  }
                />
              </label>
            )}
            <Button
              onClick={/* 预览不保存规则或执行任务。 */ () => preview.mutate(schedule)}
              loading={preview.isPending}
            >
              预览执行时间
            </Button>
            {preview.error && <Alert type="error" title={errorMessage(preview.error)} />}
            {preview.data && (
              <ul className="automation-times">
                {preview.data.items.map(
                  /* 每个真实时区结果单独展示。 */ (time) => (
                    <li key={time}>{timeLabel(time)}</li>
                  ),
                )}
              </ul>
            )}
          </>
        )}
        <label>
          已有执行时
          <Select
            aria-label="重叠策略"
            value={overlap}
            onChange={setOverlap}
            options={[
              { value: 'skip', label: '跳过本次' },
              { value: 'coalesce', label: '合并保留最近一次待执行' },
            ]}
          />
        </label>
        <label>
          权限
          <Select
            aria-label="自动化权限"
            value={permissionMode}
            onChange={setPermissionMode}
            options={[
              { value: 'research', label: '仅研究' },
              { value: 'workspace_edit', label: '工作区编辑，未授权命令仍需审批' },
              { value: 'strict', label: '严格审批' },
            ]}
          />
        </label>
        <details>
          <summary>运行配置</summary>
          <label>
            模型、工具、技能、记忆与预算（公开配置 JSON）
            <Input.TextArea
              aria-label="高级运行配置"
              rows={5}
              value={configuration}
              onChange={
                /* 只编辑草稿，后端仍严格验证公开字段。 */ (event) =>
                  setConfiguration(event.target.value)
              }
            />
          </label>
        </details>
        <small>
          电脑与宿主需保持运行。保存或编辑后均暂停；启用不会自动批准未来方案和未授权动作。
        </small>
      </div>
    </Modal>
  );
}

/** 持续授权对话框；只接受当前审核指纹和精确命令，onSaved成功后刷新规则。 */
function GrantDialog({
  rule,
  allowed,
  onClose,
  onSaved,
}: {
  rule: AutomationRule;
  allowed: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const query = useQuery({
    queryKey: ['automation-review', rule.id],
    queryFn: /* 读取不会授予任何许可。 */ () => getAutomationReview(rule.id),
  });
  const [commands, setCommands] = useState('[]'); // 用户审核的精确argv/cwd列表。
  const [verification, setVerification] = useState(false); // 验收授权默认关闭。
  const [hooks, setHooks] = useState<string[]>([]); // 默认不授权Hook命令。
  const [parseError, setParseError] = useState(''); // 无效JSON保留在表单，不发送请求。
  const mutation = useMutation({
    mutationFn: /* 后端再次核对指纹、宿主和实际入口。 */ (
      body: Parameters<typeof grantAutomation>[1],
    ) => grantAutomation(rule.id, body),
    onSuccess: onSaved,
  });
  useEffect(
    /* 当前授权只作为明确可修改的审核草稿。 */ () => {
      if (query.data?.grant) {
        setCommands(JSON.stringify(query.data.grant.commands, null, 2));
        setVerification(query.data.grant.verification);
        setHooks(query.data.grant.hooks);
      }
    },
    [query.data],
  );
  /** 将用户确认的命令JSON提交；无审核快照或格式无效时不请求授权。 */
  function submit(): void {
    try {
      const values = JSON.parse(commands);
      if (!Array.isArray(values) || !query.data) throw new Error();
      mutation.mutate({
        revision: query.data.revision,
        fingerprint: query.data.fingerprint,
        commands: values,
        verification,
        hooks,
      });
      setParseError('');
    } catch {
      setParseError('命令需为 JSON 数组，例如 [{"argv":["python","-m","pytest"],"cwd":"."}]。');
    }
  }
  return (
    <Modal
      title="审核持续授权"
      open
      width={700}
      onCancel={onClose}
      footer={
        <>
          <Button onClick={onClose}>关闭</Button>
          <Button
            type="primary"
            disabled={!allowed || !query.data}
            loading={mutation.isPending}
            onClick={submit}
          >
            保存精确授权
          </Button>
        </>
      }
    >
      {!allowed && (
        <Alert type="info" title="宿主尚未开启持续授权；任务仍可运行，遇到动作审批时等待处理。" />
      )}
      {(query.error || mutation.error || parseError) && (
        <Alert type="error" title={parseError || errorMessage(query.error || mutation.error)} />
      )}
      {query.data && (
        <div className="automation-form">
          <p>{query.data.path}</p>
          <p>本机命令按当前用户权限执行。授权绑定规则版本、参数、目录及入口内容，可随时撤销。</p>
          <details>
            <summary>有效运行配置</summary>
            <pre>{JSON.stringify(query.data.config, null, 2)}</pre>
          </details>
          <label>
            精确命令（argv / cwd JSON）
            <Input.TextArea
              aria-label="持续授权命令"
              rows={5}
              value={commands}
              onChange={/* 不解析自然语言命令或前缀。 */ (event) => setCommands(event.target.value)}
            />
          </label>
          {query.data.verifier && (
            <Checkbox
              checked={verification}
              onChange={
                /* 用户明确选择固定验收入口。 */ (event) => setVerification(event.target.checked)
              }
            >
              允许固定验收：{query.data.verifier.argv.join(' ')}（{query.data.verifier.cwd}）
            </Checkbox>
          )}
          {query.data.hooks.map(
            /* 未审核或停用的Hook不可由持续授权解除限制。 */ (hook) => (
              <Checkbox
                key={hook.key}
                disabled={!hook.trusted || !hook.enabled}
                checked={hooks.includes(hook.key)}
                onChange={
                  /* 只更改本表单选中的Hook编号。 */ (event) =>
                    setHooks(
                      event.target.checked
                        ? [...hooks, hook.key]
                        : hooks.filter((key) => key !== hook.key),
                    )
                }
              >
                {hook.key} · {hook.rule.argv.join(' ')}
                {!hook.trusted && '（需先在项目设置审核）'}
              </Checkbox>
            ),
          )}
        </div>
      )}
    </Modal>
  );
}

/** 展示rule触发的真实状态与作业链接，不用页面重试旧运行。 */
function RuleHistory({ rule }: { rule: AutomationRule }) {
  const query = useQuery({
    queryKey: ['automation-history', rule.id],
    queryFn: /* 仅查询触发账本。 */ () => getAutomationHistory(rule.id),
    refetchInterval: 2000,
  });
  return (
    <div className="automation-history">
      {query.error && <Alert type="error" title={errorMessage(query.error)} />}
      {query.data?.items.length === 0 && <p>还没有触发记录</p>}
      {query.data?.items.map(
        /* 跳过或合并也保留计划时刻和原因。 */ (fire) => (
          <article key={fire.id}>
            <div>
              <span>{timeLabel(fire.scheduled_at, rule.definition.schedule.timezone)}</span>
              <strong>{statusLabels[fire.status] || fire.status}</strong>
              {fire.validation_status && (
                <span>
                  {
                    (
                      {
                        unverified: '未验收',
                        passed: '验收通过',
                        failed: '验收失败',
                        error: '验收异常',
                      } as Record<string, string>
                    )[fire.validation_status]
                  }
                </span>
              )}
            </div>
            {fire.reason && <p>{fire.reason}</p>}
            {fire.source_run_id && <Link to={'/runs/' + fire.source_run_id}>来源运行</Link>}
            {fire.job_id && (
              <Link to={'/jobs/' + fire.job_id}>
                {['waiting_input', 'needs_review'].includes(fire.status)
                  ? '处理此项'
                  : '查看执行与成果'}
              </Link>
            )}
          </article>
        ),
      )}
    </div>
  );
}

/** 本机自动化管理和持久收件箱；桌面及Web共用API，刷新不触发任务。 */
export function AutomationsPage() {
  const cache = useQueryClient();
  const rules = useQuery({
    queryKey: ['automations'],
    queryFn: getAutomations,
    refetchInterval: 3000,
  });
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 将查询取消信号传给既有读取API。 */ ({ signal }) => getOptions(signal),
  });
  const inbox = useQuery({
    queryKey: ['automation-notifications'],
    queryFn: getAutomationNotifications,
    refetchInterval: 3000,
  });
  const [editing, setEditing] = useState<AutomationRule | null | undefined>(); // undefined关闭，null新建，否则编辑。
  const [granting, setGranting] = useState<AutomationRule | null>(null); // 当前审核规则。
  const [history, setHistory] = useState<string | null>(null); // 展开一个规则历史，避免无意义轮询。
  const attempt = useRef<{ key: string; id: string } | null>(null); // 响应不明时保留原操作编号，手动重试不能另建触发。
  const mutation = useMutation({
    mutationFn: /* 只提交当前明确操作，失败重试复用相同规则/动作的编号。 */ ({
      rule,
      action,
    }: {
      rule: AutomationRule;
      action: Parameters<typeof automationAction>[1];
    }) => {
      const key = `${rule.id}:${rule.revision}:${action}`;
      if (attempt.current?.key !== key) attempt.current = { key, id: crypto.randomUUID() };
      return automationAction(rule, action, attempt.current.id);
    },
    onSuccess: /* 宿主确认后展开本次执行历史，下一次点击才表示新操作。 */ (_result, variables) => {
      attempt.current = null;
      if (variables.action === 'run') setHistory(variables.rule.id);
      refresh();
    },
  });
  const read = useMutation({ mutationFn: readAutomationNotification, onSuccess: refresh });
  /** 重新读取规则、授权和历史，成功响应不在客户端猜测状态。 */
  function refresh(): void {
    void cache.invalidateQueries({ queryKey: ['automations'] });
    void cache.invalidateQueries({ queryKey: ['automation-history'] });
    void cache.invalidateQueries({ queryKey: ['automation-review'] });
    void cache.invalidateQueries({ queryKey: ['automation-notifications'] });
  }
  /** 关闭表单并回读持久状态；保存不会自动启用规则。 */
  function saved(): void {
    setEditing(undefined);
    setGranting(null);
    refresh();
  }

  return (
    <section className="page-section automations-page">
      <div className="automation-heading">
        <h1>自动化</h1>
        <Button
          type="primary"
          disabled={!options.data?.recent_projects.length}
          onClick={/* 新建只打开草稿。 */ () => setEditing(null)}
        >
          新建自动化
        </Button>
      </div>
      {(rules.error || mutation.error || options.error || read.error) && (
        <Alert
          type="error"
          showIcon
          title={errorMessage(rules.error || mutation.error || options.error || read.error)}
        />
      )}
      {rules.data?.error && <Alert type="error" title={rules.data.error} />}
      {rules.data?.queue_paused && (
        <Alert
          type="warning"
          title={
            <>
              后台队列已暂停，自动化不会绕过。<Link to="/jobs">查看后台工作</Link>
            </>
          }
        />
      )}
      {rules.data?.items.length === 0 && <p>保存可重复执行的任务，再选择触发时间或本机事件。</p>}
      <div className="automation-rules">
        {rules.data?.items.map(
          /* 规则与触发分开呈现，列表不会自动启动。 */ (rule) => (
            <article key={rule.id} className="automation-rule">
              <div className="automation-rule-title">
                <h2>{rule.definition.name}</h2>
                <Switch
                  aria-label={'启用' + rule.definition.name}
                  checked={rule.enabled}
                  loading={mutation.isPending && mutation.variables?.rule.id === rule.id}
                  onChange={
                    /* 启停都绑定当前规则版本。 */ (enabled) =>
                      mutation.mutate({ rule, action: enabled ? 'enable' : 'pause' })
                  }
                />
              </div>
              <p className="automation-task">{rule.definition.task.prompt}</p>
              <p>
                {rule.definition.schedule.kind === 'event'
                  ? rule.definition.schedule.event === 'completed'
                    ? '普通任务执行完成时'
                    : '普通任务验收失败时'
                  : timeLabel(rule.next_at, rule.definition.schedule.timezone)}
                {rule.definition.schedule.kind !== 'event' &&
                  ' · ' + rule.definition.schedule.timezone}
                {rule.has_grant && ' · 已保存持续授权'}
              </p>
              {rule.invalid_reason && <Alert type="warning" title={rule.invalid_reason} />}
              <div className="automation-actions">
                <Button
                  onClick={
                    /* 手动试跑仍经过同一账本、权限和重叠策略。 */ () =>
                      mutation.mutate({ rule, action: 'run' })
                  }
                  disabled={mutation.isPending}
                >
                  立即运行
                </Button>
                <Button onClick={/* 编辑草稿不改已保存定义。 */ () => setEditing(rule)}>
                  编辑
                </Button>
                <Button onClick={/* 打开审核不表示批准。 */ () => setGranting(rule)}>
                  审核授权
                </Button>
                <Button
                  onClick={/* 历史只读。 */ () => setHistory(history === rule.id ? null : rule.id)}
                >
                  触发历史
                </Button>
                <Button
                  danger
                  onClick={
                    /* 单项停止及后代清理由原队列负责。 */ () =>
                      mutation.mutate({ rule, action: 'stop' })
                  }
                  disabled={mutation.isPending}
                >
                  停止当前执行
                </Button>
                {rule.has_grant && (
                  <Button
                    danger
                    onClick={
                      /* 撤销同时阻止旧运行的新动作。 */ () =>
                        mutation.mutate({ rule, action: 'revoke' })
                    }
                    disabled={mutation.isPending}
                  >
                    撤销授权
                  </Button>
                )}
              </div>
              {history === rule.id && <RuleHistory rule={rule} />}
            </article>
          ),
        )}
      </div>
      <div className="automation-inbox">
        <h2>需要关注</h2>
        {inbox.error && <Alert type="error" title={errorMessage(inbox.error)} />}
        {inbox.data?.items.length === 0 && <p>暂时没有通知</p>}
        {inbox.data?.items.map(
          /* 已读记录仍可回看；重复失败通过count合并。 */ (item) => (
            <article key={item.id} className={item.read ? 'is-read' : ''}>
              <div>
                <strong>{item.name}</strong>
                <span>
                  {item.message}
                  {item.count > 1 && `（${item.count}次）`}
                </span>
              </div>
              {item.job_id ? (
                <Link
                  to={'/jobs/' + item.job_id}
                  onClick={/* 用户进入结果时确认已读。 */ () => read.mutate(item.id)}
                >
                  查看与处理
                </Link>
              ) : (
                <Button onClick={/* 无作业的准备错误也能标记已读。 */ () => read.mutate(item.id)}>
                  标为已读
                </Button>
              )}
            </article>
          ),
        )}
      </div>
      {editing !== undefined && (
        <RuleEditor
          rule={editing}
          projects={options.data?.recent_projects || []}
          onClose={/* 关闭保留已保存规则。 */ () => setEditing(undefined)}
          onSaved={saved}
        />
      )}
      {granting && (
        <GrantDialog
          rule={granting}
          allowed={Boolean(rules.data?.grants_allowed)}
          onClose={/* 关闭不授予许可。 */ () => setGranting(null)}
          onSaved={saved}
        />
      )}
    </section>
  );
}
