import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Segmented } from 'antd';
import { DownOutlined } from '@ant-design/icons';
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

/** 命令失败原因来自实际退出或宿主停止原因。 */
const commandErrors: Record<string, string> = {
  command_spawn_failed: '无法启动进程',
  command_timeout: '命令超过执行时限',
  command_output_limit: '达到输出捕获上限，之后的内容未保存',
  command_descendants_unconfirmed: '子进程清理未确认',
  command_nonzero_exit: '命令以非零状态退出',
};
const maxPreviewLength = 65536; // 最新输出最多保留的前端字符数，旧内容可从已保存日志回看。

/** 展示 runId 下 command 的实际状态与日志；selected 定位目标命令，readOnly 禁止停止。 */
function CommandRow({
  runId,
  command,
  readOnly,
  selected,
}: {
  runId: string; // 包含此命令的父运行。
  command: CommandExecution; // 命令归属、执行状态和已保存日志。
  readOnly: boolean; // 历史或归档命令只能读取。
  selected: boolean; // 从某个工具操作跳转时直接打开该命令。
}) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(selected); // 展开才请求日志；深链接的目标默认打开。
  const [stream, setStream] = useState<'stdout' | 'stderr'>('stdout'); // 双流使用独立字节偏移。
  const row = useRef<HTMLDetailsElement>(null); // 从执行过程跳转时只定位命令行。
  const active = ['starting', 'running', 'stopping'].includes(command.status);
  const issue = command.result?.error_code;
  useLayoutEffect(
    /* 同一输出页内换目标命令时展开对应项，滚动只影响输出面板。 */ () => {
      if (selected) {
        setOpen(true);
        row.current?.scrollIntoView?.({ block: 'nearest' });
      }
    },
    [selected],
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
      ref={row}
      open={open}
      onToggle={
        /* 用户打开或收起时同步日志挂载状态。 */ (event) => setOpen(event.currentTarget.open)
      }
    >
      <summary>
        <DownOutlined className="runtime-chevron" aria-hidden="true" />
        <code title={command.argv.join(' ')}>{command.argv.join(' ')}</code>
        <span className="runtime-command-status" data-status={command.status}>
          {commandLabels[command.status]}
        </span>
      </summary>
      {open && (
        <div className="runtime-command-body">
          <div className="runtime-command-meta">
            <code title={command.workspace}>
              {command.cwd === '.' ? command.workspace : `${command.workspace} / ${command.cwd}`}
            </code>
            {command.result?.exit_code != null && <span>退出码 {command.result.exit_code}</span>}
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
          {issue && commandErrors[issue] && (
            <p className="runtime-command-issue">{commandErrors[issue]}</p>
          )}
          {['unknown', 'incomplete'].includes(command.cleanup_status) && (
            <Alert type="warning" title="进程清理尚未确认，请核对本地进程和工作区。" />
          )}
          {stop.error && <Alert type="error" title={errorMessage(stop.error)} />}
          <div className="runtime-stream">
            <Segmented<'stdout' | 'stderr'>
              aria-label="命令输出流"
              size="small"
              value={stream}
              onChange={setStream}
              options={[
                { value: 'stdout', label: '标准输出' },
                { value: 'stderr', label: '标准错误' },
              ]}
            />
          </div>
          <CommandLog
            key={stream}
            runId={runId}
            command={command}
            stream={stream}
            active={active}
          />
          {command.result?.truncated && issue !== 'command_output_limit' && (
            <p className="muted">模型收到的输出预览已截断。此处读取已保存日志。</p>
          )}
        </div>
      )}
    </details>
  );
}

