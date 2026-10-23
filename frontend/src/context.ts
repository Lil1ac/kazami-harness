import type { ContextPolicy } from './types';

/** 缺少上下文配置的旧宿主选项用于后续提交的默认值，不代表历史任务曾采用它。 */
export const DEFAULT_CONTEXT_POLICY: ContextPolicy = {
  load_project_instructions: true, // 默认读取实际工作区根目录的项目规则。
  max_input_bytes: 262144, // 整体模型输入上限 256 KiB。
  max_project_instruction_bytes: 32768, // 单份项目指令文件上限 32 KiB。
  compression: 'off', // 缺少新配置的旧宿主保持关闭。
  trigger_ratio: 0.8, // 接近预算时触发。
  target_ratio: 0.6, // 批量回收目标。
  inline_tool_bytes: 8192, // 外置阈值。
  keep_recent_groups: 3, // 近期完整批次。
  max_summary_calls: 2, // 额外摘要次数上限。
  max_summary_bytes: 8192, // 摘要正文上限。
  model_context_tokens: null, // 未声明窗口。
  token_safety_margin: 2048, // 额外token预留。
};

/** 已知指令加载状态文案；未知服务端状态由调用者按原始文本展示。 */
export const instructionStatusLabels: Record<string, string> = {
  loaded: '已加载',
  missing: '未找到',
  disabled: '已关闭加载',
  error: '加载失败',
};

/** 项目指令的受控错误说明，不包含宿主异常堆栈或不可信路径。 */
const instructionErrors: Record<string, string> = {
  project_instructions_too_large: '项目指令文件超过本次大小上限，未截断加载。',
  project_instructions_invalid_utf8: '项目指令不是有效的 UTF-8 文本。',
  project_instructions_unreadable: '无法读取项目指令文件，请检查文件权限。',
  project_instructions_outside_workspace: '项目指令路径指向工作区外部。',
  project_instructions_run_records_forbidden: '项目指令路径指向受保护的运行记录。',
  project_instructions_not_regular_file: '项目指令路径不是普通文件。',
  project_instructions_changed: '读取期间项目指令文件发生变化，请核对后重新提交。',
  project_instructions_invalid_path: '项目指令路径无效。',
};

/** 将已知 UTF-8 字节数转换为 KiB，最多保留三位小数；不解释为 token 数。 */
export function formatContextBytes(bytes: number): string {
  return `${(bytes / 1024).toLocaleString('zh-CN', { maximumFractionDigits: 3 })} KiB`;
}

/** 根据稳定原因码返回指令读取说明；未知码保持可见以便排查，不推断原因。 */
export function instructionErrorLabel(code: string): string {
  return instructionErrors[code] ?? `项目指令加载失败（${code}）。`;
}
