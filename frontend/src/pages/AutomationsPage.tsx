import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Dropdown,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Switch,
  Tabs,
} from 'antd';
import { ClockCircleOutlined, MoreOutlined, PlusOutlined, RightOutlined } from '@ant-design/icons';
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
import type {
  AutomationDefinition,
  AutomationFire,
  AutomationRule,
  AutomationSchedule,
} from '../automationTypes';
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

/** 将五段 Cron 解析为可编辑预设；cron 为原表达式，返回频率、时分及星期，复杂表达式保持 custom。 */
function cronParts(cron: string | null): { frequency: string; time: string; day: string } {
  const fields = (cron || '').trim().split(/\s+/);
  const [minute, hour, date, month, day] = fields;
  if (
    fields.length === 5 &&
    /^\d+$/.test(minute) &&
    /^\d+$/.test(hour) &&
    Number(minute) < 60 &&
    Number(hour) < 24 &&
    date === '*' &&
    month === '*'
  ) {
    const frequency =
      day === '*'
        ? 'daily'
        : day === 'mon-fri'
          ? 'weekdays'
          : /^(mon|tue|wed|thu|fri|sat|sun)$/.test(day)
            ? 'weekly'
            : 'custom';
    return { frequency, time: `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`, day };
  }
  return { frequency: 'custom', time: '09:00', day: 'mon' };
}

