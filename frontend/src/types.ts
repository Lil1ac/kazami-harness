/** Python 数据模型的浏览器表示；字段名与后端持久化及 HTTP 契约保持一致。 */
export type ToolName = 'read_file' | 'search_files' | 'write_file' | 'edit_file' | 'run_command';
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
  compression: 'off' | 'masking' | 'hybrid'; // 宿主选择的压缩策略。
  trigger_ratio: number; // 触发使用率，0至1。
  target_ratio: number; // 目标使用率，必须小于触发比例。
  inline_tool_bytes: number; // 成功工具正文外置阈值，UTF-8字节。
  keep_recent_groups: number; // 原文保留的近期完整批次数。
  max_summary_calls: number; // 摘要调用上限，同时占总模型轮次。
  max_summary_bytes: number; // 摘要最大UTF-8字节数。
  model_context_tokens: number | null; // 宿主声明窗口；null关闭token限制。
  token_safety_margin: number; // 除输出外额外预留的token。
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

/** 宿主发现的技能选项；不可用项仍保留受控说明，页面不能修改来源。 */
export interface SkillOption {
  name: string; // 可提交的技能标识；不可用项仅用于说明。
  description: string; // 描述适用任务与工作方式。
  source: string; // 宿主登记文件来源，只读展示。
  available: boolean; // false 时不能新增选择。
  error_code?: string | null; // 发现或解析失败的稳定原因码。
  error_message?: string | null; // 宿主返回的安全中文说明。
}

/** 本轮建立时固定的完整技能文件，历史显示不读取当前目录。 */
export interface SkillSnapshot {
  name: string; // 本轮实际加载的技能标识。
  description: string; // 本轮文件中的用途描述。
  source: string; // 加载时的文件来源。
  content: string; // 完整 UTF-8 SKILL.md，包含 frontmatter。
  sha256: string; // 原文件字节的 SHA-256。
  size_bytes: number; // 原文件 UTF-8 字节数。
}

/** 所选技能加载失败时保留的受控错误；失败发生在模型与工具执行之前。 */
export interface SkillFailure {
  name?: string | null; // 已知的失败技能；无法确定时为空。
  code: string; // 宿主稳定错误码。
  message: string; // 面向用户的安全说明。
}

/** 后端从完整轨迹聚合的上下文快照，不依赖浏览器当前保留的事件窗口。 */
export interface ContextView {
  instruction: ProjectInstructionSnapshot; // 本次实际加载的项目规则及错误。
  last_check: ContextCheck | null; // 最近一次预算检查，尚未检查时为 null。
  serialization: 'json_utf8_v1'; // 输入大小度量协议版本。
  actions?: ContextAction[]; // 最近50条变换，旧记录可缺省。
  action_count?: number; // 全部变换数，包含失败。
  summary_calls?: number; // 已开始摘要次数，包含失败/取消。
  skills?: SkillSnapshot[]; // 本轮固定的实际技能快照；旧记录缺省时不推断。
  skill_error?: SkillFailure | null; // 所选技能读取失败的原因，无错误时为空。
}

/** 可序列化运行设置；api_key_env 只保存变量名，前端从不接收密钥值。 */
export interface RunConfig {
  auto_repair: boolean; // 公开项目测试失败时最多自动修复两次，默认关闭，不重置预算。
  provider: 'deepseek'; // 当前宿主配置的供应商。
  requested_model: string; // 本次请求使用的模型名称。
  api_key_env: 'DEEPSEEK_API_KEY'; // 仅展示宿主环境变量来源。
  tools: ToolName[]; // 本次任务允许使用的工具集合。
  system_prompt: string; // 服务端系统提示词，只读展示。
  skill_roots?: string[]; // 宿主登记技能集合目录，只读且不能通过页面覆盖。
  skills?: string[]; // 本次选择的技能名称；空列表明确表示不加载。
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

/** 对应某次代码版本的实际检查，0为首次，1/2为相应修复后的结果。 */
export interface VerificationAttempt {
  repair_attempt: number; // 检查时已经开始的修复次数，不是模型请求次数。
  result: VerificationResult; // 当次真实进程结果与独立原始输出引用。
}

/** 当前或最终执行结果；执行、验收和清理状态必须分别解释。 */
export interface RunResult {
  schema_version: 1; // 当前记录格式版本。
  run_id: string; // 本次运行 UUID。
  execution_status: ExecutionStatus; // 模型工具循环的执行状态。
  validation_status: ValidationStatus; // 独立验收的结论。
  verification: VerificationResult | null; // 未运行验收或旧记录时为 null。
  verification_attempts: VerificationAttempt[]; // 全部实际检查，后续通过不覆盖此前失败。
  repair_attempts: number; // 已启动的自动修复次数，0至2，与用户轮次分别记录。
  cleanup_status: CleanupStatus; // 所有已执行进程合并后的最坏清理状态。
  stop_reason: string | null; // 宿主结束原因码。
  model_turns: number; // 已发起的逻辑模型轮次。
  http_attempts: number; // 实际 HTTP 尝试次数，含重试。
  tool_calls: number; // 已接纳的工具调用次数。
  final_answer: string | null; // 最终模型原文，界面按受限Markdown展示。
  artifacts: ArtifactRef[]; // 已登记的所有产物。
  artifact_status: 'pending' | 'complete' | 'incomplete'; // 文件收集状态，独立于执行和测试。
  artifact_warnings: string[]; // 采集受限或证据未完整保存的后端说明。
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number }; // 已知 token 数。
  usage_complete: boolean; // false 时 token 数仅为已知部分。
  active_duration_seconds?: number; // 本次主动执行秒数；旧记录缺省不推断。
}

