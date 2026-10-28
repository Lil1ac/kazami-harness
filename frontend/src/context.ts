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
