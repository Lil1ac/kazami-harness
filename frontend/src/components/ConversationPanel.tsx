import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Alert, Button, Input } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, ReloadOutlined } from '@ant-design/icons';
import { phaseLabel } from '../format';
import { useConversationSend } from '../hooks/useConversationSend';
import { MarkdownMessage } from './MarkdownMessage';
import { TurnActivity } from './TurnActivity';
import type { ConversationTurn, ConversationView, RunEvent, RunView } from '../types';

/** 对话工作区输入；run始终为最新轮，查看旧轮通过onInspect单独选择右侧记录。 */
interface ConversationPanelProps {
  run?: RunView; // 最新快照暂缺时保留消息与编辑，确认旧请求之外禁止新提交。
  conversation: ConversationView; // 当前会话的有序消息与继续条件。
  events: RunEvent[]; // 最新轮共享订阅，不为每条消息创建SSE。
  onInspect: (runId: string, tab?: string) => void; // 查看某轮详情，不更改追加父轮。
  onConfirmed: (run: RunView) => void; // 服务确认新运行后原地更新会话与路由。
}

/** 简洁说明单轮真实结论；未知和未验收不被展示为成功。 */
function turnStatus(status: string | null, validation: string | null, interrupted = false): string {
  const labels: Record<string, string> = {
    created: '已创建',
    running: '执行中',
    completed: '执行完成',
    failed: '执行失败',
    cancelled: '已取消',
    timed_out: '已超时',
    limit_exceeded: '达到运行限制',
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
}: {
  turn: ConversationTurn; // 本轮唯一已接纳运行及独立结果。
  run?: RunView; // 最新运行快照，缺省时不伪造活动状态。
  latestId: string; // 唯一实时订阅对应的运行。
  events: RunEvent[]; // 最新运行共享的真实事件。
  onInspect: (runId: string, tab?: string) => void; // 只改变检查器选择。
}) {
  const selected = turn.run_id === run?.run_id;
  const interrupted = turn.interrupted ?? (selected && Boolean(run?.interrupted));
  return (
    <div className="assistant-message">
      <span className="agent-avatar" aria-hidden="true">
        H
      </span>
      <div className="assistant-content">
        <div className="assistant-heading">
          <strong>Agent</strong>
          <span>第 {turn.turn_index} 轮</span>
        </div>
        <TurnActivity
          runId={turn.run_id}
          liveEvents={turn.run_id === latestId ? events : undefined}
          active={Boolean(selected && run?.active)}
          phase={phaseLabel(run?.phase || 'created')}
          onInspect={
            /* 查看本轮轨迹，不改变下一条消息的发送目标。 */ () => onInspect(turn.run_id, 'trace')
          }
        />
        {turn.final_answer ? (
          <MarkdownMessage text={turn.final_answer} />
        ) : (
          <p className="message-waiting">
            {selected && run?.active
              ? '正在处理你的要求，结果将在完成后显示。'
              : turn.execution_status
                ? turnStatus(turn.execution_status, null, interrupted) + '，本次执行没有最终回答。'
                : '运行记录待确认，请查看本次详情。'}
          </p>
        )}
        <div className="message-actions">
          <span>{turnStatus(turn.execution_status, turn.validation_status, interrupted)}</span>
          <button
            type="button"
            onClick={/* 每轮详情始终指向本轮独立记录。 */ () => onInspect(turn.run_id, 'overview')}
            aria-label={'查看第 ' + turn.turn_index + ' 轮详情'}
          >
            本轮详情
          </button>
          {run?.project && (
            <button
              type="button"
              onClick={/* 代码差异和审阅属于所选轮。 */ () => onInspect(turn.run_id, 'code')}
            >
              代码改动
            </button>
          )}
        </div>
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
  onConfirmed,
}: ConversationPanelProps) {
  const send = useConversationSend(conversation.session_id, run, conversation, onConfirmed);
  const viewport = useRef<HTMLDivElement>(null); // 唯一消息纵向滚动区域。
  const content = useRef<HTMLDivElement>(null); // 用尺寸变化补偿字体加载与过程展开。
  const atBottom = useRef(true); // 用户上翻后关闭跟随，不打断历史阅读。
  const composing = useRef(false); // 中文输入法选字期间不能把Enter当发送。
  const [showJump, setShowJump] = useState(false); // 上翻后提供明确回到底部操作。

  /** 将消息区域移到最新位置；只改变浏览器滚动，不重新读取或执行任务。 */
  function jumpToLatest(): void {
    if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    atBottom.current = true;
    setShowJump(false);
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
    send.send();
  }
  const pendingVisible =
    send.pending &&
    !conversation.turns.some(
      /* 未收到POST确认但GET已发现相同后续要求时，避免重复显示同一用户正文。 */ (turn) =>
        turn.parent_run_id === send.pending?.parentId && turn.prompt === send.pending?.body.prompt,
    );
  return (
    <section className="conversation-surface" aria-label="连续对话">
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
                <div className="user-message">
                  <span className="message-author">你</span>
                  <div className="user-message-body">{turn.prompt}</div>
                </div>
                <TurnMessage
                  turn={turn}
                  run={run}
                  latestId={conversation.latest_run_id}
                  events={events}
                  onInspect={onInspect}
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
        </div>
      </div>
      <div className="conversation-composer">
        {showJump && (
          <Button className="jump-latest" icon={<ArrowDownOutlined />} onClick={jumpToLatest}>
            回到最新
          </Button>
        )}
        <div className="composer-inner">
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
          {conversation.blocked_reason && !send.confirming && (
            <p className="composer-notice" role="status">
              {conversation.blocked_reason}
            </p>
          )}
          {conversation.continuation_notice && !conversation.blocked_reason && !send.confirming && (
            <p className="composer-notice" role="status">
              {conversation.continuation_notice}
            </p>
          )}
          <form className="message-composer" onSubmit={submit}>
            <Input.TextArea
              aria-label="输入消息"
              placeholder="继续描述你想调整的内容…"
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
                    send.send();
                  }
                }
              }
            />
            <div className="composer-toolbar">
              <span>
                {!run
                  ? '正在核对最新运行状态…'
                  : run.active
                    ? '执行中，可以先写下下一条要求'
                    : 'Enter 发送 · Shift+Enter 换行'}
              </span>
              <Button
                type="primary"
                htmlType="submit"
                className="send-message"
                aria-label={send.confirming ? '重试确认' : '发送消息'}
                disabled={!send.canSend}
                loading={send.sending}
                icon={send.confirming ? <ReloadOutlined /> : <ArrowUpOutlined />}
              >
                {send.confirming ? '重试确认' : '发送'}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </section>
  );
}