/** 当轮 Python 执行快照；源码目录和解释器从持久记录读取。 */
export interface PythonExecution {
  workspace: string; // 当轮实际工作区，作为源码目录边界。
  executable: string | null; // 当轮项目配置或 PATH 解析的解释器；null 表示未找到 Python。
  python_paths: string[]; // 当轮固定的源码导入目录，工作区根目录优先。
}

/** API 统一任务视图；运行快照不随当前项目配置改变。 */
export interface RunView {
  run_id: string; // 可用于详情路由及订阅的 UUID。
  prompt: string; // 原始任务描述。
  workspace: string; // 工具、命令和验收实际使用的绝对目录。
  python_execution: PythonExecution | null; // 保存的解释器与源码目录；null 时不猜测历史执行环境。
  verification_kind: 'independent' | 'project_tests' | null; // 固定检查用途，公开项目测试才允许自动修复反馈。
  execution_mode?: 'local' | 'isolated' | null; // CLI没有网页来源时可为空；网页会话明确固定此值。
  source_path?: string | null; // 用户最初选择的目录。
  source_kind?: 'project' | 'workspace' | null; // 项目测试与普通验收文案的来源。
  local_changes?: LocalChangeView | null; // 原目录的本轮前后差异，隔离任务为空。
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
  project_workspace: ProjectWorkspace | null; // 当前会话工作树状态；旧轮无管理权限。
  project?: ProjectReview | null; // 项目任务的独立工作树与审阅证据；旧记录缺省。
  session_id?: string | null; // 线性会话根编号；旧接口可缺省。
  parent_run_id?: string | null; // 首轮为空，后续指向前轮。
  turn_index?: number; // 用户要求序号，不等于模型轮数。
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

/** 宿主预设验收器；固定样例在名称与说明中标明适用范围。 */
export interface VerifierOption {
  id: string; // 可选验收器标识。
  label: string; // 面向用户的名称。
  description: string; // 适用范围与限制。
}

/** 会话级 Git 工作树生命周期；各轮代码证据单独保存。 */
export interface ProjectWorkspace {
  session_id: string; // 共享工作树所属会话，目录不再按轮新建。
  latest_run_id: string; // 唯一允许管理目录的最新轮。
  worktree: string; // 会话持续使用的实际目录。
  status: 'pending' | 'kept' | 'discarded'; // 最新代码的保留/丢弃状态。
}

/** 每轮独立固定的项目代码证据；旧轮只读。 */
export interface ProjectReview {
  session_id: string; // 工作树属于该会话；run_id 只标识本轮证据。
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

/** 用户明确打开的本机目录；Git元数据仅在kind=project时存在。 */
export interface LocalProjectOption {
  python_executable?: string | null; // 用户明确设置的解释器；空值按 PATH，不执行程序。
  id: string; // 后续任务只提交这个稳定编号。
  label: string; // 用户可读的文件夹名称。
  path: string; // 真实本机路径，仅作展示。
  kind: 'project' | 'workspace'; // Git项目或普通目录。
  last_opened_at?: string | null; // 用户最后明确打开的时间，读取页面不更新。
  available?: boolean; // false表示路径或Git状态暂不可用。
  error?: string | null; // 不可用的具体原因。
  branch?: string | null; // Git分支。
  source_dirty?: boolean; // 原目录是否含未提交改动。
  head_commit?: string | null; // 当前Git提交。
  test_argv?: string[]; // 已固定的测试程序与参数；空数组表示未配置。
  test_command?: string; // 编辑用测试命令。
  test_timeout_seconds?: number; // 项目测试的秒级期限。
}

/** 启动能力选项；密钥仅返回是否配置，不返回值。 */
export interface OptionsResponse {
  recent_projects: LocalProjectOption[]; // 用户目录与宿主初始项目，按最近打开排序。
  folder_picker_available?: boolean; // 本机原生文件夹选择器是否受支持。
  config: RunConfig; // 服务端默认配置。
  key_configured: boolean; // 进程中是否存在可用模型密钥。
  verifiers: VerifierOption[]; // 已登记验收程序。
  skills?: SkillOption[]; // 宿主技能目录元数据；正文只在所选运行中加载。
  busy_run_id: string | null; // 唯一运行槽当前占用者。
}

/** 本浏览器下一次提交的配置；不修改服务器全局设置。 */
export interface ConfigOverrides {
  auto_repair?: boolean; // 本轮明确开启/关闭自动修复；缺省继承宿主或前轮。
  requested_model?: string; // 缺省沿用宿主模型。
  tools?: ToolName[]; // 缺省沿用宿主工具。
  skills?: string[]; // 缺省继承宿主或前轮选择，[] 明确清空。
  limits?: Partial<RunLimits>; // 仅覆盖明确填写的预算字段。
  context?: Partial<ContextPolicy>; // 逐字段覆盖本次上下文策略，未设置字段沿用宿主默认。
}

/** 提交任务的受控 HTTP 请求体；不支持模型密钥或任意验收命令。 */
export type CreateRunRequest = {
  prompt: string; // 非空任务文本。
  execution_mode?: 'local' | 'isolated'; // 默认所选目录；isolated 仅用于主动创建 Git 工作树。
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
  interrupted?: boolean; // 本轮因服务中断缺少终态；旧接口可缺省。
}

/** 会话读取结果；可继续为初步条件，提交时后端会重新核验实际代码。 */
export interface ConversationView {
  source?: ConversationSource | null; // 用于返回所属项目和在相同来源新建会话。
  continuation_notice?: string | null; // 中断后可以普通发送继续时的非阻断提示。
  session_id: string; // 会话根编号。
  latest_run_id: string; // 只允许从最新轮次追加。
  turns: ConversationTurn[]; // 按用户要求顺序排列。
  can_continue: boolean; // 当前是否具备基本继续条件。
  blocked_code: string | null; // 阻止原因码。
  blocked_reason: string | null; // 受控中文提示。
  busy_run_id: string | null; // 当前全局活动运行。
}

/** 创建会话时固定的来源；编号、类型和路径必须同时匹配当前登记。 */
export interface ConversationSource {
  id: string; // 原宿主登记编号，普通文件夹和Git项目统一导航。
  kind: 'project' | 'workspace'; // 原目录类型。
  label: string; // 可读项目名称。
  path: string; // 原目录，而非逐轮派生目录。
  registered: boolean; // 当前登记是否仍指向相同目录。
  execution_mode: 'local' | 'isolated'; // 同一会话保持明确的执行方式，不猜测缺失模式。
}

/** 最新轮的有界导航状态，不携带完整回答和工具记录。 */
export interface ConversationRunState {
  execution_status: ExecutionStatus; // 最新轮执行结果。
  validation_status: ValidationStatus; // 最新轮独立验收结果。
  cleanup_status: CleanupStatus; // 最新轮清理结果。
  active: boolean; // 运行或整理快照期间为true。
  cancelling: boolean; // 是否已经明确请求取消。
  interrupted: boolean; // 本服务没有执行、且历史缺少可信终态。
  phase: string; // 当前真实阶段。
}

/** 一个会话只有一个入口，异常记录保留入口并显示受控原因。 */
export interface ConversationSummary {
  session_id: string; // 稳定会话编号。
  title: string; // 首次用户要求形成的标题。
  entry_run_id: string; // 正常进入最新轮，异常时进入可读轮次。
  latest_run_id: string | null; // 以权威会话关系为准，未知时为空。
  turn_count: number | null; // 已接纳用户轮数，记录损坏时未知。
  created_at: string; // 会话创建时间。
  updated_at: string; // 最后已落盘事件时间，查看不更新时间。
  source: ConversationSource | null; // 旧CLI记录可能没有项目来源。
  latest: ConversationRunState | null; // 最新轮不可核对时不显示旧轮通过。
  error: string | null; // 关联或记录异常的中文说明。
}

/** 按完整会话筛选、排序和分页之后的导航结果。 */
export interface ConversationPage {
  items: ConversationSummary[]; // 当前页每会话一条。
  total: number; // 筛选后的会话总数。
  busy_run_id: string | null; // 真实全局单槽。
}

/** 原目录相对本轮开始状态的变化，不提供自动保留、丢弃或回滚原目录操作。 */
export interface LocalChangeView {
  status: 'pending' | 'ready' | 'error'; // 差异报告采集状态，与执行/验收分开。
  files: { path: string; status: 'added' | 'modified' | 'deleted'; binary: boolean }[]; // 完整变化清单。
  patch_available: boolean; // 固定文本差异可读取；二进制仅列变化。
  warnings: string[]; // 超出范围或损坏时的受控提示。
}
