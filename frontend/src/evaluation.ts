import { request } from './api';
import type { BudgetHandoffTrigger, MemoryEvidenceRef, MemoryUsage, PublicSelfTestResult } from './types';

/** 独立评分回执；官方测试名区分缺陷复现与回归，不从进程退出码猜测通过。 */
export interface EvaluationGrade {
  status: string; // passed、failed、error或尚未评分。
  stdout?: string; stderr?: string; reason?: string | null; // 有界日志及环境错误原因。
  official?: {
    resolved: boolean; // 上游全部指定断言是否满足。
    tests_status?: Record<string, { success: string[]; failure: string[] }>; // FAIL_TO_PASS与PASS_TO_PASS逐项结果。
  };
}

/** 批次账本金额均为皮美元，1 USD 等于 10^12 皮美元。 */
export interface EvaluationCost {
  known_usd_picos: number; // 已收到有效用量的峰时估算。
  reserved_usd_picos: number; // 已发请求尚缺可信回执的保留金额。
}

/** 以题为单位的成败配对，未完成或证据异常的题不进入有效分母。 */
interface EvaluationPair {
  metric?: 'delivery' | 'resolved'; // 完整交付与独立修复分别配对。
  baseline: string; candidate: string; // 方向为候选相对基线。
  wins: string[]; losses: string[]; ties: string[]; // 各结果对应的题号。
  valid_pairs: number; planned_pairs: number; // 有效与计划题数。
}

/** 报告启动时为每个策略和重复预分配的位置及编号。 */
export interface EvaluationSlot {
  run_id: string; case_id: string; strategy: string; repeat: number; // repeat 从 0 开始。
}

/** 一个已落盘试次的执行事实及独立评分。 */
export interface EvaluationTrial extends EvaluationSlot {
  host_entry?: { status: 'passed' | 'failed' | 'error'; output: string }; // 宿主在送评补丁上执行的独立入口复核，缺省表示无回执。
  entry?: {
    status: string; // Agent 自选用例在冻结脚本上的结果，或候选在验证后变化。
    entry_kind?: 'http' | 'admin' | 'cli' | 'api' | 'self'; // 原始问题可由宿主观测的操作边界。
    next_action?: string; // 已登记脚本仅覆盖下层组件时的修订建议。
    coverage_review?: string | null; // 页面用例通过但只验证了一种显示状态时的核查提示。
    progress?: {batch: number; state: string; paths?: string[]; path_count?: number}[]; // 定期采集的实际工作树差异。
    baseline?: {status: string; command: string; output: string}; // 原始用户入口的命令与可见结果。
    candidate?: {status: string; command: string; output: string}; // 针对已登记补丁、同一用例的回执。
    files?: string[]; omitted?: string[]; submitted_sha256?: string; // 实际交付范围及指纹。
  };
  executor?: 'agent_harness' | 'mini_swe_agent'; // 当前试次的实际执行器。
  context_policy?: string; // 与执行器独立冻结的上下文条件。
  trajectory_available?: boolean; trajectory_sha256?: string; // 外部基线原生轨迹的登记状态和摘要。
  agent_seconds?: number; preparation_seconds?: number; grading_seconds?: number; // 三段墙钟秒数。

  success: boolean; execution_status: string; stop_reason: string | null; // 执行与实际达成分别表达。
  grade: EvaluationGrade; // 外部评分证据。
  source_url?: string | null; // 任务来源修复 PR。
  run_available?: boolean; // false 表示准备中止而未创建 Run。
  interrupted?: boolean; // 已启动但尚未取到完整试次回执。
  patch_available?: boolean; // 宿主成功采集并登记候选补丁时为 true。
  patch_bytes?: number | null; // 宿主实际补丁字节数，0 表示空补丁。
  search?: { started: number; total_ms: number; local_timeouts: number; run_timeouts: number }; // 实际搜索及其停止回执。
  first_write?: { path: string; model_turn: number | null; seq: number; elapsed_ms: number }; // 首条成功文件写入的事实。
  grade_artifacts?: { score?: string; log?: string }; // 评分 JSON 与测试日志的 SHA256。
  grading_recovery?: { original_report_id: string; receipt_id: string; external_model_requests: number; grading_seconds: number }; // 同一模型产物补评分的原异常来源和新增耗时，不增加模型试次。
  failure_type?: string | null; // 执行失败、评分环境异常或测试断言失败。
  known_tokens: number; usage_complete: boolean; // 本试次用量与完整性。
  cost?: (EvaluationCost & { complete: boolean }) | null; // 收费受控批次的费用与完整性。
  model_calls?: number; http_attempts?: number; duration_seconds?: number; // 实际调用与执行加评分的秒数。
  input_bytes_peak?: number; summary_calls?: number; // 压缩检查的字节峰值和摘要尝试。
  execution_calls?: number; // 已接纳的执行请求，包含末轮收尾，独立于摘要。
  last_admitted_input_bytes?: number | null; // 最后接纳的执行输入字节数，尚未请求时为 null。
  last_rejected_input_bytes?: number | null; // 最后被输入门禁拒绝的字节数。
  handoff_status?: 'not_triggered' | 'started' | 'ready' | 'partial' | 'invalid' | 'skipped'; // 根 Run 的预算内收尾回执。
  handoff_trigger?: BudgetHandoffTrigger | null; // 触发末轮或普通输入被拒时的收尾判断。
  handoff_input_bytes?: number | null; // 紧凑收尾实际接纳的输入字节数。
  handoff_checked_bytes?: number | null; // 最后紧凑检查的字节数，包括未发送的请求。
  handoff_skip_reason?: string | null; // 收尾前置条件未满足时记录的事实。
  public_self_test?: PublicSelfTestResult | null; // 当前候选对应的公开测试结果，不影响独立评分字段。
  response_model?: string | null; // 服务端最后响应的实际模型。
  compression_applied?: boolean; // 是否实际发生外置、遮蔽或摘要。
  context_actions?: string[]; // 落盘的上下文变换及失败动作。
  budget_projection_actions?: number; // 为整体输入预算实际缩短的回执数量。
  memory?: {
    required: string[]; retrieved: string[]; exposed: string[]; forbidden_hits: string[]; // 预标注证据的检索、实际输入与误用。
    searches: number; topic_requests: number; // 此试次实际历史查询和主题展示次数。
    evidence: MemoryEvidenceRef[]; // 已发送的可回读来源。
  };
}

