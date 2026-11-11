import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Segmented } from 'antd';
import { CheckCircleOutlined, CodeOutlined, ExperimentOutlined } from '@ant-design/icons';
import { artifactUrl, errorMessage, getCommandOutput, getRuntime, stopCommand } from '../api';
import { duration } from '../format';
import {
  checkAttempts,
  checkLabel,
  checkReason,
  commandLabels,
  commandReason,
  hasCommandError,
  outputStreamLabels,
  usesProjectChecks,
} from '../runOutput';
import type { FailureEvidence, OutputStream, OutputTarget } from '../runOutput';
import type { CommandExecution } from '../runtimeTypes';
import type { RunView, VerificationAttempt } from '../types';
import './RunOutputPanel.css';

/** 写入当前会话草稿，若当前无法写入则返回需要展示的原因。 */
export type ContinueFromError = (evidence: FailureEvidence) => string | undefined;

/** id 是可写入 URL 的记录编号；command 与 check 互斥，各自保留完整宿主证据。 */
type OutputRecord =
  | { id: OutputTarget; command: CommandExecution; check?: never }
  | { id: OutputTarget; check: VerificationAttempt; command?: never };

const streamOptions = [
  { value: 'stdout' as const, label: outputStreamLabels.stdout },
  { value: 'stderr' as const, label: outputStreamLabels.stderr },
]; // 双流在同一位置切换，文字同时用作日志和草稿的流名称。
const maxPreviewLength = 65_536; // 末尾预览的字符上限，早期内容仍可按字节回看。

/** 区分首次和自动修复后的检查；编号来自真实检查快照。 */
function checkName(project: boolean, repairAttempt: number): string {
  const name = project ? '项目测试' : '独立验收';
  return repairAttempt ? '第 ' + repairAttempt + ' 次修复后' + name : name;
}

