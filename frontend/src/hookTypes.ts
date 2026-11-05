/** 脚本生命周期事件；运行完成事实仍使用既有 run_finished。 */
export type HookEvent =
  | 'RunStart'
  | 'PreToolUse'
  | 'PostToolUse'
  | 'PostToolUseFailure'
  | 'CommandFinished'
  | 'PreCompact'
  | 'PostCompact'
  | 'SubagentStart'
  | 'SubagentStop'
  | 'Stop';

/** 宿主/项目统一规则；页面只审核和启停，不提交任意脚本。 */
export interface HookRule {
  id: string; // 来源内唯一标识。
  event: HookEvent; // 触发节点。
  argv: string[]; // 精确命令参数。
  cwd: string; // 实际工作区内的相对目录。
  tools: string[]; // 工具名 glob；空表示全部。
  paths: string[]; // 相对文件路径 glob；空表示全部。
  agent_scopes: ('root' | 'develop')[]; // 默认主 Agent；开发子项需显式授权。
  timeout_seconds: number; // 单项秒数。
  on_failure: 'continue' | 'block'; // 脚本故障策略。
  required: boolean; // 必需门禁不能因未审核而跳过。
  enabled: boolean; // 配置中的开关。
}

/** 固定定义与当前审核状态；历史只读取运行时保存的值。 */
export interface HookDefinition {
  key: string; // source:id。
  rule: HookRule; // 审核的精确定义。
  workspace: string; // 实际工作区。
  cwd: string; // 已解析工作目录。
  scripts: Record<string, string>; // 显式脚本绝对路径到内容摘要。
  script_previews: Record<string, { text: string; truncated: boolean }>; // 有界源码，截断时需到本地文件审核剩余内容。
  previous_definition: unknown; // 上一次用户审核的定义；未审核时为空，用于核对变化。
  fingerprint: string; // 定义与入口身份指纹。
  trusted: boolean; // 当前内容是否已审核。
  enabled: boolean; // 配置与人工开关的交集。
}

/** 单次 Hook 的最新事实；原始事件保留开始、等待和结束的变化。 */
export interface HookRecord {
  id: string; // 稳定事件编号。
  key: string; // 所用规则。
  event: HookEvent; // 实际触发事件。
  fingerprint: string; // 当次定义指纹。
  status: string; // running/waiting/completed/failed/cancelled/skipped。
  seconds?: number; // 已结束时的活动秒数。
  reason?: string; // 跳过或故障原因。
  output?: { decision: string; reason: string; additional_context: string } | null; // 经过校验的脚本输出。
  result?: {
    stdout: string;
    stderr: string;
    exit_code: number | null;
    cleanup_status: string;
    stdout_ref: string;
    stderr_ref: string;
  }; // 实际进程回执及可追溯原始输出路径。
}

/** 当轮定义、实际调用与逻辑执行累计开销；无规则时允许空对象。 */
export interface HookSnapshot {
  definitions?: HookDefinition[]; // 实际加载快照，尚未准备时缺省。
  records?: HookRecord[]; // 当前运行段的调用。
  calls?: number; // 累计实际启动数。
  seconds?: number; // 累计活动秒数。
  stop_retries?: number; // 已使用的有限续跑次数。
}

/** 已登记项目的配置阅读视图。 */
export interface HookCatalog {
  items: HookDefinition[]; // 宿主先、项目后的生效顺序。
  config_path: string; // 用户维护 TOML 的项目路径。
}
