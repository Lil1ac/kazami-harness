import type { RunEvent } from './types';

/** 一项由实际事件投影的操作；缺少配对记录时保持未知，不推断成功。 */
export interface ActivityStep {
  id: string; // 工具调用编号或事件唯一序号。
  label: string; // 面向使用者的操作名称。
  state: 'running' | 'passed' | 'failed' | 'unknown'; // 仅由已保存结果确定。
  arguments: unknown; // 工具原始参数，展示时按纯文本转义。
  result: unknown; // 实际结果；尚未返回时为空。
}
const toolLabels: Record<string, string> = {
  read_file: '读取文件',
  search_files: '搜索内容',
  write_file: '写入文件',
  run_command: '执行命令',
  read_context: '回读历史证据',
}; // 文件、命令及只读历史工具的操作语义。

/** 安全解析工具参数 JSON；损坏输入保留原字符串，便于在完整轨迹中核对。 */
function argumentsValue(value: unknown): unknown {
  if (typeof value !== 'string') return value ?? null;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

/** 从有界events投影工具/验收；active默认false，缺少结果只在确认活动时显示执行中。 */
export function activitySteps(events: RunEvent[], active = false): ActivityStep[] {
  const steps: ActivityStep[] = [];
  const tools = new Map<string, ActivityStep>();
  let verification: ActivityStep | undefined; // 当前窗口中的验收开始，不能替代实际验收结果。
  for (const event of events) {
    const payload = event.payload;
    if (event.type === 'tool_started') {
      const id = typeof payload.call_id === 'string' ? payload.call_id : `event-${event.seq}`;
      const args = argumentsValue(payload.arguments_json);
      const path =
        args && typeof args === 'object' && 'path' in args && typeof args.path === 'string'
          ? args.path
          : '';
      const name = String(payload.name || '工具调用');
      const step: ActivityStep = {
        id,
        label: `${toolLabels[name] || name}${path ? ` · ${path}` : ''}`,
        state: 'running',
        arguments: args,
        result: null,
      };
      tools.set(id, step);
      steps.push(step);
    } else if (event.type === 'tool_result') {
      const id = String(payload.call_id || `result-${event.seq}`);
      let step = tools.get(id);
      if (!step) {
        step = {
          id,
          label: toolLabels[String(payload.name)] || String(payload.name || '工具结果'),
          state: 'unknown',
          arguments: null,
          result: null,
        };
        steps.push(step);
      }
      const result = payload.result as { ok?: unknown } | null;
      step.result = payload.result;
      step.state = result?.ok === true ? 'passed' : result?.ok === false ? 'failed' : 'unknown';
    } else if (event.type === 'verification_started') {
      verification = {
        id: `verification-${event.seq}`,
        label: '宿主独立验收',
        state: 'running',
        arguments: null,
        result: null,
      };
      steps.push(verification);
    } else if (event.type === 'verification_result') {
      verification ||= {
        id: `verification-${event.seq}`,
        label: '宿主独立验收',
        state: 'unknown',
        arguments: null,
        result: null,
      };
      if (!steps.includes(verification)) steps.push(verification);
      const result = payload.verification as { status?: string } | null;
      verification.result = result;
      verification.state =
        result?.status === 'passed'
          ? 'passed'
          : result?.status === 'failed' || result?.status === 'error'
            ? 'failed'
            : 'unknown';
    }
  }
  // 终态以后仍缺少工具结果时不能一直显示正在执行，保留为未确认事实。
  if (
    !active ||
    events.some(/* 仅可信终态事件表明调度已经结束。 */ (event) => event.type === 'run_finished')
  )
    for (const step of steps) if (step.state === 'running') step.state = 'unknown';
  return steps;
}
