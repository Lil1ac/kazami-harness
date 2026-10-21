/** Python 数据模型的浏览器表示；字段名与后端持久化及 HTTP 契约保持一致。 */
export type ToolName = 'read_file' | 'search_files' | 'write_file' | 'run_command';
export type ExecutionStatus =
  'created' | 'running' | 'completed' | 'failed' | 'cancelled' | 'timed_out' | 'limit_exceeded';
export type ValidationStatus = 'unverified' | 'passed' | 'failed' | 'error';
export type CleanupStatus = 'not_needed' | 'complete' | 'incomplete' | 'unknown';

/** 单次任务预算；时间以秒计，字节和字符预算分别命名，禁止混用。 */
export interface RunLimits {
  max_model_turns: number; // 允许的逻辑模型请求轮数。
  max_tool_calls: number; // 整次任务接纳的工具调用上限。
  model_max_output_tokens: number; // 单轮模型输出 token 上限。
  max_http_attempts_per_turn: number; // 一轮包含重试的最大 HTTP 尝试数。
  model_turn_timeout_seconds: number; // 单轮模型及重试的总超时。
  command_timeout_seconds: number; // 单个工具命令超时。
  verifier_timeout_seconds: number; // 验收器默认超时。
  run_timeout_seconds: number; // 覆盖执行与验收的总运行超时。
  cleanup_grace_seconds: number; // 停止子进程时的清理宽限。
  tool_output_preview_chars: number; // 输出预览字符上限。
  max_process_capture_bytes: number; // 标准输出和错误输出合计字节上限。
  max_file_write_bytes: number; // 单次文件写入字节上限。
}

/** 可序列化运行设置；api_key_env 只保存变量名，前端从不接收密钥值。 */
export interface RunConfig {
  provider: 'deepseek'; // 当前宿主配置的供应商。
  requested_model: string; // 本次请求使用的模型名称。
  api_key_env: 'DEEPSEEK_API_KEY'; // 仅展示宿主环境变量来源。
  tools: ToolName[]; // 本次任务允许使用的工具集合。
  system_prompt: string; // 服务端系统提示词，只读展示。
  limits: RunLimits; // 每个任务独立的预算快照。
}

/** 已登记产物；path 相对于 location 对应目录，由后端再次检查安全边界。 */
export interface ArtifactRef {
  location: 'workspace' | 'run'; // 工作区文件或运行内输出。
  path: string; // 安全的相对路径。
  kind: 'file' | 'stdout' | 'stderr'; // 产物用途。
}

/** 独立验收详情；退出码不存在时为 null，预览可能截断。 */
export interface VerificationResult {
  status: Exclude<ValidationStatus, 'unverified'>; // 内容通过、内容不符或验收执行异常。
  reason: string; // 宿主稳定原因码。
  exit_code: number | null; // 实际子进程退出码。
  stdout: string; // 有界标准输出预览。
  stderr: string; // 有界错误输出预览。
  timed_out: boolean; // 验收或整次运行是否超时。
  cancelled: boolean; // 是否收到明确取消。
  output_limit_exceeded: boolean; // 原始输出是否超过保存上限。
  cleanup_status: CleanupStatus; // 验收子进程的清理结论。
  elapsed_ms: number; // 从启动到清理完成的毫秒数。
  truncated: boolean; // 输出是否截断。
  artifacts: ArtifactRef[]; // 验收原始输出引用。
}

/** 当前或最终执行结果；执行、验收和清理状态必须分别解释。 */
export interface RunResult {
  schema_version: 1; // 当前记录格式版本。
  run_id: string; // 本次运行 UUID。
  execution_status: ExecutionStatus; // 模型工具循环的执行状态。
  validation_status: ValidationStatus; // 独立验收的结论。
  verification: VerificationResult | null; // 未运行验收或旧记录时为 null。
  cleanup_status: CleanupStatus; // 所有已执行进程合并后的最坏清理状态。
  stop_reason: string | null; // 宿主结束原因码。
  model_turns: number; // 已发起的逻辑模型轮次。
  http_attempts: number; // 实际 HTTP 尝试次数，含重试。
  tool_calls: number; // 已接纳的工具调用次数。
  final_answer: string | null; // 最终模型回答，按安全文本展示。
  artifacts: ArtifactRef[]; // 已登记的所有产物。
  missing_artifacts: string[]; // 声明了但未找到的预期产物。
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number }; // 已知 token 数。
  usage_complete: boolean; // false 时 token 数仅为已知部分。
}

/** API 统一任务视图；active 与 execution_status 共同区分验收中和真实终态。 */
export interface RunView {
  run_id: string; // 可用于详情路由及订阅的 UUID。
  prompt: string; // 原始任务描述。
  workspace: string; // 本次实际使用的工作区绝对路径。
  created_at: string; // 带时区的创建时间。
  result: RunResult; // 持久化结果或安全构造的当前快照。
  phase: string; // 当前执行阶段。
  event_count: number; // 已保存事件总数。
  last_seq: number; // 最近已保存事件序号。
  active: boolean; // 后台任务仍存活，验收期间也为 true。
  cancelling: boolean; // 已请求取消且正在等待清理。
  interrupted: boolean; // 服务重启等导致执行中断，不能伪称成功。
  warnings: string[]; // 宿主给出的受控提示。
  config: RunConfig; // 本次任务的配置快照。
  project?: ProjectReview | null; // 项目任务的独立工作树与审阅证据；旧记录缺省。
}

