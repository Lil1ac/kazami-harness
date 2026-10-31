import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Input, Radio } from 'antd';
import {
  ArrowRightOutlined,
  CheckOutlined,
  DownOutlined,
  DownloadOutlined,
  EditOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import { MarkdownMessage } from './MarkdownMessage';
import { planMarkdownUrl } from '../api';
import type { HumanDecision, PlanRevision, PlanningState, UserInteraction } from '../types';
import './PlanningPanel.css';

const planLabels: Record<PlanRevision['status'], string> = {
  pending: '待批准',
  approved: '已批准',
  rejected: '已拒绝',
  superseded: '已被替代',
}; // 用户对方案的决定；步骤全部完成也不改变批准或验收结论。
const stepLabels: Record<PlanRevision['steps'][number]['status'], string> = {
  pending: '待处理',
  in_progress: '进行中',
  completed: '已完成',
  blocked: '受阻',
}; // 由工作项投影的执行进度，独立验收仍从本轮结果查看。

/** 按 plan 的原始顺序展示完整步骤，返回带可读状态的清单，不改写步骤正文或进度。 */
function PlanSteps({ plan }: { plan: PlanRevision }) {
  return (
    <ol className="plan-steps">
      {plan.steps.map(
        /* 稳定步骤编号与 AgentTask 来源对应；图标之外保留文字，读屏无需辨认颜色。 */ (
          step,
          index,
        ) => (
          <li key={index} className={`plan-step plan-step-${step.status}`}>
            <span className="plan-step-marker" aria-hidden="true">
              {step.status === 'completed' ? <CheckOutlined /> : index + 1}
            </span>
            <div className="plan-step-copy">
              <span>{step.text}</span>
              <small>{stepLabels[step.status]}</small>
            </div>
          </li>
        ),
      )}
    </ol>
  );
}

/** 展示 plan 的全部固定正文，返回只读内容；结构化字段原样保留，不猜测或删减 Markdown。 */
function PlanContent({ plan }: { plan: PlanRevision }) {
  return (
    <div className="plan-content">
      <section className="plan-section" aria-label="目标与架构">
        <h4>目标与架构</h4>
        <MarkdownMessage text={plan.summary} />
      </section>
      <section className="plan-section" aria-label="改动范围">
        <h4>改动范围</h4>
        <ul>
          {plan.scope.map(
            /* 范围属于当前固定版本，不作为可执行指令解释。 */ (item, index) => (
              <li key={index}>{item}</li>
            ),
          )}
        </ul>
      </section>
      <section className="plan-section" aria-label="执行步骤">
        <h4>执行步骤</h4>
        <PlanSteps plan={plan} />
      </section>
      <section className="plan-section" aria-label="验收方式">
        <h4>验收方式</h4>
        <ul>
          {plan.verification.map(
            /* 展示计划中的检查方式，不冒充已经通过的证据。 */ (item, index) => (
              <li key={index}>{item}</li>
            ),
          )}
        </ul>
      </section>
      <details className="plan-workspace">
        <summary>执行目录</summary>
        <code>{plan.workspace}</code>
      </details>
    </div>
  );
}

/** 将 plan 呈现为计划块；runId 固定导出来源，actions 缺省时只提供阅读和导出。 */
function PlanCard({
  runId,
  plan,
  actions,
}: {
  runId: string; // 当前快照所属运行，导出不跟随会话最新轮自动切换。
  plan: PlanRevision; // 要审阅或回看的版本及固定进度。
  actions?: ReactNode; // 仅当前待批准请求提供操作区，历史不传。
}) {
  const [expanded, setExpanded] = useState(plan.status === 'pending'); // 待批准展开全文，批准后切换为紧凑进度。
  const contentId = useId(); // 折叠按钮与阅读区的稳定关联，不含运行授权信息。
  const completed = plan.steps.filter(
    /* 仅计入模型明确标记完成的步骤。 */ (step) => step.status === 'completed',
  ).length;
  const current =
    plan.steps.find(
      /* 受阻优先提示，避免把无法推进的计划展示成正常执行。 */ (step) => step.status === 'blocked',
    ) ??
    plan.steps.find(
      /* 后端保证最多一个进行中步骤；页面不自行推进。 */ (step) => step.status === 'in_progress',
    );
  return (
    <article className={`plan-card plan-card-${plan.status}`} aria-label={`计划 v${plan.version}`}>
      <header className="plan-heading">
        <div className="plan-eyebrow">
          <FileTextOutlined aria-hidden="true" />
          <span>实施计划</span>
          <span className="plan-version">v{plan.version}</span>
        </div>
        <span className={`plan-status plan-status-${plan.status}`}>
          <span aria-hidden="true" />
          {planLabels[plan.status]}
        </span>
      </header>
      <h3 className="plan-title">{plan.title}</h3>
      <div className="plan-overview">
        <span className="plan-count" title="工作项执行进度；验收结论见本轮结果">
          已完成 {completed} / {plan.steps.length} 步
        </span>
        <div className="plan-reading-actions">
          <a
            className="plan-export"
            href={planMarkdownUrl(runId, plan.version)}
            download
            aria-label={`导出计划 v${plan.version} 为 Markdown`}
            title="导出 Markdown"
          >
            <DownloadOutlined aria-hidden="true" />
            导出
          </a>
          <button
            type="button"
            className="plan-disclosure"
            aria-expanded={expanded}
            aria-controls={contentId}
            onClick={/* 展开只改变阅读状态，不发送用户决定。 */ () => setExpanded(!expanded)}
          >
            {expanded ? '收起计划' : '查看计划'}
            <DownOutlined aria-hidden="true" />
          </button>
        </div>
      </div>
      {plan.status === 'approved' && (
        <progress
          className="plan-progress"
          aria-label="计划步骤进度"
          max={plan.steps.length}
          value={completed}
        />
      )}
      {!expanded && current && (
        <p className={`plan-current plan-current-${current.status}`}>
          <span>{stepLabels[current.status]}</span>
          {current.text}
        </p>
      )}
      <div
        id={contentId}
        hidden={!expanded}
        className="plan-reading"
        role="region"
        aria-label={`计划 v${plan.version} 正文`}
        tabIndex={expanded ? 0 : -1}
      >
        <PlanContent plan={plan} />
      </div>
      {actions}
    </article>
  );
}

/** 展示当前 request 的问题；disabled 阻止编辑与发送，onDecision 只在用户点击时收到回答或取消。 */
function QuestionForm({
  request,
  disabled,
  onDecision,
}: {
  request: UserInteraction; // 后端固定的问题及建议答案，不提供默认批准。
  disabled: boolean; // 活动运行或发送未确认期间锁定，防止改写已发答案。
  onDecision: (action: HumanDecision['action'], answer: string) => void; // 交给父级幂等发送器，组件不创建运行。
}) {
  const [answer, setAnswer] = useState(''); // 当前问题的自由回答；切换请求时由父级 key 清空。
  return (
    <div className="planning-question">
      <div className="plan-eyebrow">需要你确认</div>
      <MarkdownMessage text={request.question} />
      {!!request.options.length && (
        <Radio.Group
          aria-label="建议答案"
          disabled={disabled}
          value={answer}
          onChange={
            /* 选择只编辑答案，必须明确点击才发送。 */ (event) => setAnswer(event.target.value)
          }
          options={request.options.map(
            /* 建议项与自由输入共用同一答案正文。 */ (value) => ({ label: value, value }),
          )}
        />
      )}
      <Input.TextArea
        aria-label="回答问题"
        placeholder="选择上方选项，或输入你的回答…"
        disabled={disabled}
        value={answer}
        maxLength={8000}
        autoSize={{ minRows: 2, maxRows: 6 }}
        onChange={
          /* 自由输入覆盖选择，不改变问题请求身份。 */ (event) => setAnswer(event.target.value)
        }
      />
      <div className="plan-buttons">
        <Button
          type="primary"
          icon={<ArrowRightOutlined aria-hidden="true" />}
          disabled={disabled || !answer.trim()}
          onClick={/* 问题回答永不转换成计划批准。 */ () => onDecision('answer', answer.trim())}
        >
          回答并继续
        </Button>
        <Button
          type="text"
          disabled={disabled}
          onClick={
            /* 取消只结束当前等待，迟到回答不能恢复。 */ () =>
              onDecision('cancel', '取消本次问题等待')
          }
        >
          取消等待
        </Button>
      </div>
    </div>
  );
}

/** 展示 request 绑定版本的计划操作；disabled 时全部锁定，反馈仅由 revise 明确提交，不推断文字批准。 */
function PlanDecisions({
  request,
  disabled,
  onDecision,
}: {
  request: UserInteraction; // 必须是当前版本的待批准请求，父级核对后才渲染。
  disabled: boolean; // 发送期间及无法继续时禁用；不能靠展开表单绕过。
  onDecision: (action: HumanDecision['action'], answer: string) => void; // 由父级附加请求编号与版本并处理防重。
}) {
  const [editing, setEditing] = useState(false); // 修改表单按需展开，减少审阅时的无关输入。
  const [answer, setAnswer] = useState(''); // 返回审阅后保留反馈草稿，但不自动发送。
  return (
    <div className="plan-decisions">
      {editing ? (
        <>
          <Input.TextArea
            autoFocus
            aria-label="计划反馈"
            placeholder="说明需要调整的内容…"
            disabled={disabled}
            value={answer}
            maxLength={8000}
            autoSize={{ minRows: 3, maxRows: 6 }}
            onChange={/* 编辑反馈不会改变当前批准状态。 */ (event) => setAnswer(event.target.value)}
          />
          <div className="plan-buttons">
            <Button
              type="primary"
              disabled={disabled || !answer.trim()}
              onClick={
                /* 修改产生重新规划，不执行当前版本。 */ () => onDecision('revise', answer.trim())
              }
            >
              提交修改意见
            </Button>
            <Button
              type="text"
              disabled={disabled}
              onClick={/* 返回仅关闭编辑区，保留本次草稿且不发送。 */ () => setEditing(false)}
            >
              返回计划
            </Button>
          </div>
        </>
      ) : (
        <div className="plan-buttons">
          <Button
            type="primary"
            icon={<ArrowRightOutlined aria-hidden="true" />}
            disabled={disabled}
            onClick={
              /* 批准绑定实际审阅版本，不接受普通文字猜测授权。 */ () =>
                onDecision('approve', `批准执行计划 v${request.plan_version}`)
            }
          >
            批准并执行
          </Button>
          <Button
            icon={<EditOutlined aria-hidden="true" />}
            disabled={disabled}
            onClick={/* 打开编辑不是决定，不创建新运行。 */ () => setEditing(true)}
          >
            要求修改
          </Button>
          <Button
            type="text"
            className="plan-reject"
            disabled={disabled}
            onClick={
              /* 拒绝只保存决定，宿主不调用模型与工具。 */ () =>
                onDecision('reject', answer.trim() || `拒绝执行计划 v${request.plan_version}`)
            }
          >
            拒绝计划
          </Button>
        </div>
      )}
    </div>
  );
}

/** 统一显示 state 的当前计划和历史；disabled 默认锁定，没有 onDecision 时不提供任何回答或批准入口。 */
export function PlanningPanel({
  runId,
  state,
  disabled = true,
  onDecision,
}: {
  runId: string; // 当前规划快照所属运行，所有版本的导出都绑定该轮记录。
  state: PlanningState; // 宿主快照中的模式、版本、问题与用户决定，浏览器不修改。
  disabled?: boolean; // 仅最新轮且父级允许继续时传 false，默认禁止操作。
  onDecision?: (action: HumanDecision['action'], answer: string) => void; // 历史检查器不传，始终只读。
}) {
  const plan = state.plans.at(-1);
  const request = state.pending;
  if (!plan && !request && state.phase !== 'planning') return null;
  return (
    <section className="planning-panel" aria-label="规划与决策">
      {plan && (
        <PlanCard
          key={`${plan.version}:${plan.status}`}
          runId={runId}
          plan={plan}
          actions={
            request?.kind === 'plan_approval' &&
            request.plan_version === plan.version &&
            plan.status === 'pending' &&
            onDecision ? (
              <PlanDecisions
                key={request.id}
                request={request}
                disabled={disabled}
                onDecision={onDecision}
              />
            ) : undefined
          }
        />
      )}
      {!plan && !request && (
        <p className="planning-placeholder" role="status">
          尚未提交计划 · {state.reason || '等待形成可审阅方案'}
        </p>
      )}
      {request?.kind === 'question' &&
        (onDecision ? (
          <QuestionForm
            key={request.id}
            request={request}
            disabled={disabled}
            onDecision={onDecision}
          />
        ) : (
          <div className="planning-question">
            <div className="plan-eyebrow">当时的问题</div>
            <MarkdownMessage text={request.question} />
          </div>
        ))}
      {state.plans.length > 1 && (
        <details className="plan-history">
          <summary>
            历史版本<span>{state.plans.length - 1}</span>
          </summary>
          {state.plans.slice(0, -1).map(
            /* 历史方案只读，不能复用当前版本的决定入口。 */ (previous) => (
              <details key={previous.version} className="plan-history-version">
                <summary>
                  v{previous.version} · {previous.title}
                  <span>{planLabels[previous.status]}</span>
                </summary>
                <a
                  className="plan-export"
                  href={planMarkdownUrl(runId, previous.version)}
                  download
                  aria-label={`导出历史计划 v${previous.version} 为 Markdown`}
                >
                  导出 Markdown
                </a>
                <PlanContent plan={previous} />
              </details>
            ),
          )}
        </details>
      )}
      {!!state.decisions.length && (
        <details className="plan-history">
          <summary>
            决定记录<span>{state.decisions.length}</span>
          </summary>
          <ul className="plan-decision-log">
            {state.decisions.map(
              /* 决定原文仅按文本呈现，不重放或解释其指令。 */ (decision, index) => (
                <li key={index}>
                  {decision.plan_version ? <span>v{decision.plan_version}</span> : null}
                  {decision.text}
                </li>
              ),
            )}
          </ul>
        </details>
      )}
    </section>
  );
}