/** 将公开计划转换为列表摘要；schedule 为持久触发条件，返回不依赖浏览器时区的中文说明。 */
function scheduleLabel(schedule: AutomationSchedule): string {
  if (schedule.kind === 'event')
    return schedule.event === 'completed' ? '普通任务执行完成时' : '普通任务验收失败时';
  if (schedule.kind === 'once') return `一次性 · ${timeLabel(schedule.at, schedule.timezone)}`;
  if (schedule.kind === 'interval') return `每 ${schedule.interval_minutes} 分钟`;
  const parts = cronParts(schedule.cron);
  const days: Record<string, string> = {
    mon: '一',
    tue: '二',
    wed: '三',
    thu: '四',
    fri: '五',
    sat: '六',
    sun: '日',
  }; // 与后端星期名称保持一致。
  const frequency =
    parts.frequency === 'daily'
      ? '每天'
      : parts.frequency === 'weekdays'
        ? '工作日'
        : parts.frequency === 'weekly'
          ? `每周${days[parts.day]}`
          : null;
  return frequency ? `${frequency} ${parts.time}` : `Cron · ${schedule.cron}`;
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
    rule?.definition.task.project_id ||
      rule?.definition.task.workspace_id ||
      projects.find(/* 默认选可用登记项目。 */ (item) => item.available)?.id,
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
  const [customCron, setCustomCron] = useState(cronParts(schedule.cron).frequency === 'custom'); // 复杂表达式不被预设覆盖。
  const parts = cronParts(schedule.cron); // 时间控件始终反映当前待保存表达式。
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
    setCustomCron(false);
    preview.reset();
  }

  /** 根据频率、时分和星期生成唯一 Cron；参数为空沿用当前值，更新草稿并清除过期预览，不发起请求。 */
  function changeCadence(frequency = parts.frequency, time = parts.time, day = parts.day): void {
    if (frequency === 'custom') {
      setCustomCron(true);
      return;
    }
    const [hour, minute] = time.split(':').map(Number);
    if (!Number.isInteger(hour) || !Number.isInteger(minute)) return;
    setCustomCron(false);
    setSchedule({
      ...schedule,
      cron: `${minute} ${hour} * * ${frequency === 'daily' ? '*' : frequency === 'weekdays' ? 'mon-fri' : /^(mon|tue|wed|thu|fri|sat|sun)$/.test(day) ? day : 'mon'}`,
    });
    preview.reset();
  }

  useEffect(
    /* 任意计划字段改变后清除旧预览，防止把上一次时间当成当前结果。 */ () => {
      preview.reset();
    },
    [schedule],
  );

  /** 校验公开JSON配置并提交；无项目或无效JSON只显示错误，不发送任何任务。 */
  function submit(): void {
    if (!project?.available) return;
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
    <Drawer
      title={rule ? '编辑自动化' : '新建自动化'}
      open
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            loading={save.isPending}
            disabled={!project?.available || !name.trim() || !prompt.trim()}
            onClick={submit}
          >
            保存并保持暂停
          </Button>
        </>
      }
      size={620}
      className="automation-editor"
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
            <div className="automation-form-row">
              <label>
                重复
                <Select
                  aria-label="重复频率"
                  value={customCron ? 'custom' : parts.frequency}
                  onChange={/* 频率切换只修改草稿。 */ (value) => changeCadence(value)}
                  options={[
                    { value: 'daily', label: '每天' },
                    { value: 'weekdays', label: '工作日' },
                    { value: 'weekly', label: '每周' },
                    { value: 'custom', label: '自定义 Cron' },
                  ]}
                />
              </label>
              {!customCron && (
                <label>
                  时间
                  <Input
                    type="time"
                    aria-label="触发时间"
                    value={parts.time}
                    onChange={
                      /* 浏览器时分直接编码为指定时区的 Cron，不转成本机时区。 */ (event) =>
                        changeCadence(parts.frequency, event.target.value)
                    }
                  />
                </label>
              )}
            </div>
            {!customCron && parts.frequency === 'weekly' && (
              <label>
                星期
                <Select
                  aria-label="执行星期"
                  value={parts.day}
                  onChange={
                    /* 单周星期与时分一同写入表达式。 */ (day) =>
                      changeCadence('weekly', parts.time, day)
                  }
                  options={['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'].map(
                    /* 星期名称与后端一致。 */ (day, index) => ({
                      value: day,
                      label: `星期${'一二三四五六日'[index]}`,
                    }),
                  )}
                />
              </label>
            )}
            {customCron && (
              <label>
                Cron
                <Input
                  value={schedule.cron || ''}
                  onChange={
                    /* 复杂表达式交由后端校验。 */ (event) => {
                      setSchedule({ ...schedule, cron: event.target.value });
                      preview.reset();
                    }
                  }
                />
                <small>五段表达式；星期推荐 mon–sun，数字 0 表示周一。</small>
              </label>
            )}
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
        <details className="automation-advanced">
          <summary>高级设置</summary>
          <div className="automation-form">
            {schedule.kind !== 'event' && (
              <>
                <label className="automation-inline">
                  <Switch
                    checked={schedule.catch_up}
                    onChange={
                      /* 用户明确选择才补跑。 */ (catch_up) =>
                        setSchedule({ ...schedule, catch_up })
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
          </div>
        </details>
        <small>
          电脑与宿主需保持运行。保存或编辑后均暂停；启用不会自动批准未来方案和未授权动作。
        </small>
      </div>
    </Drawer>
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

/** 展示当前规则的真实触发记录；rule 决定时区，query 复用详情查询结果，返回只读历史及原作业链接。 */
function RuleHistory({
  rule,
  query,
}: {
  rule: AutomationRule;
  query: { data?: { items: AutomationFire[] }; error: Error | null; isPending: boolean };
}) {
  return (
    <div className="automation-history">
      {query.isPending && <Skeleton active paragraph={{ rows: 2 }} />}
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
  const [modal, modalContext] = Modal.useModal(); // 确认框继承当前主题，异步失败时保留确认状态。
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
  const [selectedId, setSelectedId] = useState<string | null>(null); // 仅保存编号，每次轮询采用最新规则版本。
  const [tab, setTab] = useState('tasks'); // 任务与需要关注分别呈现。
  const [detailTab, setDetailTab] = useState('overview'); // 当前规则详情页签。
  const selected = rules.data?.items.find(
    /* 详情随服务快照更新。 */ (rule) => rule.id === selectedId,
  );
  const activity = useQuery({
    queryKey: ['automation-history', selectedId],
    queryFn: /* 仅打开规则后读取真实活动，不逐行请求历史。 */ () =>
      getAutomationHistory(selectedId!),
    enabled: Boolean(selectedId),
    refetchInterval: 2000,
  });
  const hasActiveRun = activity.data?.items.some(
    /* 仅存在活动执行时显示停止。 */ (fire) =>
      [
        'pending',
        'dispatching',
        'queued',
        'preparing',
        'running',
        'waiting_input',
        'coalesced',
      ].includes(fire.status),
  );
  const unread =
    inbox.data?.items.filter(/* 未读数来自持久通知。 */ (item) => !item.read).length || 0;
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
      if (variables.action === 'run') {
        setSelectedId(variables.rule.id);
        setDetailTab('history');
      }
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
      {modalContext}
      <div className="automation-heading">
        <h1>自动化</h1>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          aria-label="新建自动化"
          disabled={
            !options.data?.recent_projects.some(
              /* 只有可用项目才能新建。 */ (project) => project.available,
            )
          }
          onClick={/* 新建只打开草稿。 */ () => setEditing(null)}
        >
          新建自动化
        </Button>
      </div>
      {(rules.error || (!selected && mutation.error) || options.error || read.error) && (
        <Alert
          type="error"
          showIcon
          title={errorMessage(
            rules.error || (!selected && mutation.error) || options.error || read.error,
          )}
        />
      )}
      {rules.data?.error && <Alert type="error" title={rules.data.error} />}
      {rules.data?.queue_paused && (
        <Alert
          type="warning"
          title={
            <>
              运行队列已暂停，自动化将继续等待。<Link to="/jobs">查看运行队列</Link>
            </>
          }
        />
      )}
      <Tabs
        activeKey={tab}
        onChange={setTab}
        destroyOnHidden
        items={[
          {
            key: 'tasks',
            label: '任务',
            children: (
              <div className="automation-rules" aria-label="自动化任务">
                {rules.isPending && <Skeleton active paragraph={{ rows: 3 }} />}
                {rules.data?.items.length === 0 && (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有自动化任务" />
                )}
                {!!rules.data?.items.length && (
                  <div className="automation-list-heading" aria-hidden="true">
                    <span>任务</span>
                    <span>触发计划</span>
                    <span>状态</span>
                  </div>
                )}
                {rules.data?.items.map(
                  /* 点击只查看详情，开关独立控制启停。 */ (rule) => (
                    <article key={rule.id} className="automation-rule">
                      <button
                        className="automation-rule-open"
                        aria-label={'查看' + rule.definition.name}
                        onClick={
                          /* 新规则从概览打开。 */ () => {
                            setSelectedId(rule.id);
                            setDetailTab('overview');
                          }
                        }
                      >
                        <span className="automation-rule-icon">
                          <ClockCircleOutlined />
                        </span>
                        <span className="automation-rule-name">
                          <strong>{rule.definition.name}</strong>
                          <small>
                            {options.data?.recent_projects.find(
                              /* 项目展示使用登记名称。 */ (project) =>
                                project.id ===
                                (rule.definition.task.project_id ||
                                  rule.definition.task.workspace_id),
                            )?.label ||
                              rule.definition.task.project_id ||
                              rule.definition.task.workspace_id}
                          </small>
                        </span>
                        <span className="automation-rule-schedule">
                          {scheduleLabel(rule.definition.schedule)}
                          <small>
                            {rule.definition.schedule.kind === 'event'
                              ? '本机事件'
                              : rule.definition.schedule.timezone}
                          </small>
                        </span>
                        <RightOutlined className="automation-rule-chevron" />
                      </button>
                      <div className="automation-rule-state">
                        <span
                          className={
                            rule.invalid_reason
                              ? 'automation-state-warning'
                              : rule.enabled
                                ? 'automation-state-enabled'
                                : ''
                          }
                        >
                          {rule.invalid_reason ? '需处理' : rule.enabled ? '已启用' : '已暂停'}
                        </span>
                        <Switch
                          size="small"
                          aria-label={'启用' + rule.definition.name}
                          checked={rule.enabled}
                          disabled={mutation.isPending}
                          onChange={
                            /* 启停绑定当前版本，不隐式授权。 */ (enabled) =>
                              mutation.mutate({ rule, action: enabled ? 'enable' : 'pause' })
                          }
                        />
                      </div>
                    </article>
                  ),
                )}
              </div>
            ),
          },
          {
            key: 'inbox',
            label: (
              <span>
                需要关注{unread > 0 && <span className="automation-unread">{unread}</span>}
              </span>
            ),
            children: (
              <div className="automation-inbox">
                {inbox.isPending && <Skeleton active />}
                {inbox.error && <Alert type="error" title={errorMessage(inbox.error)} />}
                {inbox.data?.items.length === 0 && (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂时没有通知" />
                )}
                {inbox.data?.items.map(
                  /* 已读仍可回看，进入原作业才标记已读。 */ (item) => (
                    <article key={item.id} className={item.read ? 'is-read' : ''}>
                      <div>
                        <strong>
                          {!item.read && <span className="automation-unread-dot" />}
                          {item.name}
                        </strong>
                        <span>
                          {item.message}
                          {item.count > 1 && `（${item.count}次）`}
                        </span>
                      </div>
                      {item.job_id ? (
                        <Link
                          to={'/jobs/' + item.job_id}
                          onClick={/* 跳转确认读取。 */ () => read.mutate(item.id)}
                        >
                          查看与处理 <RightOutlined />
                        </Link>
                      ) : (
                        <Button
                          type="text"
                          disabled={item.read || read.isPending}
                          onClick={/* 无作业通知也能确认读取。 */ () => read.mutate(item.id)}
                        >
                          {item.read ? '已读' : '标为已读'}
                        </Button>
                      )}
                    </article>
                  ),
                )}
              </div>
            ),
          },
        ]}
      />
      <Drawer
        open={Boolean(selected)}
        title={selected?.definition.name}
        size={620}
        onClose={/* 关闭详情即停止历史轮询。 */ () => setSelectedId(null)}
        className="automation-detail"
        destroyOnHidden
      >
        {selected && (
          <>
            <div className="automation-detail-actions">
              <Button
                type="primary"
                disabled={mutation.isPending}
                onClick={
                  /* 试跑仍经过同一账本。 */ () =>
                    mutation.mutate({ rule: selected, action: 'run' })
                }
              >
                立即运行
              </Button>
              <Button onClick={/* 编辑不改变已存定义。 */ () => setEditing(selected)}>编辑</Button>
              <Dropdown
                trigger={['click']}
                menu={{
                  items: [
                    { key: 'grant', label: '审核授权' },
                    ...(selected.has_grant
                      ? [{ key: 'revoke', label: '撤销授权', danger: true }]
                      : []),
                  ],
                  onClick: /* 撤销需明确确认，菜单打开不执行动作。 */ ({ key }) => {
                    if (key === 'grant') setGranting(selected);
                    if (key === 'revoke')
                      modal.confirm({
                        title: '撤销持续授权？',
                        content: '规则将暂停，当前执行也会停止；再次运行时，需审批的动作将重新等待确认。',
                        okText: '撤销授权',
                        cancelText: '取消',
                        okButtonProps: { danger: true },
                        onOk: /* 确认后等待宿主持久化，失败保留弹窗。 */ () =>
                          mutation.mutateAsync({ rule: selected, action: 'revoke' }),
                      });
                  },
                }}
              >
                <Button aria-label="更多操作" icon={<MoreOutlined />} />
              </Dropdown>
            </div>
            {mutation.error && <Alert type="error" title={errorMessage(mutation.error)} />}
            {selected.invalid_reason && <Alert type="warning" title={selected.invalid_reason} />}
            <Tabs
              activeKey={detailTab}
              onChange={setDetailTab}
              items={[
                {
                  key: 'overview',
                  label: '概览',
                  children: (
                    <div className="automation-overview">
                      <div className="automation-detail-status">
                        <span>{selected.enabled ? '已启用' : '已暂停'}</span>
                        <Switch
                          aria-label={'详情启用' + selected.definition.name}
                          checked={selected.enabled}
                          disabled={mutation.isPending}
                          onChange={
                            /* 详情采用相同版本校验。 */ (enabled) =>
                              mutation.mutate({
                                rule: selected,
                                action: enabled ? 'enable' : 'pause',
                              })
                          }
                        />
                      </div>
                      <dl>
                        <dt>触发计划</dt>
                        <dd>{scheduleLabel(selected.definition.schedule)}</dd>
                        {selected.definition.schedule.kind !== 'event' && (
                          <>
                            <dt>时区</dt>
                            <dd>{selected.definition.schedule.timezone}</dd>
                            <dt>下次执行</dt>
                            <dd>
                              {selected.enabled
                                ? timeLabel(selected.next_at, selected.definition.schedule.timezone)
                                : '启用后按计划执行'}
                            </dd>
                          </>
                        )}
                        <dt>持续授权</dt>
                        <dd>
                          {selected.has_grant ? '已保存精确授权' : '未设置，需审批的动作将等待处理'}{' '}
                          <Button
                            type="link"
                            size="small"
                            onClick={/* 审阅本身不授予权限。 */ () => setGranting(selected)}
                          >
                            审核授权
                          </Button>
                        </dd>
                      </dl>
                      <section>
                        <h3>任务内容</h3>
                        <p className="automation-task">{selected.definition.task.prompt}</p>
                      </section>
                    </div>
                  ),
                },
                {
                  key: 'history',
                  label: '运行记录',
                  children: (
                    <>
                      <RuleHistory rule={selected} query={activity} />
                      {hasActiveRun && (
                        <Button
                          danger
                          disabled={mutation.isPending}
                          onClick={
                            /* 仅停止已有活动执行。 */ () =>
                              mutation.mutate({ rule: selected, action: 'stop' })
                          }
                        >
                          停止当前执行
                        </Button>
                      )}
                    </>
                  ),
                },
              ]}
            />
          </>
        )}
      </Drawer>
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
