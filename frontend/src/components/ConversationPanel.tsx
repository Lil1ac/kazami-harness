import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Input, Select, Popover, Tooltip } from 'antd';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  ReloadOutlined,
  UnorderedListOutlined,
  AuditOutlined,
  TeamOutlined,
  StopOutlined,
  PauseOutlined,
  SettingOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons';
import './ConversationComposer.css';
import { ModeSelect } from './ModeSelect';
import { phaseLabel, modelFailureLabel } from '../format';
import { useConversationSend } from '../hooks/useConversationSend';
import { useRuntimeControl } from '../hooks/useRuntimeControl';
import { RuntimePanel } from './RuntimePanel';
import { useRunPlanning } from '../hooks/useRunPlanning';
import { MarkdownMessage } from './MarkdownMessage';
import { TurnActivity } from './TurnActivity';
import { SkillSelector } from './SkillSelector';
import { McpSelector } from './McpSelector';
import type { McpToolOption } from '../mcpTypes';
import { PlanningPanel } from './PlanningPanel';
import { PermissionPanel, PermissionSelect } from './PermissionPanel';
import { MemoryPanel, SaveMemoryButton } from './MemoryPanel';
import { Link } from 'react-router';
import type { ConversationTurn, ConversationView, RunEvent, RunView, SkillOption } from '../types';

/** 对话工作区输入；run始终为最新轮，查看旧轮通过onInspect单独选择右侧记录。 */
interface ConversationPanelProps {
  mcpTools?: McpToolOption[]; // 仅供下一轮选择，不覆盖当轮历史。
  run?: RunView; // 最新快照暂缺时保留消息与编辑，确认旧请求之外禁止新提交。
  conversation: ConversationView; // 当前会话的有序消息与继续条件。
  events: RunEvent[]; // 最新轮共享订阅，不为每条消息创建SSE。
  skills?: SkillOption[]; // 当前宿主目录仅用于下一条的选择，不改写旧轮证据。
  onInspect: (runId: string, tab?: string, commandCallId?: string) => void; // 查看某轮详情或命令，不更改追加父轮。
  decisionRef?: React.Ref<HTMLDivElement>; // 详情跳回待处理决定时，用于定位对话内唯一审批区。
  onConfirmed: (run: RunView) => void; // 服务确认新运行后原地更新会话与路由。
  onStop?: () => void; // 用户点击停止后由宿主取消当前运行；缺省不提供停止入口。
  stopping?: boolean; // 宿主正在清理或取消请求尚未返回。
}

/** 简洁说明单轮真实结论；未知和未验收不被展示为成功。 */
function turnStatus(
  status: string | null,
  validation: string | null,
  interrupted = false,
  reason?: string | null,
): string {
  if (!interrupted && status === 'failed' && modelFailureLabel(reason))
    return modelFailureLabel(reason)!;
  if (!interrupted && reason === 'planning_complete') return '规划已结束';
  if (!interrupted && reason === 'plan_rejected') return '已拒绝计划';
  if (!interrupted && reason === 'user_paused') return '已暂停';
  const labels: Record<string, string> = {
    created: '已创建',
    running: '执行中',
    completed: '执行完成',
    failed: '执行失败',
    cancelled: '已取消',
    timed_out: '已超时',
    limit_exceeded: '达到运行限制',
    waiting_input: '等待你的决定',
  };
  const checks: Record<string, string> = {
    passed: '检查通过',
    failed: '检查未通过',
    error: '检查异常',
    unverified: '未验收',
  };
  return `${interrupted ? '运行已中断' : status ? labels[status] || status : '记录待确认'}${validation ? ` · ${checks[validation] || validation}` : ''}`;
}