/** 已冻结策略统计；null表示没有足够数据，不能当成零。 */
export interface EvaluationGroup {
  executor?: string; context_policy?: string; // 冻结执行器与上下文条件。
  patch_pass_rate?: number | null; // 独立修复比例，不要求Agent正常交付。
  known_usd_picos?: number; cost_complete?: boolean; // 包含失败任务的费用与完整性。
  cost_per_delivery_usd_picos?: number | null; cost_per_resolved_usd_picos?: number | null; // 两种成功口径的单位费用。
  cache_hit_ratio?: number | null; // 供应商实际缓存命中token占输入比例。
  summary_calls?: number; compression_trials?: number; context_limit_failures?: number; // 上下文机制触发事实。
  agent_seconds_p50?: number | null; agent_seconds_p95?: number | null; // 独立Agent耗时分位数。
  grading_seconds_p50?: number | null; grading_seconds_p95?: number | null; // 外部评分耗时分位数。
  strata?: Record<string, { name: string; trials: number; deliveries: number; resolved: number }[]>; // 仓库与难度分层结果。
  failures?: Record<string, number>; // 完整交付失败的原因计数。

  strategy: string; // 本组的压缩或记忆对照条件。
  planned: number; // 冻结计划中的试次数。
  finished: number; // 已保存评分回执的次数。
  successes: number; // 完成且独立评分通过的次数。
  patch_passes?: number; scored?: number; // 独立评分通过及已评分的次数。
  success_rate: number | null; // 已结束试次成功比例；未结束时只作阶段观察。
  complete: boolean; // 所有计划试次是否结束。
  pass3: number | null; // 每题前三次均成功的比例；不足三次为null。
  known_tokens: number; // 已知用量，可能不完整。
  usage_complete: boolean; // 所有请求是否具有完整用量。
  tokens_per_success: number | null; // 包含失败成本；未知用量或零成功时为空。
  duration_p50: number | null; // 从启动到外部评分的秒数中位数。
  duration_p95: number | null; // 最近秩95分位秒数。
  intervention_rate: number | null; // 进入待用户输入状态的比例，不假设用户已救援。
  model_calls: number; // 含摘要和子调用的总数，父子不重复相加。
  memory?: {
    required_trials: number; // 需要历史依据的试次数。
    all_evidence_exposed: number; // 全部所需证据进入模型输入的次数。
    forbidden_hits: number; // 越界或撤回的证据被检索或输入的次数。
    searches: number; // 历史检索次数。
    consolidation_tokens: number; // 本组承担的实际整理用量。
    total_tokens: number; // 前台与整理的已知总用量。
    total_usage_complete: boolean; // 总用量是否覆盖所有请求。
    consolidation_calls: number; // 独立整理调用数。
    review_seconds: number; // 固定证据规则核对的秒数。
    total_tokens_per_success: number | null; // 摊入整理后的单位成功成本。
  }; // 仅记忆试点使用的指标。
}

