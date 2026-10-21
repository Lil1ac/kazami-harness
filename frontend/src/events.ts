import type { RunEvent } from './types';

/** 按 run_id 和 seq 合并事件并只保留末尾 limit 条；乱序、重复和旧任务事件不会污染轨迹。 */
export function mergeEvents(
  previous: RunEvent[],
  incoming: RunEvent[],
  runId: string,
  limit = 400,
): RunEvent[] {
  const bySeq = new Map<number, RunEvent>();
  for (const event of [...previous, ...incoming]) {
    if (event.run_id === runId && Number.isInteger(event.seq) && event.seq > 0)
      bySeq.set(event.seq, event);
  }
  return [...bySeq.values()]
    .sort(/* 按持久化序号而非到达时间排序。 */ (left, right) => left.seq - right.seq)
    .slice(-limit);
}

/** 只有当前连续游标后的首条事件需要补齐；重复序号不触发补请求。 */
export function hasEventGap(cursor: number, event: RunEvent): boolean {
  return event.seq > cursor + 1;
}

/** 从不可信 SSE 正文解析完整事件；非法、错任务及非正整数序号返回 null。 */
export function parseRunEvent(raw: string, runId: string): RunEvent | null {
  try {
    const event = JSON.parse(raw) as Partial<RunEvent>;
    if (
      event.run_id !== runId ||
      !Number.isInteger(event.seq) ||
      (event.seq ?? 0) < 1 ||
      typeof event.type !== 'string' ||
      typeof event.elapsed_ms !== 'number' ||
      typeof event.time_utc !== 'string' ||
      !event.payload ||
      typeof event.payload !== 'object'
    )
      return null;
    return event as RunEvent;
  } catch {
    return null;
  }
}