/** 展示一轮用户要求对应的执行、回答和详情；历史轮只读取原记录。 */
function TurnMessage({
  turn,
  run,
  latestId,
  events,
  onInspect,
  readOnly,
  onRevealActivity,
}: {
  turn: ConversationTurn; // 本轮唯一已接纳运行及独立结果。
  run?: RunView; // 最新运行快照，缺省时不伪造活动状态。
  latestId: string; // 唯一实时订阅对应的运行。
  events: RunEvent[]; // 最新运行共享的真实事件。
  onInspect: ConversationPanelProps['onInspect']; // 只改变检查器选择。
  readOnly: boolean; // 归档会话仍展示旧消息，但不提供会话写入入口。
  onRevealActivity: () => void; // 用户展开过程时暂停自动滚动。
}) {
  const selected = turn.run_id === run?.run_id;
  const answer = selected && run?.active ? null : turn.final_answer; // 宿主检查和自动修复结束前，文字仍属于过程。
  const interrupted = turn.interrupted ?? (selected && Boolean(run?.interrupted));
  return (
    <div className="assistant-message">
      <div className="assistant-content">
        <div className="assistant-heading">
          <strong>Agent</strong>
          <span>第 {turn.turn_index} 轮</span>
          <button
            type="button"
            className="text-button turn-inspect"
            onClick={/* 每轮详情始终指向本轮独立记录。 */ () => onInspect(turn.run_id, 'result')}
            aria-label={'查看第 ' + turn.turn_index + ' 轮详情'}
          >
            本轮详情
          </button>
        </div>
        <TurnActivity
          runId={turn.run_id}
          liveEvents={turn.run_id === latestId ? events : undefined}
          active={Boolean(selected && run?.active)}
          phase={phaseLabel(run?.phase || 'created')}
          finalAnswer={answer}
          onReveal={onRevealActivity}
          onOpenOutput={/* 工具调用只定位本轮日志，不会重放命令。 */ (callId) =>
            onInspect(turn.run_id, 'output', callId)
          }
          onInspect={
            /* 查看本轮轨迹，不改变下一条消息的发送目标。 */ () =>
              onInspect(turn.run_id, 'diagnostics')
          }
        />
        {selected && run?.result.planning.pending?.kind === 'question' ? (
          <p className="message-waiting">需要你确认下面的问题。</p>
        ) : answer ? (
          <MarkdownMessage text={answer} />
        ) : selected && run?.active ? null : (
          <p className="message-waiting">
            {turn.execution_status
              ? turnStatus(turn.execution_status, null, interrupted, turn.stop_reason) +
                '，本次执行没有最终回答。'
              : '运行记录待确认，请查看本次详情。'}
          </p>
        )}
        <div className="message-actions">
          {answer && !readOnly && <SaveMemoryButton runId={turn.run_id} text={answer} />}
          {(!selected || !run?.active) && (
            <span>
              {turnStatus(
                turn.execution_status,
                turn.validation_status,
                interrupted,
                turn.stop_reason,
              )}
            </span>
          )}
          {selected && (run.project || Boolean(run.local_changes?.files.length)) && (
            <button
              type="button"
              onClick={/* 代码差异和审阅属于所选轮。 */ () => onInspect(turn.run_id, 'files')}
            >
              代码改动
            </button>
          )}
        </div>
        <RuntimePanel
          runId={turn.run_id}
          active={Boolean(selected && run?.active)}
          readOnly={readOnly || !selected}
          display="conversation"
          onOpenOutput={/* 当轮只有命令摘要，完整日志在输出页。 */ () => onInspect(turn.run_id, 'output')}
        />
      </div>
    </div>
  );
}

