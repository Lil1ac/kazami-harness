import { Alert, Button, Empty, Tag } from 'antd';
import { CheckOutlined, ToolOutlined, MessageOutlined, LoadingOutlined } from '@ant-design/icons';
import type { ConnectionState } from '../hooks/useRunEvents';
import { duration, pretty } from '../format';
import { formatContextBytes, instructionStatusLabels } from '../context';
import type { RunEvent } from '../types';

const eventLabels: Record<string, string> = {
  run_created: '任务已登记',
  run_started: '开始执行',
  history_loaded: '继承前轮历史',
  context_prepared: '准备项目指令',
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
  tool_started: '工具调用',
  tool_result: '工具返回',
  execution_completed: '执行完成',
  verification_started: '开始独立验收',
  verification_result: '验收结果',
  verification_skipped: '未执行验收',
  run_finished: '运行结束',
  cancellation_received: '收到取消',
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
    return `第 ${String(payload.model_turn ?? '—')} 轮 · ${size} · ${decision}`;
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
  if (event.type === 'tool_started' || event.type === 'tool_result')
    return String(payload.name ?? '');
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
    <section className="event-trail" aria-labelledby="trail-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">EXECUTION TRACE</span>
          <h2 id="trail-heading">执行轨迹</h2>
        </div>
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
                    <strong>{eventLabels[event.type] || event.type}</strong>
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