/** 一条可按运行 ID 和连续序号去重的持久化事件。 */
export interface RunEvent {
  schema_version: 1; // 事件格式版本。
  run_id: string; // 所属任务 ID。
  seq: number; // 从 1 递增的序号。
  time_utc: string; // UTC 事件时间。
  elapsed_ms: number; // 单调时钟累计毫秒数。
  type: string; // 宿主事件类型。
  payload: Record<string, unknown>; // 不可信工具输出只按文本展示。
}

/** 服务启动时登记的工作区选项；前端不能提交任意绝对目录。 */
export interface WorkspaceOption {
  id: string; // 提交给 API 的受控标识。
  label: string; // 可读名称。
  path: string; // 源目录说明。
  copy_on_run: boolean; // true 时每次运行使用独立副本。
}

/** 宿主预设验收器；固定样例在名称与说明中标明适用范围。 */
export interface VerifierOption {
  id: string; // 可选验收器标识。
  label: string; // 面向用户的名称。
  description: string; // 适用范围与限制。
}

/** 宿主登记的 Git 项目；测试命令只能由宿主管理，页面不能覆盖。 */
export interface ProjectOption {
  id: string; // 提交给 API 的受控项目标识。
  label: string; // 项目的可读名称。
  path: string; // 原始 Git 仓库目录，只读展示。
  base_ref: string; // 默认提交引用，通常为 HEAD。
  head_commit: string | null; // 读取选项时的 HEAD 提交，不可用项目可能为 null。
  source_dirty: boolean; // 原目录是否包含不会带入任务的未提交改动。
  test_argv: string[]; // 宿主登记的测试参数列表，不经过浏览器拼接 shell。
  test_timeout_seconds: number; // 项目测试超时，单位秒。
  available?: boolean; // false 时登记项目目前不可使用，旧接口缺省视为可用。
  error?: string | null; // 项目不可用时的受控说明，可用时为 null。
}

/** 一项独立 Git 任务的代码快照及人工审阅；不代表提交或合并。 */
export interface ProjectReview {
  run_id: string; // 对应任务 UUID。
  project_id: string; // 项目登记标识。
  project_label: string; // 运行时项目名称快照。
  repository: string; // 原始仓库目录。
  base_ref: string; // 用户选择的提交引用。
  base_commit: string; // 任务建立时解析的不可变提交哈希。
  source_dirty: boolean; // 建立任务时原目录是否含未提交改动。
  worktree: string; // 本任务独立工作树目录。
  status: 'pending' | 'kept' | 'discarded'; // 等待人工决定、保留或丢弃，独立于测试状态。
  snapshot_status: 'pending' | 'ready' | 'error'; // 补丁证据是否完成，未完成不能做审阅决定。
  files: { path: string; status: 'added' | 'modified' | 'deleted' | 'renamed'; binary: boolean }[]; // 相对基线的增改删及二进制标记。
  patch_available: boolean; // 是否可以下载运行记录内保存的完整补丁。
  warnings: string[]; // 快照或清理过程的受控说明。
}

/** 代码补丁的有界纯文本预览；完整文件通过附件单独下载。 */
export interface DiffResponse {
  content: string; // 未解析的安全文本，不作为 HTML 或 Markdown 执行。
  truncated: boolean; // true 时预览仅包含补丁前段。
  size_bytes: number; // 完整补丁 UTF-8 字节数。
}

/** 启动能力选项；密钥仅返回是否配置，不返回值。 */
export interface OptionsResponse {
  config: RunConfig; // 服务端默认配置。
  key_configured: boolean; // 进程中是否存在可用模型密钥。
  workspaces: WorkspaceOption[]; // 已登记目录。
  verifiers: VerifierOption[]; // 已登记验收程序。
  projects?: ProjectOption[]; // 已登记 Git 项目；旧服务缺省等价于空列表。
  busy_run_id: string | null; // 唯一运行槽当前占用者。
}

/** 本浏览器下一次提交的配置；不修改服务器全局设置。 */
export interface ConfigOverrides {
  requested_model?: string; // 缺省沿用宿主模型。
  tools?: ToolName[]; // 缺省沿用宿主工具。
  limits?: Partial<RunLimits>; // 仅覆盖明确填写的预算字段。
}

/** 提交任务的受控 HTTP 请求体；不支持模型密钥或任意验收命令。 */
export type CreateRunRequest = {
  prompt: string; // 非空任务文本。
  expected_outputs: string[]; // 工作区相对产物路径。
  config: ConfigOverrides; // 仅影响本次任务的配置覆盖。
} & (
  | {
      workspace_id: string; // 普通任务的登记工作区 ID，与 project_id 互斥。
      verifier_id: string | null; // null 表示不做独立验收。
      project_id?: never; // 项目字段不能夹带进普通任务。
      base_ref?: never; // 普通任务没有 Git 提交基线。
    }
  | {
      project_id: string; // 代码任务的登记项目 ID，与 workspace_id 互斥。
      base_ref: string; // Git 提交基线，空白输入规范为 HEAD。
      workspace_id?: never; // 独立工作树由宿主创建，不能指定普通工作区。
      verifier_id?: never; // 项目只运行宿主登记的测试命令。
    }
);

/** 按序读取的事件分页结果。 */
export interface EventsResponse {
  items: RunEvent[]; // 按 seq 递增的事件页。
  has_more: boolean; // 是否还有更大的序号。
  last_seq: number; // 服务端已保存的最近序号。
}
