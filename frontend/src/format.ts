import type { ProjectReview } from './types';

/** 格式化毫秒为可读耗时；负值按零处理，不返回倒计时推断。 */
export function duration(milliseconds: number): string {
  const seconds = Math.max(0, milliseconds) / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
}

/** 将带时区时间转换为浏览器本地时间；非法值显示占位。 */
export function dateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
}

/** 将任意 JSON 数据变成安全纯文本；由 React 转义，不使用 innerHTML。 */
export function pretty(value: unknown): string {
  return typeof value === 'string' ? value : (JSON.stringify(value, null, 2) ?? '');
}

/** 将宿主阶段码翻译成任务操作语言；未知阶段保留原始码便于定位。 */
export function phaseLabel(phase: string): string {
  return (
    (
      {
        created: '等待执行',
        starting: '开始执行',
        model: '等待模型',
        model_request: '等待模型',
        context_compaction: '整理历史上下文',
        model_response: '处理模型响应',
        tool: '执行工具',
        tool_execution: '执行工具',
        tool_result: '处理工具结果',
        execution_completed: '执行完成',
        verification: '独立验收',
        project_snapshot: '整理代码改动',
        workspace_snapshot: '保存工作区快照',
        verification_result: '保存验收结果',
        finished: '已结束',
        running: '正在执行',
      } as Record<string, string>
    )[phase] ?? phase
  );
}

/** 人工审阅状态文案；独立于运行是否完成和测试是否通过。 */
export const reviewLabels: Record<ProjectReview['status'], string> = {
  pending: '等待审阅',
  kept: '已保留工作树',
  discarded: '已丢弃工作树',
};