/** 按宿主字节游标读命令日志；默认跟随末尾，手动回看已保存页时冻结阅读位置。 */
function CommandLog({
  runId,
  command,
  stream,
  active,
}: {
  runId: string; // 根运行编号，用于校验日志访问范围。
  command: CommandExecution; // 提供命令编号与真实归属运行。
  stream: 'stdout' | 'stderr'; // 任一双流，切换时重新建立该流的阅读状态。
  active: boolean; // 命令仍活动时才轮询追加内容。
}) {
  const [tail, setTail] = useState({ offset: 0, content: '', clipped: false }); // 末尾的有界预览。
  const [pages, setPages] = useState<number[] | null>(null); // 非空表示正在按页回看，从 0 开始的字节偏移栈。
  const [follow, setFollow] = useState(true); // 日志内向上滚动时暂停跟随与轮询。
  const log = useRef<HTMLPreElement>(null); // 只滚动日志窗口，不跳动外层对话。
  const offset = pages?.at(-1) ?? tail.offset;
  const output = useQuery({
    queryKey: ['command-output', runId, command.id, stream, offset, active],
    queryFn: /* UTF-8 字符数和字节数不同，偏移只使用宿主回执。 */ () =>
      getCommandOutput(runId, command.id, command.run_id, stream, offset),
    refetchInterval: active && follow && !pages ? 800 : false,
    gcTime: 30_000,
    retry: false,
  });
  const chunk = output.data?.output;
  useEffect(
    /* 手动读页期间不推进实时游标；日志结束时也从当前偏移核对最后一页。 */ () => {
      if (pages || !follow || !chunk || chunk.next_offset <= chunk.offset) return;
      setTail((current) => {
        if (current.offset !== chunk.offset) return current;
        const content = current.content + chunk.content;
        return {
          offset: chunk.next_offset,
          content: content.slice(-maxPreviewLength),
          clipped: current.clipped || content.length > maxPreviewLength,
        };
      });
    },
    [chunk, follow, pages],
  );
  useLayoutEffect(
    /* 增量只跟随当前读者，主动选择的历史页从页首开始。 */ () => {
      if (log.current && (pages || follow)) {
        log.current.scrollTop = pages ? 0 : log.current.scrollHeight;
      }
    },
    [follow, pages, tail.content],
  );
  const content = pages ? chunk?.content : tail.content;
  return (
    <>
      {output.error && (
        <Alert
          type="error"
          title={errorMessage(output.error)}
          action={
            <Button size="small" onClick={/* 重读当前流与字节位置。 */ () => void output.refetch()}>
              重试
            </Button>
          }
        />
      )}
      <pre
        ref={log}
        aria-label="命令输出"
        tabIndex={0}
        onScroll={
          /* 用户滚回末尾后恢复跟随；历史页始终由翻页操作控制。 */ (event) => {
            if (pages) return;
            const node = event.currentTarget;
            setFollow(node.scrollHeight - node.scrollTop - node.clientHeight < 24);
          }
        }
      >
        {content || (output.isPending ? '正在读取…' : active ? '等待输出…' : '此流暂无输出')}
      </pre>
      {(pages || tail.clipped || !follow) && (
        <div className="runtime-log-actions">
          {pages ? (
            <>
              <Button
                type="text"
                size="small"
                disabled={pages.length === 1}
                onClick={/* 返回已知的上一页字节偏移。 */ () => setPages(pages.slice(0, -1))}
              >
                上一页
              </Button>
              <Button
                type="text"
                size="small"
                disabled={!chunk || chunk.eof || chunk.next_offset <= offset}
                onClick={
                  /* 采用宿主提供的下一页偏移。 */ () =>
                    chunk && setPages([...pages, chunk.next_offset])
                }
              >
                下一页
              </Button>
            </>
          ) : (
            tail.clipped && (
              <Button
                type="text"
                size="small"
                onClick={/* 保留末尾缓存，从首个已保存字节浏览。 */ () => setPages([0])}
              >
                从开头查看
              </Button>
            )
          )}
          {(pages || !follow) && (
            <Button
              type="text"
              size="small"
              onClick={
                /* 返回最新时继续从既有游标读增量。 */ () => {
                  setPages(null);
                  setFollow(true);
                }
              }
            >
              回到最新
            </Button>
          )}
        </div>
      )}
    </>
  );
}

/** 展示 runId 本段输入及父子命令；active 决定刷新，readOnly 禁止历史/归档管理，返回空视图时不占布局。 */
export function RuntimePanel({
  runId,
  active,
  readOnly = false,
  display = 'all',
  commandCallId,
  onOpenOutput,
}: {
  runId: string; // 当前查看的运行编号。
  active: boolean; // 活动轮需要刷新快照；历史只读取。
  readOnly?: boolean; // 归档及历史禁止命令停止和输入写入。
  display?: 'all' | 'conversation' | 'output'; // 作业详情兼顾两类证据；聊天和检查器分开展示。
  commandCallId?: string; // 仅当匹配当轮工具回执时打开对应命令。
  onOpenOutput?: () => void; // 从会话摘要打开本轮命令列表。
}) {
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ['runtime', runId],
    queryFn: /* 读取持久事实，不恢复旧任务。 */ () => getRuntime(runId),
    refetchInterval: active && display === 'all' ? 1000 : false,
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
    display === 'output'
      ? []
      : data?.state?.messages.filter((item) => item.status !== 'retracted') || [];
  const commands = data?.commands || [];
  if (display === 'output' && query.isPending) return <p className="muted">正在读取命令…</p>;
  if (display === 'output' && query.error && !data)
    return <Alert type="error" title="无法读取命令" description={errorMessage(query.error)} />;
  if (display === 'output' && !commands.length) return <p className="muted">本轮暂无命令输出</p>;
  if (display === 'all' && query.isPending) return <p className="muted">正在读取执行记录…</p>;
  if (display === 'all' && query.error && !data)
    return <Alert type="error" title="无法读取执行记录" description={errorMessage(query.error)} />;
  if (!messages.length && !commands.length && data?.state?.status !== 'needs_review')
    return display === 'all' ? <p className="muted">暂无命令或补充要求</p> : null;
  return (
    <section
      className="runtime-panel"
      aria-label={display === 'output' ? '执行输出' : '命令与运行中要求'}
    >
      {display !== 'output' && data?.state?.status === 'needs_review' && (
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
      {display === 'conversation' && commands.length > 0 && onOpenOutput && (
        <button type="button" className="runtime-output-link" onClick={onOpenOutput}>
          查看命令输出 <span>{commands.length}</span>
        </button>
      )}
      {display !== 'conversation' &&
        commands.map(
          /* 命令 UUID 全局唯一，父子活动状态仍由各自日志确定。 */ (command) => (
            <CommandRow
              key={command.id}
              runId={runId}
              command={command}
              readOnly={readOnly || !active}
              selected={Boolean(
                commandCallId && command.call_id === commandCallId && command.run_id === runId,
              )}
            />
          ),
        )}
    </section>
  );
}
