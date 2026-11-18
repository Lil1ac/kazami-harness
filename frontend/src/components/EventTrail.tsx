import { Alert, Button, Empty, Tag } from 'antd';
import { CheckOutlined, ToolOutlined, MessageOutlined, LoadingOutlined } from '@ant-design/icons';
import type { ConnectionState } from '../hooks/useRunEvents';
import { duration, pretty } from '../format';
import { formatContextBytes, instructionStatusLabels } from '../context';
import { budgetHandoffTriggerLabels, publicSelfTestReason, publicSelfTestStatusLabels, toolFailureMessage } from '../activity';
import { lifecycleOutcomes, lifecycleStages, lifecycleTrace } from '../lifecycle';
import type { BudgetHandoffTrigger, PublicSelfTestStatus, RunEvent } from '../types';

const eventLabels: Record<string, string> = {
  pipeline_stage: '生命周期阶段',
  pipeline_feedback: '生命周期反馈',
  hooks_prepared: '固定 Hooks 定义',
  hooks_restored: '接续 Hooks 阶段与开销',
  hook_record: 'Hook 执行状态',
  hook_tool_receipt: '保存原工具回执',
  hook_feedback: '交付 Hook 反馈',
  hook_stop_retry: '按完成检查继续处理',
  hook_queued: '接纳生命周期事件',
  hook_dequeued: '生命周期事件已处理',
  permissions_prepared: '固定执行权限',
  permission_requested: '等待动作审批',
  automation_authorized: '自动化精确授权已核验',
  permission_resuming: '按批准接续原动作',
  tool_requested: '接纳工具请求',
  subagent_snapshot: '子 Agent 进度',
  team_snapshot: '更新团队状态',
  team_messages_delivered: '交付团队消息',
  subagent_report_retry: '纠正子 Agent 交接报告',
  subagent_reports_delivered: '交付子 Agent 报告',
  subagent_resume_reports: '交付接续报告',
  subagent_handoff: '收到子报告，重新决定后续动作',
  budget_handoff_started: '预算收尾交接',
  closeout_advice: '可编辑收尾提醒',
  budget_handoff_finished: '记录收尾答复',
  budget_handoff_skipped: '未执行预算收尾',
  public_self_test_started: '开始公开自测',
  public_self_test_finished: '公开自测结果',
  public_self_test_invalidated: '公开自测证据已失效',
  run_created: '任务已登记',
  run_started: '开始执行',
  planning_updated: '更新规划与用户请求',
  history_loaded: '继承前轮历史',
  context_prepared: '准备项目指令',
  memory_prepared: '准备项目记忆',
  memory_read: '回读项目记忆',
  memory_proposed: '保存记忆候选',
  memory_selected: '选择本次输入记忆',
  memory_failed: '项目记忆不可用',
  memory_history_projected: '刷新跨轮记忆依据',
  context_checked: '检查输入预算',
  context_action: '上下文输入变换',
  context_compaction_started: '生成历史工作摘要',
  context_summary_response: '历史摘要模型返回',
  model_request_started: '请求模型',
  model_http_attempt: 'HTTP 请求尝试',
  http_attempt_started: 'HTTP 请求',
  http_attempt_finished: 'HTTP 请求完成',
  http_attempt_failed: 'HTTP 请求失败',
  model_response: '模型响应',
  assistant_message: '模型消息',
  model_delta: '模型增量',
  tool_started: '工具调用',
  file_write_preparing: '登记文件写入',
  file_write_intent: '文件写入意图已保存',
  crash_resumed: '已接续中断运行',
  tool_result: '工具返回',
  execution_completed: '执行完成',
  verification_started: '开始独立验收',
  verification_result: '验收结果',
  repair_started: '开始自动修复',
  repair_stopped: '自动修复停止',
  verification_skipped: '未执行验收',
  run_finished: '运行结束',
  cancellation_received: '收到取消',
  artifact_collection_started: '收集本轮文件',
  artifacts_collected: '文件收集结束',
  artifact_registered: '登记产物',
};
const connectionLabels: Record<ConnectionState, string> = {
  loading: '读取历史',
  live: '实时连接',
  reconnecting: '连接中断，正在补齐',
  closed: '历史记录',
  error: '连接异常',
};

