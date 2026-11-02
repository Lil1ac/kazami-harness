import type { ConversationManagement } from '../types';
import type {
  OptionsResponse,
  LocalProjectOption,
  ProjectReview,
  RunConfig,
  RunEvent,
  RunView,
  JobView,
} from '../types';

export const jobRuns = new Map<string, RunView>(); // 作业确认后可读取的真实运行夹具。

/** 将 run 包装为已建立运行的作业夹具；登记只读 Run 响应，返回统一 JobView。 */
export function makeJob(run: RunView = makeRun()): JobView {
  jobRuns.set(run.run_id, run);
  return {
    id: 'job-' + run.run_id,
    run_id: run.run_id,
    session_id: run.session_id || run.run_id,
    title: run.prompt,
    project_id: 'sample',
    project_label: '示例项目',
    status: run.active ? 'running' : 'completed',
    error: null,
    execution_status: run.result.execution_status,
    validation_status: run.result.validation_status,
  };
}

// 完整无密钥配置；与后端字段契约一致，测试不调用真实模型。
export const config: RunConfig = {
  memory: { use: true, capture: true, max_input_bytes: 16384, max_entries: 6 }, // 与宿主默认一致。
  planning_policy: 'auto', // 默认由Agent判断；强制规划由宿主TOML配置。
  auto_repair: false, // 默认只检查一次，公开项目测试需明确开启修复。
  provider: 'deepseek',
  requested_model: 'deepseek-flash',
  api_key_env: 'DEEPSEEK_API_KEY',
  tools: ['read_file', 'search_files', 'write_file', 'run_command'],
  system_prompt: '如实执行任务。',
  context: {
    load_project_instructions: true, // 默认加载根目录项目规则。
    max_input_bytes: 262144, // 整体输入 256 KiB。
    max_project_instruction_bytes: 32768, // 指令文件 32 KiB。
    compression: 'off', // 此组离线测试默认保留完整输入。
    trigger_ratio: 0.8, // 接近预算时触发压缩。
    target_ratio: 0.6, // 压缩后期望占用比例。
    inline_tool_bytes: 8192, // 大结果的外置阈值。
    keep_recent_groups: 3, // 保留近期完整工具批次。
    max_summary_calls: 2, // 每轮最多两次摘要调用。
    max_summary_bytes: 8192, // 单份摘要正文上限。
    model_context_tokens: null, // 未声明额外token窗口。
    token_safety_margin: 2048, // 声明窗口时额外预留token。
  },
  limits: {
    max_model_turns: 20,
    max_tool_calls: 40,
    model_max_output_tokens: 4096,
    max_http_attempts_per_turn: 3,
    model_turn_timeout_seconds: 60,
    command_timeout_seconds: 30,
    verifier_timeout_seconds: 30,
    run_timeout_seconds: 300,
    cleanup_grace_seconds: 5,
    tool_output_preview_chars: 12000,
    max_process_capture_bytes: 8388608,
    max_file_write_bytes: 1048576,
  },
};
export const options: OptionsResponse = {
  config,
  key_configured: true,
  recent_projects: [], // 首次启动等待用户打开真实目录。
  verifiers: [], // 无宿主登记时不提供独立验收选择。
  busy_run_id: null,
};

/** 构造独立任务视图；覆盖项只修改顶层，嵌套状态由调用方明确复制。 */
export function makeRun(overrides: Partial<RunView> = {}): RunView {
  return {
    run_id: 'run-a',
    prompt: '统计日志',
    workspace: '/workspace',
    project_workspace: overrides.project
      ? {
          session_id: overrides.project.session_id,
          latest_run_id: overrides.run_id || 'run-a',
          worktree: overrides.project.worktree,
          status: overrides.project.status,
        }
      : null, // 模拟真实会话当前状态。
    python_execution: null, // 未准备执行配置的记录；测试可提供独立轮次快照。
    verification_kind: null, // 此夹具默认没有宿主检查。
    created_at: '2026-10-19T10:00:00Z',
    phase: 'finished',
    event_count: 0,
    last_seq: 0,
    active: false,
    cancelling: false,
    interrupted: false,
    warnings: [],
    config,
    context: null, // 默认模拟尚未记录上下文的历史任务。
    result: {
      planning: { phase: 'execution', reason: null, plans: [], pending: null, decisions: [] },
      schema_version: 1,
      run_id: 'run-a',
      execution_status: 'completed',
      validation_status: 'unverified',
      verification: null,
      verification_attempts: [],
      repair_attempts: 0,
      cleanup_status: 'not_needed',
      stop_reason: 'final_answer',
      model_turns: 1,
      http_attempts: 1,
      tool_calls: 0,
      final_answer: '执行完成',
      artifacts: [],
      artifact_status: 'complete',
      artifact_warnings: [],
      usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      usage_complete: true,
    },
    ...overrides,
  };
}

/** 构造事件；seq 为正整数，runId 默认当前任务，便于验证跨任务隔离。 */
export function makeEvent(seq: number, runId = 'run-a'): RunEvent {
  return {
    schema_version: 1,
    run_id: runId,
    seq,
    time_utc: '2026-10-19T10:00:00Z',
    elapsed_ms: seq * 10,
    type: 'tool_result',
    payload: { name: 'read_file' },
  };
}

/** 无密钥的代码项目预设；允许测试原目录含未提交改动的说明和固定测试 argv。 */
export const projectOption: LocalProjectOption = {
  id: 'sample-project', // 受控项目 ID。
  label: '示例后端', // 展示名称。
  path: '/source/backend', // 原仓库只读说明。
  kind: 'project', // Git 项目与普通目录共用一个选项结构。
  head_commit: 'abcdef1234567890', // 可见的当前 HEAD。
  source_dirty: true, // 原目录有不会带入任务的改动。
  test_argv: ['python', '-m', 'pytest', '-q'], // 宿主测试命令，前端不覆盖。
  test_timeout_seconds: 60, // 秒。
  available: true, // 可以选择。
  error: null, // 无不可用原因。
};

/** 构造审阅证据；overrides 修改指定事实，不触发 Git 或模型请求。 */
export function makeProject(overrides: Partial<ProjectReview> = {}): ProjectReview {
  return {
    run_id: 'run-a',
    session_id: 'run-a', // 首轮创建的会话，后续复用该工作树。
    project_id: projectOption.id,
    project_label: projectOption.label,
    repository: projectOption.path,
    base_ref: 'HEAD',
    base_commit: projectOption.head_commit!, // 本测试预设始终可用，具有非空提交哈希。
    source_dirty: true,
    worktree: '/worktrees/run-a',
    status: 'pending',
    snapshot_status: 'ready',
    files: [{ path: 'app.py', status: 'modified', binary: false }],
    patch_available: true,
    warnings: [],
    ...overrides,
  };
}

/** 构造完整管理视图；overrides仅覆盖明确的测试状态，不从消息或目录猜测权限。 */
export function makeConversationManagement(
  overrides: Partial<ConversationManagement> = {},
): ConversationManagement {
  return {
    session_id: 'run-a',
    title: '统计日志',
    title_override: null,
    assigned_project_id: null,
    assigned_project: null,
    state: 'active',
    pinned_at: null,
    deleted_at: null,
    restore_state: 'active',
    revision: 0,
    can_organize: true,
    blocked_reason: null,
    can_purge: false,
    purge_blocked_reason: '请先移入回收站',
    run_count: 1,
    memory_references: 0,
    ...overrides,
  };
}