/** 只读评测报告；列表省略trials，其余字段取当时固定快照。 */
export interface EvaluationReport {
  id: string; // 实验UUID。
  name: string; // 题集名称。
  created_at: string; // 创建时间，ISO格式。
  state: string; // 不完整、完成、停止或仅预检通过。
  mode: string; // real_model、scripted或preflight，不混用成绩。
  model: string; // 请求模型名称，实际响应见Run轨迹。
  provenance?: string; // 题集来源及适用限制。
  protocol_sha256?: string; // 题集、配置、代码指纹的联合摘要。
  stop_reason?: string | null; // 整批停止原因。
  active_trial?: (EvaluationSlot & { run_available?: boolean }) | null; // 最后登记的未结束试次。
  schedule?: EvaluationSlot[]; // 固定的完整计划，含未启动编号。
  budget?: EvaluationCost & {
    limit_usd_picos: number; reservation_usd_picos: number; // 批次上限和每次发单预留。
    calls_admitted: number; max_calls: number; // 已接纳及最大允许的请求数。
    known_tokens: number; max_known_tokens: number; // 已结算 token 和批次停止线。
    pricing_url: string; // 冻结定价出处。
    approved_holds?: { run_id: string; turn: number; reserved_usd_picos: number; receipt_sha256: string; approval_sha256: string }[]; // 经批准永久保留的未知费用，仍包含在总预留内。
  };
  campaign_phase?: 'calibration' | 'formal' | 'stability'; // 当前阶段，共用整轮累计账本。
  preflight_source_id?: string; // 已通过的独立零模型预检批次。
  recovery?: {
    status: 'ready' | 'paused' | 'stopped'; // 恢复就绪、已暂停或异常停止。
    fresh: number; reused: number; recheck: number; // 本次新验、证据复用和需要重新验证的题数。
    evidence: { case_id: string; report_id: string; certificate_sha256: string }[]; // 指向原始零模型报告的证据索引。
  };
  preflight_reuse?: { status: 'reused'; report_id: string; certificate_sha256: string; adopted: boolean }; // 逐题复用事实，采纳不等于重新执行。
  preflight_error?: { case_id: string; reason: string }; // 无法进入付费阶段的题及原因。
  quality_review?: { verdict: string; reason: string; case_ids: string[] } | null; // 独立复核附注，不修改原评分；有缺陷时不能发布提升结论。
  environments?: Record<string, { repository: string; base_commit: string; image: string; image_id: string; grader: string; source_sha256: string }>; // 每题真实镜像与源码指纹。
  cases?: { id: string; prompt: string }[]; // 预先冻结的原始需求，不含参考解及隐藏测试。
  preflight?: { case_id: string; baseline: EvaluationGrade; reference: EvaluationGrade; baseline_valid: boolean; workspace_verified?: boolean; public_self_test?: PublicSelfTestResult | null }[]; // 付费前评分、工作区及公开自测预检。
  experiment: { split: string; repeats: number; kind?: 'context' | 'memory' }; // 冻结实验轴、划分和重复次数。
  summary: {
    groups: EvaluationGroup[]; // 各组可核对的统计。
    paired: { metric?: 'delivery' | 'resolved'; baseline: string; candidate: string; tasks: number; difference: number; ci95: number[] }[]; // 以任务为单位的配对自助区间。
    memory_pairs?: EvaluationPair[]; // 记忆同题配对。
    patch_pairs?: EvaluationPair[]; // 独立修复的同题赢输平。
    context_pairs?: EvaluationPair[]; // 上下文同题配对。
  };
  consolidations?: {
    id: string; project: string; state: string; // 项目整理阶段及其稳定编号。
    model_calls: number; http_attempts: number; // 逻辑模型与传输次数。
    usage: MemoryUsage; usage_complete: boolean; // 实际用量和完整性。
    duration_seconds: number; review_seconds: number; // 整理、固定规则审阅的秒数。
    changes: unknown[]; // 候选数量，原始正文保存在冻结报告。
    receipt?: { state: string; selected: number[] }; // 已发布或拒绝的事务回执。
    error?: string | null; review_message?: string; // 模型失败或审阅冲突。
    snapshot?: { inputs: MemoryEvidenceRef[] } | null; // 整理实际使用的来源。
  }[];
  active_consolidation?: { project: string; state: string } | null; // 最后保存的整理检查点。
  trials?: EvaluationTrial[]; // 已落盘的逐次执行与评分证据。
}

/** 读取当前数据目录报告列表，无参数、不启动评测，返回可跳转摘要。 */
export function getEvaluations(): Promise<{ items: EvaluationReport[]; unreadable: number }> {
  return request('/api/evaluations');
}

/** 按id读取冻结报告，返回明细；不存在及损坏沿用统一HTTP错误。 */
export function getEvaluation(id: string): Promise<EvaluationReport> {
  return request('/api/evaluations/' + encodeURIComponent(id));
}

/** 按已登记的报告与试次读取原生轨迹；只读，不执行轨迹中的任何命令。 */
export function getEvaluationTrajectory(id: string, runId: string): Promise<{ messages: { role: string; content?: string; tool_calls?: unknown[] }[] }> {
  return request('/api/evaluations/' + encodeURIComponent(id) + '/trajectories/' + encodeURIComponent(runId));
}
