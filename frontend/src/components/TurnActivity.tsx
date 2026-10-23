import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  LoadingOutlined,
  MinusOutlined,
} from '@ant-design/icons';
import { getEvents, errorMessage } from '../api';
import { activitySteps } from '../activity';
import { pretty } from '../format';
import type { RunEvent } from '../types';

/** 轮次过程输入；liveEvents 只为当前活动订阅的轮次提供，历史轮按展开动作读取。 */
interface TurnActivityProps {
  runId: string; // 对应真实运行。
  liveEvents?: RunEvent[]; // 当前轮共享的有界事件，不另建SSE。
  active: boolean; // 该轮后台任务是否仍活动。
  phase: string; // 活动阶段的受控中文描述。
  onInspect: () => void; // 打开该轮完整轨迹，不改变会话执行目标。
}

/** 将真实工具/验收事件收进可展开过程；历史读取有界，完整细节通过右侧轨迹继续查看。 */
export function TurnActivity({ runId, liveEvents, active, phase, onInspect }: TurnActivityProps) {
  const [open, setOpen] = useState(false); // 默认折叠，避免工具 JSON 淹没回答。
  const query = useQuery({
    queryKey: ['turn-activity', runId],
    queryFn: /* 历史只读一页，展开后才发请求，不订阅或重放旧工具。 */ ({ signal }) =>
      getEvents(runId, 0, 100, signal),
    enabled: open && liveEvents === undefined,
    staleTime: Infinity,
    retry: false,
  });
  const steps = activitySteps(liveEvents ?? query.data?.items ?? [], active);
  return (
    <details
      className={`turn-activity ${active ? 'is-active' : ''}`}
      open={open}
      onToggle={
        /* 原生details同时支持鼠标与键盘，状态只控制历史读取。 */ (event) =>
          setOpen(event.currentTarget.open)
      }
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
        {steps.length === 0 && !query.isLoading && (
          <p className="muted">
            {active ? '等待下一条执行记录。' : '当前窗口没有工具或验收记录。'}
          </p>
        )}
        <ol className="activity-steps">
          {steps.map(
            /* 结果图标只反映配对事件，不从模型自述推断。 */ (step) => (
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
            ),
          )}
        </ol>
        {query.data?.has_more && (
          <p className="muted">这里只显示前100条事件，请在完整轨迹中继续查看。</p>
        )}
        <button className="text-action" type="button" onClick={onInspect}>
          查看本轮完整轨迹
        </button>
      </div>
    </details>
  );
}