/** 只提供追加草稿的操作；写入失败时保留读者的位置和日志。 */
function ContinueButton({
  action,
  evidence,
  loading = false,
}: {
  action?: ContinueFromError; // 缺省表示当前记录只可阅读。
  evidence: FailureEvidence; // 当前已读失败记录，包含实际双流之一的摘录。
  loading?: boolean; // 正在读取所选日志，暂缓追加不完整的证据。
}) {
  const [error, setError] = useState<string>(); // 草稿过长或当前运行刚进入不可续轮状态的反馈。
  if (!action) return null;
  return (
    <div className="run-output-continue">
      <Button
        type="primary"
        disabled={loading}
        onClick={/* 只改变草稿，下一条消息仍需用户发送。 */ () => setError(action(evidence))}
      >
        带错误继续
      </Button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

/** 展示一次固定检查的双流、退出码和对应原始输出下载。 */
function CheckOutput({
  runId,
  check,
  project,
  onContinue,
}: {
  runId: string; // 已保存检查与附件的归属运行。
  check: VerificationAttempt; // 已选的真实检查快照。
  project: boolean; // 为公开项目测试时使用项目术语。
  onContinue?: ContinueFromError; // 只由最新轮的当前失败检查提供。
}) {
  const detail = check.result;
  const [stream, setStream] = useState<OutputStream>(
    detail.status !== 'passed' && detail.stderr ? 'stderr' : 'stdout',
  );
  const reason = checkReason(detail);
  const title = checkName(project, check.repair_attempt);
  const codeChanged =
    detail.code_before && detail.code_after && detail.code_before !== detail.code_after;
  return (
    <>
      <div className="run-output-meta">
        <span>{reason || checkLabel(detail.status, project)}</span>
        <span>退出码 {detail.exit_code ?? '未获得'}</span>
        <span>{duration(detail.elapsed_ms)}</span>
      </div>
      {codeChanged && (
        <Alert type="warning" title="检查期间源码发生变化，需要对结束代码重新检查。" />
      )}
      {detail.truncated && (
        <p className="run-output-note">
          {detail.output_limit_exceeded ? '已达到宿主日志捕获上限。' : '日志预览已截断。'}
          {detail.artifacts.length > 0 && '可下载已保存的输出。'}
        </p>
      )}
      <div className="run-output-toolbar">
        <Segmented<OutputStream>
          aria-label="输出流"
          size="small"
          value={stream}
          onChange={setStream}
          options={streamOptions}
        />
        {detail.artifacts.map(
          /* 保留检查自己的路径，后续修复不会改变旧检查的下载目标。 */ (artifact) => (
            <a key={artifact.path} href={artifactUrl(runId, artifact)} download>
              {artifact.kind === 'stderr' ? '下载错误输出' : '下载标准输出'}
            </a>
          ),
        )}
      </div>
      <pre
        className="run-output-log"
        key={stream}
        aria-label={outputStreamLabels[stream]}
        tabIndex={0}
      >
        {detail[stream] || '此流暂无输出'}
      </pre>
      <ContinueButton
        action={onContinue}
        evidence={{
          runId,
          title,
          reason,
          exitCode: detail.exit_code,
          stream,
          content: detail[stream],
        }}
      />
    </>
  );
}

/** 按真实命令归属读取日志，活动命令仍支持停止。 */
function CommandOutput({
  runId,
  command,
  canStop,
  onContinue,
}: {
  runId: string; // 日志接口校验范围的根运行。
  command: CommandExecution; // 包含实际命令与所有者编号，可能是子执行。
  canStop: boolean; // 仅当可管理的最新轮仍活动时允许停止。
  onContinue?: ContinueFromError; // 终态命令失败时允许追加可编辑证据。
}) {
  const cache = useQueryClient();
  const [chosenStream, setChosenStream] = useState<OutputStream>(); // 手动选择优先于自动匹配错误流。
  const [emptyStderr, setEmptyStderr] = useState(false); // 结束后确认错误流为空，默认阅读实际标准输出。
  const stream = chosenStream ?? (hasCommandError(command) && !emptyStderr ? 'stderr' : 'stdout');
  const active = ['starting', 'running', 'stopping'].includes(command.status);
  const reason = commandReason(command);
  const workingDirectory =
    command.cwd === '.' ? command.workspace : command.workspace + ' / ' + command.cwd;
  const stop = useMutation({
    mutationFn: /* 使用真实命令编号及其归属，宿主确认清理。 */ () =>
      stopCommand(runId, command.id, command.run_id),
    onSuccess: /* 从宿主快照重新获取状态和退出事实。 */ () => {
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
    },
  });
  return (
    <>
      <div className="run-output-meta">
        <code title={workingDirectory}>{workingDirectory}</code>
        {command.result && <span>退出码 {command.result.exit_code ?? '未获得'}</span>}
        {canStop && active && (
          <Button
            size="small"
            loading={stop.isPending}
            disabled={command.status === 'stopping'}
            onClick={/* 等待宿主停止该命令。 */ () => stop.mutate()}
          >
            停止命令
          </Button>
        )}
      </div>
      {reason && <p className="run-output-note">{reason}</p>}
      {['unknown', 'incomplete'].includes(command.cleanup_status) && (
        <Alert type="warning" title="进程清理尚未确认，请核对本地进程和工作区。" />
      )}
      {stop.error && <Alert type="error" title={errorMessage(stop.error)} />}
      {command.result?.truncated && command.result.error_code !== 'command_output_limit' && (
        <p className="run-output-note">模型预览已截断，此处可查看已保存日志。</p>
      )}
      <div className="run-output-toolbar">
        <Segmented<OutputStream>
          aria-label="输出流"
          size="small"
          value={stream}
          onChange={setChosenStream}
          options={streamOptions}
        />
      </div>
      <CommandLog
        key={stream}
        runId={runId}
        command={command}
        stream={stream}
        active={active}
        onEmptyStderr={chosenStream ? undefined : setEmptyStderr}
        onContinue={onContinue}
      />
    </>
  );
}

/** 跟随宿主字节游标读取选中流；回看按页浏览，滚动和缓存只属于当前流。 */
function CommandLog({
  runId,
  command,
  stream,
  active,
  onEmptyStderr,
  onContinue,
}: {
  runId: string; // 根运行编号，用于校验日志读取范围。
  command: CommandExecution; // 提供命令编号、归属和实际失败状态。
  stream: OutputStream; // 当前双流之一，切换时重建阅读位置。
  active: boolean; // 活动命令才轮询追加内容。
  onEmptyStderr?: (empty: boolean) => void; // 初次阅读错误流时允许自动回落。
  onContinue?: ContinueFromError; // 允许把当前实际读到的日志追加到草稿。
}) {
  const [tail, setTail] = useState({ offset: 0, content: '', clipped: false }); // 末尾的有界预览。
  const [pages, setPages] = useState<number[] | null>(null); // 回看页的字节偏移栈，null 表示读末尾。
  const [follow, setFollow] = useState(true); // 上翻时暂停，滚回末尾后恢复。
  const log = useRef<HTMLPreElement>(null); // 只滚动日志内部，不跳动会话或详情。
  const offset = pages?.at(-1) ?? tail.offset;
  const output = useQuery({
    queryKey: ['command-output', runId, command.id, stream, offset, active],
    queryFn: /* 宿主按原始字节定位，不能按展示字符长度计算。 */ () =>
      getCommandOutput(runId, command.id, command.run_id, stream, offset),
    refetchInterval: active && follow && !pages ? 800 : false,
    gcTime: 30_000,
    retry: false,
  });
  const chunk = output.data?.output;
  useEffect(
    /* 已结束且确认没有错误字节时，默认展示有机会包含诊断的标准输出。 */ () => {
      if (!active && stream === 'stderr' && chunk?.eof && chunk.size_bytes === 0)
        onEmptyStderr?.(true);
    },
    [active, stream, chunk, onEmptyStderr],
  );
  useEffect(
    /* 回看期间保留末尾游标，从既有位置回到最新。 */ () => {
      if (pages || !follow || !chunk || chunk.next_offset <= chunk.offset) return;
      setTail(
        /* 只合并相邻字节页，避免缓存重读时重复拼接。 */ (current) => {
          if (current.offset !== chunk.offset) return current;
          const content = current.content + chunk.content;
          return {
            offset: chunk.next_offset,
            content: content.slice(-maxPreviewLength),
            clipped: current.clipped || content.length > maxPreviewLength,
          };
        },
      );
    },
    [chunk, follow, pages],
  );
  useLayoutEffect(
    /* 用户显式翻页时从页首读，跟随模式始终保持在日志末尾。 */ () => {
      if (log.current && (pages || follow)) {
        log.current.scrollTop = pages ? 0 : log.current.scrollHeight;
      }
    },
    [follow, pages, tail.content],
  );
  const content = pages ? chunk?.content || '' : tail.content;
  return (
    <>
      {output.error && (
        <Alert
          type="error"
          title={errorMessage(output.error)}
          action={
            <Button size="small" onClick={/* 重读当前流及所在位置。 */ () => void output.refetch()}>
              重试
            </Button>
          }
        />
      )}
      <pre
        className="run-output-log"
        ref={log}
        aria-label={outputStreamLabels[stream]}
        tabIndex={0}
        onScroll={
          /* 滚回末尾后恢复跟随，历史页由翻页按钮单独控制。 */ (event) => {
            if (pages) return;
            const node = event.currentTarget;
            setFollow(node.scrollHeight - node.scrollTop - node.clientHeight < 24);
          }
        }
      >
        {content || (output.isPending ? '正在读取…' : active ? '等待输出…' : '此流暂无输出')}
      </pre>
      {(pages || tail.clipped || !follow) && (
        <div className="run-output-toolbar">
          {pages ? (
            <>
              <Button
                type="text"
                size="small"
                disabled={pages.length === 1}
                onClick={/* 返回上一个实际字节位置。 */ () => setPages(pages.slice(0, -1))}
              >
                上一页
              </Button>
              <Button
                type="text"
                size="small"
                disabled={!chunk || chunk.eof || chunk.next_offset <= offset}
                onClick={
                  /* 使用宿主返回的下一页位置。 */ () =>
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
                onClick={/* 从首个已保存字节回看。 */ () => setPages([0])}
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
                /* 回到末尾并继续跟随增量。 */ () => {
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
      <ContinueButton
        action={onContinue}
        loading={
          output.isPending || Boolean(!pages && follow && chunk && !chunk.eof && !output.error)
        }
        evidence={{
          runId,
          title: '命令 ' + command.argv.join(' '),
          reason: commandReason(command),
          exitCode: command.result?.exit_code ?? null,
          stream,
          content,
        }}
      />
    </>
  );
}

/** 按轮次展示命令和实际检查；URL 明确指定的记录未找到时保留定位反馈。 */
export function RunOutputPanel({
  run,
  active,
  readOnly,
  target,
  onSelect,
  onContinue,
}: {
  run: RunView; // 所选轮次，包含固定的当前检查及历史尝试。
  active: boolean; // 活动轮需要轮询命令状态。
  readOnly: boolean; // 历史或归档禁用停止及继续操作。
  target?: OutputTarget; // 指定命令 UUID、工具回执或检查尝试。
  onSelect: (target: OutputTarget) => void; // 保存输出定位，刷新仍能找到同一项。
  onContinue?: ContinueFromError; // 最新可继续轮的可编辑草稿入口。
}) {
  const runtime = useQuery({
    queryKey: ['runtime', run.run_id],
    queryFn: /* 仅查询已记录的父子命令。 */ () => getRuntime(run.run_id),
    refetchInterval: active ? 1500 : false,
    retry: false,
  });
  const { verification } = run.result;
  const checks = checkAttempts(run.result);
  const records: OutputRecord[] = [
    ...(runtime.data?.commands || []).map(
      /* 每个命令使用实际执行编号，子 Agent 调用也各自可定位。 */ (command) => ({
        id: ('command:' + command.id) as OutputTarget,
        command,
      }),
    ),
    ...checks.map(
      /* 检查以本轮实际修复次数标识，后续通过不会覆盖此前失败。 */ (check) => ({
        id: ('check:' + check.repair_attempt) as OutputTarget,
        check,
      }),
    ),
  ];
  const latestCheck = checks.at(-1);
  const project = usesProjectChecks(run);
  const selected =
    target === 'check:latest'
      ? records.find(
          /* 最新检查属于当前所选轮。 */ (record) =>
            record.check === latestCheck && Boolean(record.check),
        )
      : target?.startsWith('call:')
        ? records.find(
            /* 工具过程来自根轮，避免使用子运行的同名回执。 */ ({ command }) =>
              command?.run_id === run.run_id && command.call_id === target.slice(5),
          )
        : target
          ? records.find(/* URL 对应一条实际记录。 */ ({ id }) => id === target)
          : (records
              .slice()
              .reverse()
              .find(
                /* 默认先定位实际失败的命令或检查。 */ ({ command, check }) =>
                  command ? hasCommandError(command) : check?.result.status !== 'passed',
              ) ?? records.at(-1));
  const selectedButton = useRef<HTMLButtonElement>(null); // 长列表只滚动到当前记录。
  useLayoutEffect(
    /* 深链接打开后在输出列表中露出目标项，不改变焦点。 */ () => {
      selectedButton.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    },
    [selected?.id],
  );
  const commandTarget = !target || !target.startsWith('check:');
  const canContinue = !readOnly && !active && onContinue;
  return (
    <section className="run-output" aria-label="本轮输出">
      {records.length > 0 && (
        <div className="run-output-records" role="group" aria-label="输出记录">
          {records.map(
            /* 同一列表中显示两类真实记录，选中后只渲染当前日志。 */ ({ id, command, check }) => {
              const title = command
                ? command.argv.join(' ')
                : checkName(project, check.repair_attempt);
              const label = command
                ? commandLabels[command.status]
                : checkLabel(check.result.status, project);
              const failed = command ? hasCommandError(command) : check.result.status !== 'passed';
              const current = selected?.id === id;
              return (
                <button
                  type="button"
                  key={id}
                  ref={current ? selectedButton : undefined}
                  aria-pressed={current}
                  onClick={/* 只更新日志定位。 */ () => onSelect(id)}
                  title={title}
                >
                  {command ? (
                    <CodeOutlined />
                  ) : failed ? (
                    <ExperimentOutlined />
                  ) : (
                    <CheckCircleOutlined />
                  )}
                  <span>{title}</span>
                  <span className="run-output-status" data-failed={failed}>
                    {label}
                  </span>
                </button>
              );
            },
          )}
        </div>
      )}
      {runtime.error && (
        <Alert type="error" title="无法读取命令" description={errorMessage(runtime.error)} />
      )}
      {selected ? (
        <div className="run-output-detail" key={selected.id}>
          {selected.command ? (
            <CommandOutput
              runId={run.run_id}
              command={selected.command}
              canStop={!readOnly && active}
              onContinue={
                canContinue && hasCommandError(selected.command) ? canContinue : undefined
              }
            />
          ) : (
            <CheckOutput
              runId={run.run_id}
              check={selected.check}
              project={project}
              onContinue={
                canContinue &&
                verification &&
                selected.check === latestCheck &&
                verification.status !== 'passed'
                  ? canContinue
                  : undefined
              }
            />
          )}
        </div>
      ) : commandTarget && runtime.isPending ? (
        <p className="muted" role="status">
          正在读取输出记录…
        </p>
      ) : target ? (
        <p className="muted">本轮没有找到这条输出记录。</p>
      ) : (
        !runtime.error && <p className="muted">本轮暂无命令或检查输出。</p>
      )}
    </section>
  );
}
