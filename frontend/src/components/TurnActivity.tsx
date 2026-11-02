import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  LoadingOutlined,
  MinusOutlined,
  CodeOutlined,
} from '@ant-design/icons';
import { getEvents, errorMessage } from '../api';
import { activitySteps } from '../activity';
import { projectTranscript } from '../transcript';
import { pretty } from '../format';
import { MarkdownMessage } from './MarkdownMessage';
import type { RunEvent } from '../types';

/** 同一轮的可读过程；liveEvents 为共享投影，历史仅在展开后按页补齐。 */
interface TurnActivityProps {
  runId: string; // 过程所属运行。
  liveEvents?: RunEvent[]; // 当前轮累计的文字块和工具事实，不另建实时连接。
  active: boolean; // 后台是否仍在执行，不根据连接状态猜测。
  phase: string; // 无新文字时显示实际运行阶段。
  finalAnswer?: string | null; // 已固定答复从过程排除，避免正文重复。
  onInspect: () => void; // 打开完整证据视图，不触发执行。
}

/** 按发生顺序展示模型过程与工具；运行时展开，结束后收拢，完整参数按需读取。 */
export function TurnActivity({
  runId,
  liveEvents,
  active,
  phase,
  finalAnswer,
  onInspect,
}: TurnActivityProps) {
  const [open, setOpen] = useState(active); // 当前轮默认可见，历史默认折叠。
  useEffect(/* 可信终态到来后收起过程，不等待手动清理屏幕。 */ () => setOpen(active), [active]);
  const query = useQuery({
    queryKey: ['turn-activity', runId],
    queryFn: /* 所有页都经过同一投影，首个 100 条事件不会截断长答复。 */ async ({ signal }) => {
      let cursor = 0;
      let items: RunEvent[] = [];
      while (true) {
        const page = await getEvents(runId, cursor, 100, signal);
        items = projectTranscript(items, page.items, runId);
        if (!page.has_more || !page.items.length) return { items };
        cursor = page.items.at(-1)!.seq;
      }
    },
    enabled: open && liveEvents === undefined,
    staleTime: Infinity,
    retry: false,
  });
  const source = liveEvents ?? query.data?.items ?? [];
  const timeline = useMemo(
    /* 原始事件与共享投影都可作为输入。 */ () =>
      source.some(
        /* 只检测尚未投影的模型事件。 */ (event) =>
          ['model_delta', 'assistant_message'].includes(event.type),
      )
        ? projectTranscript([], source, runId)
        : source,
    [source, runId],
  );
  const steps = useMemo(
    /* 工具状态只从已配对回执确定。 */ () => activitySteps(timeline, active),
    [timeline, active],
  );
  const lastAnswer = [...timeline]
    .reverse()
    .find(
      /* 最后的完整无工具文字可能成为最终答复。 */ (event) =>
        event.type === 'assistant_stream' &&
        event.payload.channel === 'text' &&
        event.payload.complete &&
        !event.payload.has_tools,
    );
  const visible = timeline.filter(
    /* 最终答复在聊天正文只出现一次。 */ (event) =>
      event !== lastAnswer || !finalAnswer || event.payload.text !== finalAnswer,
  );
  const renderedTools = new Set<string>(); // 开始与结果在同一行更新，不追加两行。
  let verificationId = ''; // 多次修复后的验收分别配对，不复用第一次检查行。
  return (
    <details
      className={`turn-activity ${active ? 'is-active' : ''}`}
      open={open}
      onToggle={/* 只改变阅读状态。 */ (event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <DownOutlined className="activity-chevron" />
        {active ? <LoadingOutlined spin /> : <span className="activity-mark" />}
        <span>{active ? phase : '执行过程'}</span>
        {steps.length > 0 && <small>{steps.length} 项操作</small>}
      </summary>
      <div className="activity-body">
        {query.isLoading && <p className="muted">正在读取本轮过程…</p>}
        {query.error && <p role="alert">{errorMessage(query.error)}</p>}
        <ol className="activity-steps">
          {visible.map(
            /* 文字作为正文，操作作为紧凑状态行。 */ (event) => {
              if (event.type === 'assistant_stream')
                return (
                  <li
                    className={`activity-text ${event.payload.channel === 'reasoning' ? 'activity-reasoning' : ''}`}
                    key={String(event.payload.block_key)}
                  >
                    {event.payload.channel === 'reasoning' ? (
                      <details>
                        <summary>思考</summary>
                        <MarkdownMessage text={String(event.payload.text)} />
                      </details>
                    ) : (
                      <MarkdownMessage text={String(event.payload.text)} />
                    )}
                    {!active && !event.payload.complete && (
                      <small className="muted">输出未完成</small>
                    )}
                  </li>
                );
              if (event.type === 'tool_preparing')
                return (
                  <li className="activity-preparing" key={String(event.payload.block_key)}>
                    <CodeOutlined />
                    <span>
                      {String(event.payload.name || '工具调用')}
                      <small>{active ? '生成参数' : '参数未完成，未执行'}</small>
                    </span>
                    <details className="activity-payload">
                      <summary>参数</summary>
                      <pre>{String(event.payload.arguments_json || '')}</pre>
                    </details>
                  </li>
                );
              if (event.type === 'verification_started')
                verificationId = `verification-${event.seq}`;
              if (event.type === 'verification_result' && !verificationId)
                verificationId = `verification-${event.seq}`;
              if (event.type === 'repair_started')
                return (
                  <li className="activity-repair" key={event.seq}>
                    <LoadingOutlined />
                    <span>检查未通过，开始第 {String(event.payload.repair_attempt)} 次修复</span>
                  </li>
                );
              const identifier = event.type.startsWith('verification')
                ? verificationId
                : String(event.payload.call_id || '');
              const step = steps.find(
                /* 验收或工具使用投影出的真实状态。 */ (item) => item.id === identifier,
              );
              if (!step || renderedTools.has(step.id)) return null;
              renderedTools.add(step.id);
              return (
                <li key={step.id} className={`activity-${step.state}`}>
                  <span className="activity-icon">
                    {step.state === 'passed' ? (
                      <CheckOutlined />
                    ) : step.state === 'failed' ? (
                      <CloseOutlined />
                    ) : step.state === 'running' ? (
                      <LoadingOutlined spin />
                    ) : (
                      <MinusOutlined />
                    )}
                  </span>
                  <div>
                    <span className="activity-label">{step.label}</span>
                    {step.errorMessage && <p className="activity-error">{step.errorMessage}</p>}
                    <details className="activity-payload">
                      <summary>参数与结果</summary>
                      {step.arguments !== null && <pre>{pretty(step.arguments)}</pre>}
                      {step.result !== null && <pre>{pretty(step.result)}</pre>}
                      {step.state === 'unknown' && (
                        <p className="muted">记录尚未配对，不能确认结果。</p>
                      )}
                    </details>
                  </div>
                </li>
              );
            },
          )}
        </ol>
        {active && (
          <div className="working-indicator" role="status" aria-label="Agent 正在处理">
            <i />
            <i />
            <i />
          </div>
        )}
        {!active && !visible.length && !query.isLoading && (
          <p className="muted">本轮没有额外操作记录。</p>
        )}
        {!active && (
          <button type="button" className="text-button" onClick={onInspect}>
            查看完整轨迹
          </button>
        )}
      </div>
    </details>
  );
}
