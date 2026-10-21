import type {
  OptionsResponse,
  ProjectOption,
  ProjectReview,
  RunConfig,
  RunEvent,
  RunView,
} from '../types';

// 完整无密钥配置；与后端字段契约一致，测试不调用真实模型。
export const config: RunConfig = {
  provider: 'deepseek',
  requested_model: 'deepseek-flash',
  api_key_env: 'DEEPSEEK_API_KEY',
  tools: ['read_file', 'search_files', 'write_file', 'run_command'],
  system_prompt: '如实执行任务。',
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
  workspaces: [{ id: 'logs', label: '日志样例', path: '/examples/logs', copy_on_run: true }],
  verifiers: [
    { id: 'logs', label: '固定日志样例专用验收', description: '只检查随仓库提供的固定日志输入。' },
  ],
  busy_run_id: null,
};

/** 构造独立任务视图；覆盖项只修改顶层，嵌套状态由调用方明确复制。 */
export function makeRun(overrides: Partial<RunView> = {}): RunView {
  return {
    run_id: 'run-a',
    prompt: '统计日志',
    workspace: '/workspace',
    created_at: '2026-10-19T10:00:00Z',
    phase: 'finished',
    event_count: 0,
    last_seq: 0,
    active: false,
    cancelling: false,
    interrupted: false,
    warnings: [],
    config,
    result: {
      schema_version: 1,
      run_id: 'run-a',
      execution_status: 'completed',
      validation_status: 'unverified',
      verification: null,
      cleanup_status: 'not_needed',
      stop_reason: 'final_answer',
      model_turns: 1,
      http_attempts: 1,
      tool_calls: 0,
      final_answer: '执行完成',
      artifacts: [],
      missing_artifacts: [],
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
export const projectOption: ProjectOption = {
  id: 'sample-project', // 受控项目 ID。
  label: '示例后端', // 展示名称。
  path: '/source/backend', // 原仓库只读说明。
  base_ref: 'HEAD', // 初始选择。
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