/** 根据已知事件生成简短摘要；不尝试将工具内容当 HTML 或 Markdown 执行。 */
function eventSummary(event: RunEvent): string {
  const payload = event.payload;
  const lifecycle = lifecycleTrace(event);
  if (lifecycle) {
    const last = lifecycle.components.at(-1)!;
    return `${lifecycleStages[lifecycle.stage]} · ${last.name} · ${lifecycleOutcomes[last.outcome]}`;
  }
  if (event.type === 'hook_record') {
    const hook = payload.hook as { key: string; event: string; status: string };
    const labels: Record<string, string> = { running: '执行中', waiting: '等待批准', completed: '已完成', failed: '失败', cancelled: '已取消', skipped: '已跳过' }; // 保持脚本状态独立于原工具。
    return `${hook.key} · ${hook.event} · ${labels[hook.status] || hook.status}`;
  }
  if (event.type === 'subagent_snapshot')
    return String((payload.subagent as { title?: string } | undefined)?.title || '只读委派');
  if (event.type === 'repair_started') return `第 ${String(payload.repair_attempt)} / 2 次修复`;
  if (event.type === 'repair_stopped') return String(payload.reason ?? '');
  if (event.type === 'context_prepared') {
    const instruction = payload.instruction as { status?: unknown } | undefined;
    const status = String(instruction?.status ?? '');
    return `AGENTS.md · ${instructionStatusLabels[status] ?? status}`;
  }
  if (event.type === 'context_checked') {
    const size =
      typeof payload.input_bytes === 'number' ? formatContextBytes(payload.input_bytes) : '未记录';
    const decision =
      payload.allowed === true ? '允许请求' : payload.allowed === false ? '达到上限' : '未记录';
    const phase = payload.phase === 'handoff' ? '紧凑收尾 · ' : payload.phase === 'execution' ? '普通输入 · ' : '';
    return `${phase}第 ${String(payload.model_turn ?? '—')} 轮 · ${size} · ${decision}`;
  }
  if (event.type === 'closeout_advice') {
    const extras = Array.isArray(payload.other_writes) ? payload.other_writes.join('、') : '';
    return `第 ${String(payload.model_turn)} 轮 · 剩余 ${String(payload.remaining)} 轮 · ${String(payload.target)}`
      + (payload.current_target_write ? '（已核对写入）' : '（无当前写入回执）')
      + (extras ? ` · 其他写入 ${extras}` : '') + ' · 未执行自测';
  }
  if (event.type === 'public_self_test_started') return String(payload.selector ?? '');
  if (event.type === 'public_self_test_invalidated') return publicSelfTestReason(payload.reason);
  if (event.type === 'public_self_test_finished') {
    const status = String(payload.status ?? '') as PublicSelfTestStatus;
    const label = publicSelfTestStatusLabels[status] ?? status;
    const executed = typeof payload.tests_run === 'number' ? ` · 实际执行 ${payload.tests_run} 项` : '';
    const reason = publicSelfTestReason(payload.reason);
    return label + executed + (reason && reason !== label ? ' · ' + reason : '');
  }
  if (event.type === 'budget_handoff_started' || event.type === 'budget_handoff_skipped') {
    const trigger = String(payload.trigger ?? '') as BudgetHandoffTrigger;
    return [budgetHandoffTriggerLabels[trigger], publicSelfTestReason(payload.reason)].filter(Boolean).join(' · ');
  }
  if (event.type === 'budget_handoff_finished') {
    const labels: Record<string, string> = { ready: '已答复', partial: '部分完成', invalid: '答复无效' }; // 收尾答复状态独立于评分。
    return labels[String(payload.status)] ?? '';
  }
  if (event.type === 'context_action')
    return (
      String(payload.kind ?? '') +
      ' · ' +
      formatContextBytes(Number(payload.before_bytes ?? 0)) +
      ' → ' +
      formatContextBytes(Number(payload.after_bytes ?? 0))
    );
  if (event.type === 'model_request_started' && payload.purpose === 'summary')
    return '生成历史工作摘要';
  if (event.type === 'tool_started' || event.type === 'tool_result') {
    const failure =
      event.type === 'tool_result' && ['edit_file', 'search_files', 'read_file'].includes(String(payload.name))
        ? toolFailureMessage(payload.result)
        : null;
    return String(payload.name ?? '') + (failure ? ` · ${failure}` : '');
  }
  if (event.type === 'model_request_started')
    return `第 ${String(payload.model_turn ?? '—')} 轮 · ${String(payload.requested_model ?? '')}`;
  if (event.type === 'assistant_message') {
    const message = payload.message as { content?: unknown } | undefined;
    return typeof message?.content === 'string'
      ? message.content.slice(0, 120)
      : '模型请求使用工具';
  }
  if (event.type === 'verification_result')
    return String((payload.verification as { reason?: unknown } | undefined)?.reason ?? '');
  if (event.type === 'verification_skipped')
    return payload.reason === 'not_configured' ? '本次没有配置独立验收' : '执行未完成，跳过验收';
  return '';
}

