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

/** 上下文输入策略；全部大小以 UTF-8 字节计，不等于模型 token 数。 */
export interface ContextPolicy {
  load_project_instructions: boolean; // true 时加载实际工作区根目录的 AGENTS.md。
  max_input_bytes: number; // 消息和工具定义序列化后的整体输入字节上限。
  max_project_instruction_bytes: number; // 项目指令文件原始 UTF-8 字节上限，超限不截断。
  compression?: 'off' | 'masking' | 'hybrid'; // 旧宿主缺省关闭。
  trigger_ratio?: number; // 触发使用率，0至1。
  target_ratio?: number; // 目标使用率，必须小于触发比例。
  inline_tool_bytes?: number; // 成功工具正文外置阈值，UTF-8字节。
  keep_recent_groups?: number; // 原文保留的近期完整批次数。
  max_summary_calls?: number; // 摘要调用上限，同时占总模型轮次。
  max_summary_bytes?: number; // 摘要最大UTF-8字节数。
  model_context_tokens?: number | null; // 宿主声明窗口；null关闭token限制。
  token_safety_margin?: number; // 除输出外额外预留的token。
}

/** 建立运行时固定的项目指令快照；不存在或读取失败时不伪造内容与大小。 */
export interface ProjectInstructionSnapshot {
  status: 'loaded' | 'missing' | 'disabled' | 'error'; // 实际加载结论，独立于整个运行是否成功。
  path: 'AGENTS.md'; // 相对于本次实际工作区根目录的指令来源。
  content: string | null; // 完整且已验证 UTF-8 的指令文本；未加载时为 null。
  sha256: string | null; // 已加载内容的指纹，便于核对本次快照。
  size_bytes: number | null; // 已知文件字节数，未知时为 null，不显示为零。
  error_code: string | null; // 受控的指令读取错误码，无错误时为 null。
}

/** 发起模型请求前的输入检查；超限检查不会增加实际模型请求次数。 */
export interface ContextCheck {
  model_turn: number; // 本次检查拟发起的逻辑模型轮次，从 1 起。
  input_bytes: number; // 消息与工具定义的 UTF-8 JSON 字节总量。
  max_input_bytes: number; // 本次任务固定的整体输入字节上限。
  message_count: number; // 被计入本轮输入的消息数量。
  allowed: boolean; // 是否允许发起该次模型请求。
  raw_input_bytes?: number | null; // 压缩前大小，旧记录未知。
  estimated_input_tokens?: number | null; // 本地估算，不等于精确供应商计数。
  max_input_tokens?: number | null; // 扣除输出和安全余量后的输入预算。
  purpose?: 'execution' | 'summary'; // 实际检查用途。
}

/** 模型整理的历史工作状态；宿主测试和审阅另行展示。 */
export interface WorkSummary {
  goal: string; // 历史目标。
  decisions: string[]; // 已有决策。
  changes: string[]; // 已有修改。
  evidence: string[]; // 当时观察的证据。
  open_issues: string[]; // 未解决问题。
  next_steps: string[]; // 待办事项。
}

/** 原始消息半开区间上的持久输入变换。 */
export interface ContextAction {
  kind: 'offload' | 'mask' | 'summary' | 'summary_failed'; // 实际变换种类。
  model_turn: number; // 对应拟请求轮次。
  before_bytes: number; // 变换前整体字节。
  after_bytes: number; // 变换后整体字节。
  source_start: number; // 原始起始索引。
  source_end: number; // 不包含的结束索引。
  source_sha256: string; // 原始证据摘要。
  reference: string; // Agent可回读的引用。
  summary: WorkSummary | null; // 仅成功摘要携带。
  reason: string | null; // 失败原因。
}

/** 后端从完整轨迹聚合的上下文快照，不依赖浏览器当前保留的事件窗口。 */
export interface ContextView {
  instruction: ProjectInstructionSnapshot; // 本次实际加载的项目规则及错误。
  last_check: ContextCheck | null; // 最近一次预算检查，尚未检查时为 null。
  serialization: 'json_utf8_v1'; // 输入大小度量协议版本。
  actions?: ContextAction[]; // 最近50条变换，旧记录可缺省。
  action_count?: number; // 全部变换数，包含失败。
  summary_calls?: number; // 已开始摘要次数，包含失败/取消。
}

