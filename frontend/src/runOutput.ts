import type { CommandExecution } from './runtimeTypes';
import type { RunResult, RunView, VerificationAttempt, VerificationResult } from './types';

/** 输出深链接。call 对应过程中的根运行工具回执，command 对应实际命令，check 对应检查次数。 */
export type OutputTarget = `call:${string}` | `command:${string}` | `check:${number | 'latest'}`;

/** 已保存的两种输出流，名称与宿主接口一致。 */
export type OutputStream = 'stdout' | 'stderr';

/** 一条可定位的失败记录，摘要只从此次实际选中的日志生成。 */
export interface FailureEvidence {
  runId: string; // 归属轮次，追加草稿前与最新运行核对。
  title: string; // 实际失败的命令或检查。
  reason: string; // 根据宿主状态和原因码解释失败。
  exitCode: number | null; // 未取得进程退出码时为空。
  stream: OutputStream; // 摘录所属的实际输出流。
  content: string; // 当前已读取的原始日志预览，可以为空。
}

export const outputStreamLabels = { stdout: '标准输出', stderr: '错误输出' } as const; // 供日志切换和续修草稿引用。
export const commandLabels: Record<CommandExecution['status'], string> = {
  starting: '启动中',
  running: '运行中',
  stopping: '停止中',
  completed: '执行完成',
  failed: '命令失败',
  cancelled: '已停止',
  needs_review: '待核对',
}; // 命令状态属于单次工具调用。

const commandReasons: Record<string, string> = {
  command_spawn_failed: '无法启动进程',
  command_timeout: '命令超过执行时限',
  command_output_limit: '达到日志捕获上限',
  command_descendants_unconfirmed: '子进程清理未确认',
  command_nonzero_exit: '命令以非零状态退出',
}; // 从宿主原因码映射简明提示，未识别原因保留通用结论。

const checkReasons: Record<string, string> = {
  assertion_failed: '检查发现结果不符合要求',
  verifier_exit_error: '检查进程异常退出',
  verifier_timeout: '检查超过执行时限',
  verifier_start_failed: '无法启动检查进程',
  verifier_cleanup_failed: '检查进程清理未确认',
  verifier_cancelled: '检查已取消',
  verifier_output_limit: '检查达到日志捕获上限',
}; // 检查未通过与检查进程异常使用不同说明。

/** 读取 URL 中的输出目标，格式无效时让面板使用当轮默认记录。 */
export function parseOutputTarget(value: string | null): OutputTarget | undefined {
  if (!value || !/^(?:(?:call|command):.+|check:(?:latest|[0-9]+))$/.test(value)) return;
  return value as OutputTarget;
}

/** 判断此轮是否有公开项目测试，原目录项目也使用测试文案。 */
export function usesProjectChecks(run: RunView): boolean {
  return (
    run.verification_kind === 'project_tests' ||
    Boolean(run.project) ||
    run.source_kind === 'project'
  );
}

/** 读取固定检查序列；只有单条当前检查的快照可按实际修复次数直接定位。 */
export function checkAttempts(result: RunResult): VerificationAttempt[] {
  const { verification, verification_attempts, repair_attempts } = result;
  return verification && !verification_attempts.length
    ? [{ repair_attempt: repair_attempts, result: verification }]
    : verification_attempts;
}

/** 将具体进程结果转成易读状态，项目检查沿用项目测试术语。 */
export function checkLabel(status: VerificationResult['status'], project: boolean): string {
  if (status === 'passed') return project ? '测试通过' : '验收通过';
  if (status === 'failed') return project ? '测试未通过' : '验收未通过';
  return project ? '测试运行异常' : '验收运行异常';
}

/** 返回当前检查的简明失败原因；通过时没有需要说明的错误。 */
export function checkReason(detail: VerificationResult): string {
  if (detail.status === 'passed') return '';
  return (
    checkReasons[detail.reason] ||
    (detail.status === 'failed' ? '检查发现结果不符合要求' : '检查未能正常结束')
  );
}

/** 识别真实命令失败及缺少可靠终态的记录；手动停止有独立状态。 */
export function hasCommandError(command: CommandExecution): boolean {
  return command.status === 'failed' || command.status === 'needs_review';
}

/** 返回最近一条失败命令，保持宿主的原始执行顺序。 */
export function lastFailedCommand(commands: CommandExecution[]): CommandExecution | undefined {
  return commands.slice().reverse().find(hasCommandError);
}

/** 解释宿主记录的命令故障，未失败的命令没有错误说明。 */
export function commandReason(command: CommandExecution): string {
  const code = command.result?.error_code;
  return (
    (code && commandReasons[code]) ||
    (hasCommandError(command) ? commandLabels[command.status] : '')
  );
}

/** 返回可编辑的续修草稿，保留原文字并避免重复附加同一段证据。 */
export function failureDraft(existing: string, evidence: FailureEvidence): string {
  const title = evidence.title.length > 240 ? evidence.title.slice(0, 240) + '…' : evidence.title;
  const text = evidence.content.trimEnd();
  const excerpt = text.length > 1600 ? '…\n' + text.slice(-1600) : text;
  const block = [
    '【失败证据】',
    title,
    '原因 ' + evidence.reason,
    '退出码 ' + (evidence.exitCode ?? '未获得'),
    ...(excerpt ? [outputStreamLabels[evidence.stream] + '摘录', excerpt] : []),
  ].join('\n');
  if (existing.includes(block)) return existing;
  return (existing || '请根据下面的失败记录继续修复，完成后重新检查。') + '\n\n' + block;
}