/** 事件轨迹参数；loadOlder 只补历史，不新建 SSE 连接。 */
interface EventTrailProps {
  events: RunEvent[]; // 有界、去重、按序的事件。
  connection: ConnectionState; // 唯一订阅状态。
  error: string | null; // 读取失败时的受控提示。
  canLoadOlder: boolean; // 是否可以扩大历史窗口。
  loadingOlder: boolean; // 历史页请求状态。
  loadOlder: () => Promise<void>; // 用户主动向前读取事件。
}

/** 展示真实事件时间线与可展开载荷；工具参数/结果保留原始语义并安全转义。 */
export function EventTrail({
  events,
  connection,
  error,
  canLoadOlder,
  loadingOlder,
  loadOlder,
}: EventTrailProps) {
  return (
    <section className="event-trail" aria-label="执行轨迹">
      <div className="section-heading">
        <span className={`connection-state ${connection}`}>
          <i />
          {connectionLabels[connection]}
        </span>
      </div>
      {error && <Alert type="warning" showIcon title={error} />}
      {canLoadOlder && (
        <Button
          block
          type="text"
          loading={loadingOlder}
          onClick={/* 点击才扩大内存窗口，上限 1000 条。 */ () => void loadOlder()}
        >
          加载更早事件
        </Button>
      )}
      {!events.length && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无事件，等待后端写入记录" />
      )}
      <ol className="event-list">
        {events.map(
          /* 每条事件的展开状态由原生 details 独立管理。 */ (event) => (
            <li key={`${event.run_id}:${event.seq}`} className={`event-row event-${event.type}`}>
              <span className="event-symbol">
                {event.type.startsWith('tool') ? (
                  <ToolOutlined />
                ) : event.type === 'run_finished' ? (
                  <CheckOutlined />
                ) : event.type.startsWith('model') ? (
                  <MessageOutlined />
                ) : (
                  <span className="event-dot" />
                )}
              </span>
              <details>
                <summary>
                  <span className="event-main">
                    <strong>{event.type === 'assistant_message' && event.payload.source === 'host_action_resume' ? '宿主接续原动作' : eventLabels[event.type] || event.type}</strong>
                    <span className="event-summary">{eventSummary(event)}</span>
                  </span>
                  <span className="event-meta">
                    #{event.seq}
                    <time>{duration(event.elapsed_ms)}</time>
                  </span>
                </summary>
                <div className="event-expanded">
                  <Tag variant="filled">{event.type}</Tag>
                  <pre>{pretty(event.payload)}</pre>
                </div>
              </details>
            </li>
          ),
        )}
      </ol>
      {connection === 'live' && (
        <div className="trace-waiting">
          <LoadingOutlined /> 等待下一条事件
        </div>
      )}
      <p className="muted trace-footnote">
        当前显示 {events.length} 条事件，最多展开 1,000 条。完整事实保存在后端运行记录中。
      </p>
    </section>
  );
}