/** 持续展示同一session的消息和底部输入栏；只在用户位于底部时跟随真实新内容。 */
export function ConversationPanel({
  run,
  conversation,
  events,
  onInspect,
  decisionRef,
  onConfirmed,
  onStop,
  stopping = false,
  skills = [],
  mcpTools = [],
}: ConversationPanelProps) {
  const send = useConversationSend(conversation.session_id, run, conversation, onConfirmed);
  const runtime = useRuntimeControl(run?.run_id, Boolean(run?.active));
  const { tasks, planning } = useRunPlanning(run);
  const viewport = useRef<HTMLDivElement>(null); // 唯一消息纵向滚动区域。
  const content = useRef<HTMLDivElement>(null); // 仅在跟随时用尺寸变化补偿流式内容或字体加载。
  const atBottom = useRef(true); // 用户上翻后关闭跟随，不打断历史阅读。
  const composing = useRef(false); // 中文输入法选字期间不能把Enter当发送。
  const [showJump, setShowJump] = useState(false); // 上翻后提供明确回到底部操作。

  /** 将消息区域移到最新位置；只改变浏览器滚动，不重新读取或执行任务。 */
  function jumpToLatest(): void {
    if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    atBottom.current = true;
    setShowJump(false);
  }

  /** 用户主动展开历史内容时停在触发位置；再次跟随由滚动到底或按钮触发。 */
  function pauseFollowing(): void {
    atBottom.current = false;
    setShowJump(true);
  }
  useLayoutEffect(
    /* 新消息、过程或发送状态改变时仅跟随底部读者。 */ () => {
      if (atBottom.current) jumpToLatest();
    },
    [conversation.turns, events, send.pending, run?.phase],
  );
  useEffect(
    /* 真实内容尺寸改变后保持底部跟随，卸载释放观察器。 */ () => {
      if (!content.current) return;
      const observer = new ResizeObserver(
        /* 上翻查看历史时不强行滚动。 */ () => {
          if (atBottom.current) jumpToLatest();
        },
      );
      observer.observe(content.current);
      return /* 会话卸载只释放尺寸订阅。 */ () => observer.disconnect();
    },
    [],
  );

  /** 原生表单提交与Enter共用同一防重入口；不会发送空白要求。 */
  function submit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    sendMessage();
  }

  /** 将当前草稿按活动状态投递；运行中输入固定意图，成功确认后才清除对应正文。 */
  function sendMessage(): void {
    if ((run?.active || runtime.pending) && conversation.management.state === 'active') {
      const original = send.text;
      void runtime.send(original).then(
        /* 不清空等待期间新写入的另一份草稿。 */ (confirmed) => {
          if (confirmed) send.setText((previous) => (previous === original ? '' : previous));
        },
      );
    } else send.send();
  }
  const pendingVisible =
    send.pending &&
    !conversation.turns.some(
      /* 未收到POST确认但GET已发现相同后续要求时，避免重复显示同一用户正文。 */ (turn) =>
        turn.parent_run_id === send.pending?.parentId && turn.prompt === send.pending?.body.prompt,
    );
  const taskItems =
    tasks.data?.items.filter(/* 分组不重复计入完成进度。 */ (item) => !item.is_group) || [];
  const completedTasks = taskItems.filter(
    /* 待复核项不展示为完成。 */ (item) => item.status === 'completed' && !item.blocked_reason,
  ).length;
  const currentTask = taskItems.find(
    /* 优先展示当前执行项。 */ (item) => item.status === 'in_progress',
  );
  return (
    <section className="conversation-surface" aria-label="连续对话">
      {run &&
      (taskItems.length > 0 ||
        planning?.plans.length ||
        run.result.subagents?.length ||
        run.result.team) ? (
        <div className="conversation-context-bar" aria-label="当前工作">
          {taskItems.length > 0 && (
            <button
              type="button"
              onClick={/* 打开任务，不改变消息和执行目标。 */ () => onInspect(run.run_id, 'tasks')}
            >
              <UnorderedListOutlined />
              <span>任务</span>
              <small>
                {completedTasks}/{taskItems.length}
              </small>
            </button>
          )}
          {Boolean(planning?.plans.length) && (
            <button
              type="button"
              onClick={/* 查看当前计划全文。 */ () => onInspect(run.run_id, 'plan')}
            >
              <AuditOutlined />
              <span>计划</span>
            </button>
          )}
          {Boolean(run.result.team || run.result.subagents?.length) && (
            <button
              type="button"
              onClick={/* 查看本轮子执行列表。 */ () => onInspect(run.run_id, 'agents')}
            >
              <TeamOutlined />
              <span>{run.result.team ? '团队' : 'Agent'}</span>
              <small>
                {run.result.team ? run.result.team.members.length : run.result.subagents!.length}
              </small>
            </button>
          )}
          {currentTask && (
            <span className="context-current-task" title={currentTask.subject}>
              {currentTask.subject}
            </span>
          )}
        </div>
      ) : null}
      <div
        className="conversation-scroll"
        ref={viewport}
        role="log"
        aria-label="会话消息"
        aria-live="polite"
        aria-relevant="additions text"
        tabIndex={0}
        onScroll={
          /* 距底部64px以外视为主动阅读历史，停止后续自动跟随。 */ () => {
            const node = viewport.current;
            if (!node) return;
            atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 64;
            setShowJump(!atBottom.current);
          }
        }
      >
        <div className="conversation-content" ref={content}>
          {conversation.turns.map(
            /* 每条用户消息与本轮执行一一对应，稳定保留历史阅读位置。 */ (turn) => (
              <article key={turn.run_id} className="chat-turn" data-run-id={turn.run_id}>
                <div className="user-message" aria-label="你的消息">
                  <div className="user-message-body">{turn.prompt}</div>
                </div>
                <TurnMessage
                  turn={turn}
                  run={run}
                  latestId={conversation.latest_run_id}
                  events={events}
                  onInspect={onInspect}
                  readOnly={conversation.management.state !== 'active'}
                  onRevealActivity={pauseFollowing}
                />
              </article>
            ),
          )}
          {pendingVisible && (
            <div className="user-message pending-message">
              <span className="message-author">你 · {send.sending ? '发送中' : '发送待确认'}</span>
              <div className="user-message-body">{send.pending!.body.prompt}</div>
            </div>
          )}
          {run?.context?.memory?.proposals?.length ? (
            <MemoryPanel runId={run.run_id} readOnly={conversation.management.state !== 'active'} />
          ) : null}
          {run && (planning?.pending || run.result.permissions.pending) && (
            <div ref={decisionRef} className="conversation-decisions" tabIndex={-1} aria-label="待处理决定">
              {planning?.pending && (
                <PlanningPanel runId={run.run_id} state={planning} disabled={!send.canDecide} onDecision={send.decide} />
              )}
              {run.result.permissions.pending && (
                <PermissionPanel
                  run={run}
                  disabled={!send.canDecide || conversation.management.state !== 'active'}
                  onDecision={send.decideAction}
                />
              )}
            </div>
          )}
        </div>
      </div>
      <div className="conversation-composer">
        {showJump && (
          <Button className="jump-latest" icon={<ArrowDownOutlined />} onClick={jumpToLatest}>
            回到最新
          </Button>
        )}
        <div className="composer-inner">
          {send.queuedJob && (
            <p role="status">
              请求已接纳 · <Link to={'/jobs/' + send.queuedJob.id}>查看排队详情</Link>
            </p>
          )}
          {runtime.error && <Alert type="error" title={runtime.error} />}
          {runtime.pending && !runtime.sending && (
            <p role="status">运行中输入尚未确认，重试会核对原内容。</p>
          )}
          {send.error && (
            <Alert
              type="error"
              showIcon
              title={send.confirming ? '发送结果尚未确认' : '发送未完成'}
              description={send.error}
            />
          )}
          {send.confirming && !send.error && (
            <p className="composer-notice" role="status">
              上一条发送尚未确认。点击“重试确认”核对原请求，草稿不会自动发送。
            </p>
          )}
          {conversation.blocked_reason &&
            conversation.blocked_code !== 'run_busy' &&
            !send.confirming && (
              <p className="composer-notice" role="status">
                {conversation.blocked_reason}
              </p>
            )}
          {conversation.continuation_notice && !conversation.blocked_reason && !send.confirming && (
            <p className="composer-notice" role="status">
              {conversation.continuation_notice}
            </p>
          )}
          {(run?.active || run?.result.stop_reason === 'user_paused') && (
            <div className="composer-run-state" aria-label="运行控制">
              <span className="composer-run-label" role="status">
                <span className={run.active ? 'run-indicator is-active' : 'run-indicator'} />
                {stopping
                  ? '正在停止…'
                  : run.active
                    ? runtime.query.data?.state?.status === 'pausing'
                      ? '正在安全暂停…'
                      : phaseLabel(run.phase || 'running')
                    : '已暂停'}
              </span>
              <div className="composer-run-actions">
                {run.active ? (
                  <>
                    {conversation.management.state === 'active' && (
                      <Button
                        type="text"
                        size="small"
                        aria-label="暂停"
                        icon={<PauseOutlined />}
                        loading={runtime.pausing}
                        disabled={stopping || runtime.query.data?.state?.status !== 'running'}
                        onClick={/* 暂停意图提交后，等待宿主确认安全收尾。 */ () => runtime.pause()}
                      >
                        暂停
                      </Button>
                    )}
                    {onStop && (
                      <Tooltip title="立即停止当前执行">
                        <Button
                          type="text"
                          size="small"
                          aria-label="停止当前执行"
                          icon={<StopOutlined />}
                          loading={stopping}
                          disabled={stopping}
                          onClick={onStop}
                        />
                      </Tooltip>
                    )}
                  </>
                ) : (
                  <Tooltip title="沿用原授权和剩余预算">
                    <Button
                      type="text"
                      size="small"
                      aria-label="恢复执行"
                      icon={<PlayCircleOutlined />}
                      onClick={send.resume}
                      disabled={
                        conversation.management.state !== 'active' ||
                        !conversation.can_continue ||
                        send.sending ||
                        !!send.pending
                      }
                    >
                      继续
                    </Button>
                  </Tooltip>
                )}
              </div>
            </div>
          )}
          <form className="message-composer" onSubmit={submit}>
            <Input.TextArea
              aria-label="输入消息"
              placeholder={run?.active ? '补充要求，或安排接下来的工作…' : '描述接下来要做的事…'}
              value={send.text}
              maxLength={65536}
              autoSize={{ minRows: 2, maxRows: 6 }}
              onChange={
                /* 执行期间也能预写下一条，切换检查轮次不会清空。 */ (event) =>
                  send.setText(event.target.value)
              }
              onCompositionStart={
                /* 输入法开始组词，暂时禁用Enter发送。 */ () => {
                  composing.current = true;
                }
              }
              onCompositionEnd={
                /* 组词结束后恢复正常快捷键。 */ () => {
                  composing.current = false;
                }
              }
              onKeyDown={
                /* Enter发送，Shift+Enter换行；兼容输入法的isComposing与229键码。 */ (event) => {
                  if (
                    event.key === 'Enter' &&
                    !event.shiftKey &&
                    !composing.current &&
                    !event.nativeEvent.isComposing &&
                    event.keyCode !== 229
                  ) {
                    event.preventDefault();
                    sendMessage();
                  }
                }
              }
            />
            <div className="composer-toolbar">
              {!run?.active && (
                <ModeSelect label="下一轮工作模式" value={send.mode} onChange={send.setMode} />
              )}
              <PermissionSelect
                value={send.permissionMode}
                onChange={send.setPermissionMode}
                disabled={
                  send.permissionBlocked ||
                  Boolean(run?.active) ||
                  run?.result.stop_reason === 'user_paused'
                }
              />
              {run && (
                <Popover
                  trigger="click"
                  placement="topLeft"
                  title="下一轮设置"
                  content={
                    <div className="composer-settings">
                      {' '}
                      <McpSelector
                        options={mcpTools}
                        selected={send.mcpTools}
                        onChange={send.setMcpTools}
                        disabled={Boolean(send.pending)}
                      />
                      <SkillSelector
                        label="下一轮技能"
                        options={skills}
                        selected={send.skills}
                        onChange={send.setSkills}
                        automatic={send.skillAuto}
                        onAutomaticChange={send.setSkillAuto}
                        disabled={!run}
                      />
                      <Checkbox
                        checked={send.memory.use}
                        onChange={
                          /* 下一轮读取选择不改变当前快照。 */ (event) =>
                            send.setMemory({ ...send.memory, use: event.target.checked })
                        }
                      >
                        使用项目记忆
                      </Checkbox>
                      <Checkbox
                        checked={send.memory.capture}
                        onChange={
                          /* 候选仍需人工确认。 */ (event) =>
                            send.setMemory({ ...send.memory, capture: event.target.checked })
                        }
                      >
                        允许提出记忆候选
                      </Checkbox>
                      {run?.verification_kind === 'project_tests' && (
                        <Checkbox
                          checked={send.autoRepair}
                          onChange={
                            /* 下一条要求沿用自己的预算；重试使用原请求冻结值。 */ (event) =>
                              send.setAutoRepair(event.target.checked)
                          }
                        >
                          测试失败自动修复（最多2次）
                        </Checkbox>
                      )}
                      {send.confirming && (
                        <p className="muted">
                          重试确认仍使用原请求的技能选择；这里的修改用于下一条新消息。
                        </p>
                      )}
                    </div>
                  }
                >
                  <Tooltip title="技能、工具与下一轮设置">
                    <Button
                      type="text"
                      className="composer-settings-trigger"
                      aria-label="下一轮设置"
                      icon={<SettingOutlined />}
                    >
                      {send.skills.length > 0 ? `技能 ${send.skills.length}` : null}
                    </Button>
                  </Tooltip>
                </Popover>
              )}
              <div className="composer-send-area">
                {run?.active && (
                  <Select
                    className="composer-delivery"
                    aria-label="运行中消息生效时机"
                    value={runtime.delivery}
                    onChange={runtime.setDelivery}
                    disabled={Boolean(runtime.pending)}
                    variant="borderless"
                    size="small"
                    popupMatchSelectWidth={180}
                    options={[
                      { value: 'steer', label: '补充当前任务' },
                      { value: 'next', label: '排到下一轮' },
                    ]}
                  />
                )}
                <Button
                  type="primary"
                  htmlType="submit"
                  className="send-message"
                  aria-label={runtime.pending || send.confirming ? '重试确认' : '发送消息'}
                  disabled={
                    run?.active || runtime.pending
                      ? conversation.management.state !== 'active' ||
                        runtime.sending ||
                        (!runtime.pending &&
                          (!send.text.trim() ||
                            send.text.length > 16000 ||
                            runtime.query.data?.state?.status !== 'running'))
                      : !send.canSend
                  }
                  loading={runtime.sending || send.sending}
                  icon={
                    runtime.pending || send.confirming ? <ReloadOutlined /> : <ArrowUpOutlined />
                  }
                >
                  {runtime.pending || send.confirming
                    ? '重试确认'
                    : run?.active
                      ? runtime.delivery === 'next'
                        ? '排队'
                        : '补充'
                      : '发送'}
                </Button>
              </div>
            </div>
          </form>
        </div>
      </div>
    </section>
  );
}