/** 可序列化运行设置；api_key_env 只保存变量名，前端从不接收密钥值。 */
export interface RunConfig {
  provider: 'deepseek'; // 当前宿主配置的供应商。
  requested_model: string; // 本次请求使用的模型名称。
  api_key_env: 'DEEPSEEK_API_KEY'; // 仅展示宿主环境变量来源。
  tools: ToolName[]; // 本次任务允许使用的工具集合。
  system_prompt: string; // 服务端系统提示词，只读展示。
  limits: RunLimits; // 每个任务独立的预算快照。
  context: ContextPolicy; // 项目指令加载与整体输入预算快照。
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

/** 一个恢复尝试所继承的检查点；旧运行无此来源。 */
export interface ResumeSource {
  source_run_id: string; // 保存检查点的原运行编号。
  checkpoint_id: string; // 不可变检查点 UUID。
  checkpoint_sha256: string; // 已核验检查点内容摘要。
}

/** 本次尝试之前已消耗的预算；累计值需加本次结果，停机时间不计入主动耗时。 */
export interface ResumeBudget {
  model_turns: number; // 前序尝试模型轮数，含摘要。
  http_attempts: number; // 前序 HTTP 尝试次数。
  tool_calls: number; // 前序接纳工具数。
  summary_calls: number; // 前序已发起摘要调用。
  prompt_tokens: number; // 前序已知输入 token。
  completion_tokens: number; // 前序已知输出 token。
  total_tokens: number; // 前序已知 token 总数。
  usage_complete: boolean; // false 表示前序请求有未知供应商用量。
  active_duration_seconds: number; // 前序主动执行秒数，不含中断后的停机时间。
}

/** 可供用户明确恢复的最近安全检查点；读取不触发执行。 */
export interface RecoveryCheckpoint {
  checkpoint_id: string; // 恢复请求必须固定的检查点 UUID。
  created_at: string; // 检查点保存的带时区时间。
  event_seq: number; // 对应原运行的持久化事件位置。
  model_turns: number; // 恢复前累计模型调用数，包含封存后已开始的请求。
  tool_calls: number; // 恢复前累计工具调用数。
  active_duration_seconds: number; // 恢复前累计主动时长；缺失响应按单次时限保守预留上界。
  summary_calls: number; // 恢复前累计摘要次数。
}

/** 服务端重新核验的恢复条件；按钮状态不能替代 POST 时的最终核验。 */
export interface RecoveryView {
  supported: boolean; // 本版仅 Git 项目任务支持恢复。
  can_resume: boolean; // 当前记录、工作树及运行槽均允许恢复。
  blocked_code: string | null; // 不能恢复时的稳定原因码。
  blocked_reason: string | null; // 面向用户的受控中文说明。
  checkpoint: RecoveryCheckpoint | null; // 无安全检查点的旧记录为空。
  busy_run_id: string | null; // 唯一运行槽占用者，空表示无活动运行。
}

/** 明确恢复的幂等请求；网络未知时必须重用同一完整载荷。 */
export interface ResumeRequest {
  request_id: string; // 浏览器生成 UUID，刷新后仍沿用待确认编号。
  checkpoint_id: string; // 首次点击时固定的检查点，不因补读而悄悄换点。
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
  final_answer: string | null; // 最终模型原文，界面按受限Markdown展示。
  artifacts: ArtifactRef[]; // 已登记的所有产物。
  missing_artifacts: string[]; // 声明了但未找到的预期产物。
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number }; // 已知 token 数。
  usage_complete: boolean; // false 时 token 数仅为已知部分。
  resume_budget?: ResumeBudget | null; // 本次执行前的累计消耗；旧记录无值。
  active_duration_seconds?: number; // 本次主动执行秒数；旧记录缺省不推断。
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
  context: ContextView | null; // 聚合上下文证据；旧记录或尚未准备时为 null。
  project?: ProjectReview | null; // 项目任务的独立工作树与审阅证据；旧记录缺省。
  session_id?: string | null; // 线性会话根编号；旧接口可缺省。
  parent_run_id?: string | null; // 首轮为空，后续指向前轮。
  turn_index?: number; // 用户要求序号，不等于模型轮数。
  resume_source?: ResumeSource | null; // 恢复来源；普通执行与旧记录为空。
  attempt_index?: number; // 同一用户要求的执行尝试，从 1 起，恢复时递增。
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
  context?: Partial<ContextPolicy>; // 逐字段覆盖本次上下文策略，未设置字段沿用宿主默认。
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

/** 用户追加要求；请求编号在网络重试时保持，避免重复修改。 */
export interface FollowupRequest {
  request_id: string; // 浏览器生成的 UUID。
  prompt: string; // 本次新增要求。
  config?: ConfigOverrides; // 默认沿用前轮，可用当前配置草稿覆盖。
  expected_outputs?: string[] | null; // 缺省沿用前轮预期产物。
}

/** 同一用户要求的一次执行；每次恢复都有独立轨迹、验收与审阅。 */
export interface ConversationAttempt {
  run_id: string; // 本次执行编号，可回看原记录。
  attempt_index: number; // 同轮执行尝试序号，首次为 1。
  interrupted?: boolean; // 服务中断导致缺少可信终态，不能继续显示为活动执行。
  resume_source: ResumeSource | null; // 首次执行为空，恢复时指向原检查点。
  final_answer: string | null; // 本次执行的最终回答。
  execution_status: ExecutionStatus | null; // 本次执行状态，未知时为空。
  validation_status: ValidationStatus | null; // 本次独立验收状态。
  created_at: string | null; // 本次运行创建时间。
}

/** 连续对话的一轮；详细测试、事件与差异仍在独立运行详情。 */
export interface ConversationTurn {
  run_id: string; // 本轮运行编号。
  parent_run_id: string | null; // 前轮编号。
  turn_index: number; // 从1开始的用户请求序号。
  prompt: string; // 用户要求。
  final_answer: string | null; // 尚无最终回答时为空。
  execution_status: ExecutionStatus | null; // 未建立可信运行记录时未知。
  validation_status: ValidationStatus | null; // 各轮独立验收结果。
  created_at: string | null; // 可信创建时间。
  interrupted?: boolean; // 最新尝试因服务中断缺少终态；旧接口可缺省。
  attempts?: ConversationAttempt[]; // 同一要求的有序执行尝试；旧接口缺省只有 turn 本身。
}

/** 会话读取结果；可继续为初步条件，提交时后端会重新核验实际代码。 */
export interface ConversationView {
  continuation_notice?: string | null; // 中断后可以普通发送继续时的非阻断提示。
  session_id: string; // 会话根编号。
  latest_run_id: string; // 只允许从最新轮次追加。
  turns: ConversationTurn[]; // 按用户要求顺序排列。
  can_continue: boolean; // 当前是否具备基本继续条件。
  blocked_code: string | null; // 阻止原因码。
  blocked_reason: string | null; // 受控中文提示。
  busy_run_id: string | null; // 当前全局活动运行。
}
