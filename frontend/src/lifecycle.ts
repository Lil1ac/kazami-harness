import type { RunEvent } from './types';

/** 对应宿主触发位置，显示时翻译成可读阶段。 */
export const lifecycleStages = {
  start: '启动与接续',
  input: '准备模型输入',
  response: '确认模型响应',
  before_tool: '工具执行前',
  after_tool: '工具返回后',
  batch_end: '工具批次结束',
  candidate: '检查候选答复',
  before_finish: '交付前检查',
  verify: '独立验收与交接',
  after_finish: '验收后确认',
} as const;

/** 组件此次实际返回的调度决定或中断情况。 */
export const lifecycleOutcomes = {
  proceed: '继续',
  replan: '重新决策',
  wait: '等待',
  finish: '进入交付',
  abort: '结束调度',
  interrupted: '中断',
} as const;

/** 一次已保存的阶段调用，包含真实进入的组件和来源。 */
export interface LifecycleStageTrace {
  stage: keyof typeof lifecycleStages; // 宿主调度位置。
  seq: number; // 运行内连续序号，同阶段比较新旧时使用。
  components: {
    name: string; // 组件稳定标识。
    source: string; // 当前执行的宿主模块。
    outcome: keyof typeof lifecycleOutcomes; // 此次组件结果。
    reason?: string | null; // 有稳定停止或等待原因时返回。
  }[];
}

/** 从事件中提取可展示的阶段；其他事件或无效数据返回 null。 */
export function lifecycleTrace(event: RunEvent): LifecycleStageTrace | null {
  if (event.type !== 'pipeline_stage') return null;
  const { stage, components } = event.payload;
  if (
    typeof stage !== 'string' ||
    !Object.hasOwn(lifecycleStages, stage) ||
    !Array.isArray(components)
  )
    return null;
  const valid = components.filter(
    /* 只展示具有已知结果与真实来源的组件，忽略不完整事件项。 */ (item) =>
      item &&
      typeof item.name === 'string' &&
      typeof item.source === 'string' &&
      typeof item.outcome === 'string' &&
      Object.hasOwn(lifecycleOutcomes, item.outcome) &&
      (item.reason == null || typeof item.reason === 'string'),
  );
  return valid.length
    ? { stage: stage as LifecycleStageTrace['stage'], seq: event.seq, components: valid }
    : null;
}

/** 合并快照与实时轨迹；按 seq 去重，每阶段只保留最近一次，空间不随工具数增长。 */
export function mergeLifecycle(
  previous: LifecycleStageTrace[],
  incoming: LifecycleStageTrace[],
): LifecycleStageTrace[] {
  if (!incoming.length) return previous;
  const latest = new Map(previous.map(/* 使用阶段作为唯一键。 */ (entry) => [entry.stage, entry]));
  for (const entry of incoming) {
    if (entry.seq > (latest.get(entry.stage)?.seq ?? 0)) latest.set(entry.stage, entry);
  }
  return [...latest.values()].sort(/* 按实际事件时序阅读。 */ (a, b) => a.seq - b.seq);
}
