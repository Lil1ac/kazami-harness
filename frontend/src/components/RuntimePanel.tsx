import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button } from 'antd';
import { Link } from 'react-router';
import { dispatchNextInput, errorMessage, getRuntime, retractRuntimeInput } from '../api';
import type { RuntimeMessage } from '../runtimeTypes';
import { lastFailedCommand } from '../runOutput';
import type { OutputTarget } from '../runOutput';
import './RuntimePanel.css';

const messageLabels: Record<RuntimeMessage['status'], string> = {
  received: '已接收 · 待处理',
  delivered: '已投递 · 待确认',
  processed: '已处理',
  retracted: '已撤回',
  queued: '已排队',
  forwarded: '已转入后续执行',
}; // 接纳成功不能直接标记模型已遵循。

/** 展示本轮输入回执与命令摘要；活动轮刷新，历史或归档禁止修改。 */
export function RuntimePanel({
  runId,
  active,
  readOnly = false,
  onOpenOutput,
}: {
  runId: string; // 当前查看的运行编号。
  active: boolean; // 活动轮需要刷新快照；历史只读取。
  readOnly?: boolean; // 归档及历史禁止命令停止和输入写入。
  onOpenOutput: (target: OutputTarget) => void; // 直接定位实际失败的命令或最近一次命令。
}) {
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ['runtime', runId],
    queryFn: /* 读取持久事实，不恢复旧任务。 */ () => getRuntime(runId),
    refetchInterval: active ? 1500 : false,
    retry: false,
  });
  const retract = useMutation({
    mutationFn: /* 只能撤回尚未投递的 id，冲突由宿主返回。 */ (id: string) =>
      retractRuntimeInput(runId, id),
    onSuccess: /* 重新读取回执，不在页面猜测撤回是否成功。 */ () => {
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
    },
  });
  const dispatch = useMutation({
    mutationFn: /* 必须用户明确操作才恢复未自动入队的请求。 */ () =>
      dispatchNextInput(runId, crypto.randomUUID()),
    onSuccess: /* 后台作业与当前会话一起更新。 */ () => {
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
      void cache.invalidateQueries({ queryKey: ['jobs'] });
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
  });
  const data = query.data;
  const messages =
    data?.state?.messages.filter(
      /* 已撤回的要求不再占用会话阅读区域。 */ (item) => item.status !== 'retracted',
    ) || [];
  const commands = data?.commands || [];
  const failure = lastFailedCommand(commands);
  const command = failure ?? commands.at(-1);
  if (!messages.length && !commands.length && data?.state?.status !== 'needs_review') return null;
  return (
    <section className="runtime-panel" aria-label="命令与运行中要求">
      {data?.state?.status === 'needs_review' && (
        <Alert
          type="warning"
          title="上次执行未留下安全停止记录；请先核对进程与工作区，旧命令不会自动重放。"
        />
      )}
      {(retract.error || dispatch.error) && (
        <Alert type="error" title={errorMessage(retract.error || dispatch.error)} />
      )}
      {messages.map(
        /* 每条输入使用宿主稳定身份，不与普通续轮消息合并。 */ (message) => (
          <div className="runtime-message" key={message.id}>
            <div className="runtime-message-heading">
              <span>
                你 · {message.delivery === 'steer' ? '本轮补充' : '下一轮'} ·{' '}
                {messageLabels[message.status]}
              </span>
              {!readOnly &&
                message.status === 'received' &&
                !Object.keys(message.consumers).length && (
                  <Button
                    type="text"
                    size="small"
                    loading={retract.isPending}
                    onClick={/* 已投递的内容由后端拒绝撤回。 */ () => retract.mutate(message.id)}
                  >
                    撤回
                  </Button>
                )}
              {message.job_id && <Link to={`/jobs/${message.job_id}`}>查看作业</Link>}
            </div>
            <div className="runtime-message-body">{message.prompt}</div>
            {message.error && <p role="alert">{message.error}</p>}
            {!readOnly &&
              !active &&
              message.delivery === 'next' &&
              message.status === 'received' && (
                <Button
                  size="small"
                  loading={dispatch.isPending}
                  onClick={/* 只明确投递已保存的原请求。 */ () => dispatch.mutate()}
                >
                  发送下一轮
                </Button>
              )}
          </div>
        ),
      )}
      {command && (
        <button
          type="button"
          className="runtime-output-link"
          data-failed={Boolean(failure)}
          onClick={
            /* 定位实际失败的命令，避免先打开同一轮的检查或成功命令。 */ () =>
              onOpenOutput(`command:${command.id}`)
          }
        >
          {failure ? '查看命令错误' : '查看命令输出'}
          {commands.length > 1 && <span>{commands.length}</span>}
        </button>
      )}
    </section>
  );
}
