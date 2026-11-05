import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button } from 'antd';
import { Link } from 'react-router';
import {
  dispatchNextInput,
  errorMessage,
  getCommandOutput,
  getRuntime,
  retractRuntimeInput,
  stopCommand,
} from '../api';
import type { CommandExecution, RuntimeMessage } from '../runtimeTypes';
import './RuntimePanel.css';

const commandLabels: Record<CommandExecution['status'], string> = {
  starting: '启动中',
  running: '运行中',
  stopping: '停止中',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已停止',
  needs_review: '待核对',
}; // 命令状态不等同宿主独立验收。
const messageLabels: Record<RuntimeMessage['status'], string> = {
  received: '已接收 · 待处理',
  delivered: '已投递 · 待确认',
  processed: '已处理',
  retracted: '已撤回',
  queued: '已排队',
  forwarded: '已转入后续执行',
}; // 接纳成功不能直接标记模型已遵循。

/** 展示 runId 下 command 的双流；readOnly 时不提供停止，输出只在展开后按字节增量读取。 */
function CommandRow({
  runId,
  command,
  readOnly,
}: {
  runId: string;
  command: CommandExecution;
  readOnly: boolean;
}) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(false); // 用户选择展开后才加载输出，避免后台列表大量读取。
  const [stream, setStream] = useState('stdout'); // 双流各自分页，切换时重置本地窗口。
  const [page, setPage] = useState({ offset: 0, content: '' }); // 最多保留 65536 个展示字符，完整输出仍可分页读取。
  const active = ['starting', 'running', 'stopping'].includes(command.status);
  const output = useQuery({
    queryKey: ['command-output', runId, command.id, stream, page.offset],
    queryFn: /* 偏移只来自服务端回执，不根据展示字符数推算原字节数。 */ () =>
      getCommandOutput(runId, command.id, command.run_id, stream, page.offset),
    enabled: open,
    refetchInterval: active ? 800 : false,
    retry: false,
  });
  useEffect(
    /* 每页追加一次并前进到宿主游标；零增量保持原页等待后续输出。 */ () => {
      const value = output.data?.output;
      if (!value || value.offset !== page.offset || value.next_offset <= page.offset) return;
      setPage((previous) => ({
        offset: value.next_offset,
        content: (previous.content + value.content).slice(-65536),
      }));
    },
    [output.data, page.offset],
  );
  const stop = useMutation({
    mutationFn: /* 以命令归属编号停止，不提交系统 PID。 */ () =>
      stopCommand(runId, command.id, command.run_id),
    onSuccess: /* 清理完成后重新核对所有命令状态。 */ () => {
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
    },
  });
  return (
    <details
      className="runtime-command"
      onToggle={/* 展开只影响输出订阅。 */ (event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <code>{command.argv.join(' ')}</code>
        <span>
          {commandLabels[command.status]}
          {command.result?.exit_code != null ? ` · ${command.result.exit_code}` : ''}
        </span>
      </summary>
      <div className="runtime-command-meta">
        <code>
          {command.workspace}
          {command.cwd !== '.' ? ` / ${command.cwd}` : ''}
        </code>
        {!readOnly && active && (
          <Button
            size="small"
            loading={stop.isPending}
            disabled={command.status === 'stopping'}
            onClick={/* 等待真实清理，成功后才更新停止状态。 */ () => stop.mutate()}
          >
            停止命令
          </Button>
        )}
      </div>
      {['unknown', 'incomplete'].includes(command.cleanup_status) && (
        <Alert type="warning" title="进程清理尚未确认，请核对本地进程和工作区。" />
      )}
      {(output.error || stop.error) && (
        <Alert type="error" title={errorMessage(output.error || stop.error)} />
      )}
      <label className="runtime-stream">
        输出{' '}
        <select
          aria-label="选择命令输出流"
          value={stream}
          onChange={
            /* 切换双流从各自开头读取。 */ (event) => {
              setStream(event.target.value);
              setPage({ offset: 0, content: '' });
            }
          }
        >
          <option value="stdout">标准输出</option>
          <option value="stderr">标准错误</option>
        </select>
      </label>
      <pre aria-label="命令输出">{page.content || (active ? '等待输出…' : '暂无输出')}</pre>
      {command.result?.truncated && (
        <p>模型预览已截断，原始输出按页保留；捕获上限仍以命令结果为准。</p>
      )}
    </details>
  );
}

/** 展示 runId 本段输入及父子命令；active 决定刷新，readOnly 禁止历史/归档管理，返回空视图时不占布局。 */
export function RuntimePanel({
  runId,
  active,
  readOnly = false,
}: {
  runId: string;
  active: boolean;
  readOnly?: boolean;
}) {
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ['runtime', runId],
    queryFn: /* 读取持久事实，不恢复旧任务。 */ () => getRuntime(runId),
    refetchInterval: active ? 1000 : false,
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
  const messages = data?.state?.messages.filter((item) => item.status !== 'retracted') || [];
  if (!messages.length && !data?.commands.length && data?.state?.status !== 'needs_review')
    return null;
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
      {data?.commands.map(
        /* 命令 UUID 全局唯一，父子活动状态仍由各自日志确定。 */ (command) => (
          <CommandRow
            key={command.id}
            runId={runId}
            command={command}
            readOnly={readOnly || !active}
          />
        ),
      )}
    </section>
  );
}
