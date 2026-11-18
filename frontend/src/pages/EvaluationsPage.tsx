import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';
import { getEvaluation, getEvaluations, getEvaluationTrajectory, type EvaluationReport, type EvaluationGrade, type EvaluationTrial } from '../evaluation';
import { artifactUrl, errorMessage } from '../api';
import { budgetHandoffTriggerLabels, publicSelfTestReason, publicSelfTestStatusLabels } from '../activity';
import type { PublicSelfTestResult } from '../types';
import './EvaluationsPage.css';

const strategyLabels: Record<string, string> = {
  A: 'A · mini-SWE-agent', B: 'B · 最近五组上下文', C: 'C · 混合压缩',
  hybrid_handoff: 'hybrid · 末轮收尾',
  hybrid_budgeted: 'hybrid · 预算投影与末轮收尾',
  hybrid_budgeted_verified_handoff: 'hybrid · 输入被拒可收尾',
  memory_current: 'A · 当前记忆', memory_history: 'B · 历史检索',
  memory_full: 'C · 完整整理', memory_off: 'D · 无记忆',
}; // 显示冻结协议里的收尾或记忆条件。

/** 基线观察到缺陷即完成复现；候选再次观察到同一缺陷则表示修复未通过。 */
const baselineLabels: Record<string, string> = { failed: '已复现', passed: '未复现', error: '执行异常' };
const candidateLabels: Record<string, string> = { passed: '通过', failed: '问题仍存在', error: '执行异常' };

const stopLabels: Record<string, string> = {
  budget_checkpoint_error: '评测检查点保存失败', internal_error: '运行宿主内部异常', mini_adapter_error: '外部基线适配器异常',
  phase_cost_limit: '当前阶段费用预留不足', phase_model_limit: '当前阶段请求额度用尽',
  trial_cost_limit: '本题费用预留不足', trial_model_limit: '本题请求额度用尽',
  batch_model_limit: '批次调用额度用尽', batch_cost_limit: '下一次请求的费用预留不足',
  batch_known_token_threshold: '达到已知 token 停止线', unknown_usage: '模型用量未确认',
  invalid_case: '评分预检未通过', preflight_environment: '仓库环境预检失败',
  preflight_mismatch: '冻结条件与零模型预检不一致', grading_environment: '独立评分环境异常',
  campaign_paused: '已安全暂停，等待继续',
  cleanup_unconfirmed: '进程或容器清理待确认', cancelled: '批次已取消', evaluation_error: '评测宿主异常',
  self_test_environment: '公开自测环境异常',
  model_authentication: '模型认证失败', model_rate_limited: '供应商限流',
  model_bad_request: '供应商拒绝请求', evaluation_model_changed: '响应模型发生变化',
  evaluation_rate_mismatch: '用量费用超出预留', max_model_input_bytes: '超过模型输入字节上限',
  model_output_truncated: '模型输出截断', max_model_turns: '试次模型轮次用尽',
  run_timeout: '试次执行超时', handoff_tools_disabled: '收尾请求了工具', handoff_invalid_response: '收尾答复格式无效',
}; // 保留具体停止原因，未知代码原样展示。

const gradeLabels: Record<string, string> = { passed: '通过', failed: '未通过', error: '评分异常', not_scored: '未评分' }; // 独立评分状态。
const handoffLabels = { not_triggered: '未触发', started: '已进入', ready: '已答复', partial: '部分完成', invalid: '答复无效', skipped: '未执行' }; // 独立评分通过仍以评分器为准。
const contextLabels: Record<string, string> = { offload: '外置', mask: '遮蔽', summary: '摘要', summary_failed: '摘要失败' }; // 原始上下文动作。

/** 返回 strategy 的用户可读名称；压缩策略沿用原值。 */
function strategyName(strategy: string): string {
  return strategyLabels[strategy] ?? strategy;
}

/** 将比例value转为百分比；null/undefined返回未记录，不生成虚假零值。 */
function percent(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`;
}

/** 将可选 value 四舍五入显示；未知量显示横杠。 */
function count(value: number | null | undefined): string {
  return value == null ? '—' : Math.round(value).toLocaleString();
}

/** 将 picos 皮美元换为跨策略可核对的估算额。 */
function dollars(picos: number, precision = 6): string {
  return '$' + (picos / 1e12).toFixed(precision);
}

/** 按 trial 回执返回单题对照中的执行和评分描述。 */
function trialLabel(trial: EvaluationTrial): string {
  if (trial.interrupted) return '未结束';
  return (trial.success ? '交付通过' : '交付未通过') + ' · 独立评分' + (gradeLabels[trial.grade.status] ?? trial.grade.status);
}

/** 按grade显示官方缺陷与回归断言名；缺少官方结果时只呈现状态及日志。 */
function Grade({ grade }: { grade: EvaluationGrade }) {
  return <>
    {Object.entries(grade.official?.tests_status ?? {}).filter(/* 只展示本轮实际计分的两类测试。 */ ([kind]) => ['FAIL_TO_PASS', 'PASS_TO_PASS'].includes(kind)).map(/* 保留失败项名称便于定位原因。 */ ([kind, tests]) => <details key={kind}>
      <summary>{kind === 'FAIL_TO_PASS' ? '缺陷修复测试' : '回归测试'}：{tests.success.length} 通过 / {tests.failure.length} 未通过</summary>
      <pre>{tests.failure.map(/* 明示失败而非只给汇总数。 */ name => 'FAIL ' + name).concat(tests.success.map(/* 成功列表同样可追溯。 */ name => 'PASS ' + name)).join('\n')}</pre>
    </details>)}
    {(grade.stdout || grade.stderr || grade.reason) && <details>
      <summary>评分输出预览</summary>
      <pre>{grade.stdout}{grade.stderr}{grade.reason}</pre>
    </details>}
  </>;
}

/** 展示当前候选的 check 回执；有 runId 时经现有 Run 产物入口下载实际日志。 */
function PublicSelfTest({ check, runId }: { check: PublicSelfTestResult; runId?: string }) {
  const status = publicSelfTestStatusLabels[check.status] ?? check.status;
  const reason = publicSelfTestReason(check.reason);
  const logs = runId ? check.artifact_refs ?? [] : [];
  return <>
    <p>公开自测 {status}
      {check.tests_run != null && ' · 实际执行 ' + count(check.tests_run) + ' 项'}
      {check.elapsed_ms != null && ' · ' + (check.elapsed_ms / 1000).toFixed(2) + ' 秒'}
      {check.status !== 'passed' && reason && reason !== status && ' · ' + reason}
    </p>
    {(check.selector || check.candidate_sha256 || logs.length > 0) && <details>
      <summary>公开自测证据</summary>
      {check.selector && <p><code>{check.selector}</code></p>}
      {check.candidate_sha256 && <p>候选 SHA-256 <code>{check.candidate_sha256}</code></p>}
      {runId && logs.map(/* 每份产物均指向本试次的真实 Run 文件。 */ ref =>
        <p key={ref.location + ':' + ref.path}><a href={artifactUrl(runId, ref)}>
          {ref.kind === 'stdout' ? '下载自测标准输出' : ref.kind === 'stderr' ? '下载自测错误输出' : '下载自测日志'}
        </a></p>)}
    </details>}
  </>;
}

/** 按需查看固定原生轨迹；enabled 仅控制只读获取，不触发评测。 */
function NativeTrajectory({ reportId, runId }: { reportId: string; runId: string }) {
  const [opened, setOpened] = useState(false); // 用户主动打开后才请求完整轨迹。
  const query = useQuery({ queryKey: ['evaluation-trajectory', reportId, runId], enabled: opened,
    queryFn: /* 两个编号由冻结报告提供。 */ () => getEvaluationTrajectory(reportId, runId) });
  return <details onToggle={/* 原生details的打开状态决定只读查询。 */ event => setOpened(event.currentTarget.open)}>
    <summary>查看 mini-SWE-agent 原生轨迹</summary>
    {query.isLoading && <p>正在读取…</p>}
    {query.error && <p role="alert">{errorMessage(query.error)}</p>}
    {query.data?.messages.map(/* 文本按React转义显示，工具命令只读。 */ (message, index) =>
      <div key={index}><strong>{message.role}</strong><pre>{message.content}{message.tool_calls ? '\n' + JSON.stringify(message.tool_calls, null, 2) : ''}</pre></div>)}
    <a href={'/api/evaluations/' + reportId + '/trajectories/' + runId}>下载原生轨迹 JSON</a>
  </details>;
}

/** 展示report冻结统计及独立评分，返回可访问原始Run的只读界面。 */
function Detail({ report }: { report: EvaluationReport }) {
  const memory = report.experiment.kind === 'memory'; // 记忆实验按真实证据和整理成本展示。
  const trials = report.trials ?? []; // 仅含已保存的试次回执。
  const trialsByRun = new Map(trials.map(/* 按预分配编号将计划与实际证据关联。 */ trial => [trial.run_id, trial]));
  const finished = trials.filter(/* 中断回执仍可查看，但不计为已结束。 */ trial => !trial.interrupted).length;
  const slots = report.schedule ?? []; // 计划需要保留尚未启动项。
  const strategies = report.summary.groups.map(/* 保持冻结的基线和候选顺序。 */ group => group.strategy);
  const hasIndependentTotals = report.summary.groups.some(/* 原报告缺少独立汇总时只展示已有统计。 */ group => group.scored != null) && !memory;
  const budget = report.budget; // 仅费用受控批次有逐请求账本。
  const approvedHold = budget?.approved_holds?.reduce(/* 汇总已批准保留的未知金额，不从总预留中扣除。 */
    (total, item) => total + item.reserved_usd_picos, 0) ?? 0;
  return <>
    <h1>{report.name}</h1>
    {report.campaign_phase && <p>{({ calibration: '开发校准', formal: '正式对照', stability: '稳定性验证' })[report.campaign_phase]} · 费用为整轮累计</p>}
    <p>{report.model} · {report.experiment.split === 'test' ? '测试集' : '开发集'} · 每题 {report.experiment.repeats} 次</p>
    <p>{report.mode === 'real_model' ? '真实模型' : report.mode === 'preflight' ? '仅评分预检' : '脚本模型 · 仅验证工程链路'} · {report.state === 'completed' ? '全部结束' : report.state === 'preflight_passed' ? '预检通过' : '尚未完整结束'}</p>
    {report.stop_reason && <p role="status">停止原因：{stopLabels[report.stop_reason] ?? report.stop_reason}</p>}
    {report.recovery && <section aria-label="恢复证据">
      <p>环境预检：新验 {report.recovery.fresh} 题 · 复用 {report.recovery.reused} 题 · 待复验 {report.recovery.recheck} 题</p>
      <details><summary>核对原始预检证据</summary>{report.recovery.evidence.map(/* 原报告保留原时间和代码，不伪装成本次新验。 */ item =>
        <p key={item.case_id}><Link to={'/evaluations/' + item.report_id}>{item.case_id}</Link> · 证书 {item.certificate_sha256.slice(0, 12)}</p>)}</details>
    </section>}
    {report.preflight_reuse && <p>本题复用已验证的环境证据{report.preflight_reuse.adopted ? '（经审查采纳）' : ''}</p>}
    {report.preflight_error && <p role="alert">{report.preflight_error.case_id} · {report.preflight_error.reason}</p>}
    <p>{report.provenance}</p>
    {report.quality_review && <p role="alert">对照不可采信：{report.quality_review.reason} 以下保留原始评分，不用于策略优劣结论。</p>}
    {report.active_trial && <p role="status">未结束记录：{report.active_trial.case_id} · {strategyName(report.active_trial.strategy)}
      {report.active_trial.run_available && <> · <Link to={'/runs/' + report.active_trial.run_id}>查看未结束 Run</Link></>}
    </p>}
    {report.active_consolidation && <p role="status">整理检查点：{report.active_consolidation.project} · {report.active_consolidation.state}</p>}
    {budget && <dl className="evaluation-budget" aria-label="批次预算">
      <div><dt>试次</dt><dd>{finished} / {slots.length} 已结束</dd></div>
      <div><dt>模型请求</dt><dd>{budget.calls_admitted} / {budget.max_calls}</dd></div>
      <div><dt>已知用量</dt><dd>{budget.known_tokens.toLocaleString()} / {budget.max_known_tokens.toLocaleString()} token</dd></div>
      <div><dt>费用估算</dt><dd>{dollars(budget.known_usd_picos)} / {dollars(budget.limit_usd_picos, 2)}</dd></div>
      {budget.reserved_usd_picos > 0 && <div><dt>未结算预留</dt><dd>{dollars(budget.reserved_usd_picos, 2)}{approvedHold > 0 && `（其中获批保留 ${dollars(approvedHold, 2)}）`}</dd></div>}
    </dl>}
    <div className="evaluation-table"><table>
      <thead><tr><th>策略</th><th>结束/计划</th><th>完整交付</th><th>成功率</th>{hasIndependentTotals && <th>独立评分通过</th>}{memory ? <><th>证据齐全</th><th>禁用证据命中</th><th>前台 / 整理 token</th></> : report.experiment.repeats >= 3 && <th>三次全成功</th>}<th>每成功任务 token</th><th>耗时 p50 / p95</th><th>请求人工输入</th><th>模型调用</th></tr></thead>
      <tbody>{report.summary.groups.map(/* 每组仅展示自身分母及完整性。 */ group => <tr key={group.strategy}>
        <td>{strategyName(group.strategy)}</td><td>{group.finished}/{group.planned}</td><td>{group.successes}</td>
        <td>{percent(group.success_rate)}{!group.complete && '（阶段值）'}</td>
        {hasIndependentTotals && <td>{group.scored ? `${group.patch_passes}/${group.scored}` : '—'}</td>}
        {group.memory ? <><td>{group.memory.all_evidence_exposed}/{group.memory.required_trials}</td>
          <td>{group.memory.forbidden_hits}</td>
          <td>{group.known_tokens.toLocaleString()} / {group.memory.consolidation_tokens.toLocaleString()}</td></> : report.experiment.repeats >= 3 && <td>{percent(group.pass3)}</td>}
        <td>{count(group.memory ? group.memory.total_tokens_per_success : group.tokens_per_success)}{!(group.memory?.total_usage_complete ?? group.usage_complete) && '（用量不完整）'}</td>
        <td>{group.duration_p50?.toFixed(2) ?? '—'} / {group.duration_p95?.toFixed(2) ?? '—'} 秒</td>
        <td>{percent(group.intervention_rate)}</td><td>{group.model_calls}{group.memory?.consolidation_calls ? ' + ' + group.memory.consolidation_calls : ''}</td>
      </tr>)}</tbody>
    </table></div>
    {!report.quality_review && report.summary.paired.filter(/* 单题无法估计任务之间的变异。 */ pair => pair.tasks >= 2).map(/* 区间只对完整且未标记题目缺陷的配对题集发布。 */ pair => <p key={pair.baseline + pair.candidate + (pair.metric ?? "delivery")}>
      {pair.metric === 'resolved' ? '独立修复 · ' : '完整交付 · '}{pair.candidate} 相对 {pair.baseline}：{(pair.difference * 100).toFixed(1)} 个百分点；95%任务重采样区间 [{pair.ci95.map(/* 区间端点转百分点。 */ n => (n * 100).toFixed(1)).join(', ')}]，{pair.tasks} 题。
    </p>)}
    {!report.quality_review && report.summary.memory_pairs?.map(/* 直接展示同题配对与缺失分母。 */ (pair) => <details key={pair.baseline + pair.candidate + (pair.metric ?? "delivery")}>
      <summary>{strategyName(pair.candidate)} 相对 {strategyName(pair.baseline)} · 胜 {pair.wins.length} / 负 {pair.losses.length} / 平 {pair.ties.length} · {pair.valid_pairs}/{pair.planned_pairs} 有效配对</summary>
      <p>胜：{pair.wins.join('、') || '—'}　负：{pair.losses.join('、') || '—'}</p>
    </details>)}
    {!report.quality_review && report.mode !== 'preflight' && [...(report.summary.context_pairs ?? []), ...(report.summary.patch_pairs ?? [])].map(/* 每题一组，保留部分批次中的有效分母。 */ pair =>
      <p key={pair.baseline + pair.candidate + (pair.metric ?? "delivery")}>{pair.metric === 'resolved' ? '独立修复 · ' : '完整交付 · '}{pair.candidate} 相对 {pair.baseline} · 胜 {pair.wins.length} / 负 {pair.losses.length} / 平 {pair.ties.length} · {pair.valid_pairs}/{pair.planned_pairs} 有效配对</p>)}
    {!memory && slots.length > 0 && report.mode !== 'preflight' && <div className="evaluation-table">
      <table aria-label="同题对照">
        <thead><tr><th>题目</th>{strategies.map(/* 使用冻结策略名。 */ strategy => <th key={strategy}>{strategyName(strategy)}</th>)}</tr></thead>
        <tbody>{report.cases?.map(/* 同题同一行，未运行的试次也显示。 */ item => <tr key={item.id}>
          <th scope="row">{item.id}</th>{strategies.map(/* 每格按计划匹配该策略的结果。 */ strategy => <td key={strategy}>
            {slots.filter(/* 仅本题与本策略。 */ slot => slot.case_id === item.id && slot.strategy === strategy).map(/* 结果锚点定位同一个Run。 */ slot => {
              const trial = trialsByRun.get(slot.run_id);
              if (trial) return <div key={slot.run_id}><a href={'#trial-' + slot.run_id}>{trialLabel(trial)}</a></div>;
              return <div key={slot.run_id}>{slot.run_id === report.active_trial?.run_id ? '已启动 · 未结束' : '未启动'}</div>;
            })}
          </td>)}
        </tr>)}</tbody>
      </table>
    </div>}
    {!!report.consolidations?.length && <><h2>整理用量</h2>{report.consolidations.map(/* 每个项目的整理单独计费及核对。 */ (item) => <details key={item.id}>
      <summary>{item.project} · {item.usage_complete ? '' : '至少 '}{(item.usage.total_tokens ?? 0).toLocaleString()} token · {item.duration_seconds.toFixed(2)} 秒</summary>
      <p>{item.model_calls} 次模型调用 / {item.http_attempts} 次 HTTP · {item.receipt?.selected.length ?? 0}/{item.changes.length} 项发布 · 规则审阅 {item.review_seconds.toFixed(2)} 秒</p>
      {(item.error || item.review_message) && <p role="status">{item.error || item.review_message}</p>}
      {item.snapshot?.inputs.map(/* 可回看整理时实际输入的学习 Run。 */ (ref) =>
        <p key={ref.id}><Link to={'/runs/' + ref.run_id}>{ref.run_id.slice(0, 8)} · 事件 {ref.seq}</Link></p>)}
    </details>)}</>}
    {!!report.preflight?.length && <><h2>环境预检</h2>
      {report.preflight_source_id && <p><Link to={'/evaluations/' + report.preflight_source_id}>查看零模型预检报告</Link></p>}
      {report.preflight.map(/* 预检失败也可直接在前端核对，不被空试次隐藏。 */ item => <details key={item.case_id}>
      <summary>{item.case_id} · {item.baseline_valid && item.reference.status === 'passed' && (item.public_self_test == null || item.public_self_test.status === 'passed') ? '预检通过' : '预检未通过'}</summary>
      {report.environments?.[item.case_id] && <p>{report.environments[item.case_id].repository} · {report.environments[item.case_id].grader}<br />基线：<code>{report.environments[item.case_id].base_commit}</code><br />镜像：<code>{report.environments[item.case_id].image_id}</code></p>}
      <p>缺陷复现：{item.baseline_valid ? '有效' : '无效'}；参考修复：{gradeLabels[item.reference.status] ?? item.reference.status}
        {item.workspace_verified && '；工作区解包及 Git 基线通过'}</p>
      {item.public_self_test && <PublicSelfTest check={item.public_self_test} />}
      {report.cases?.find(/* 按稳定题号匹配需求，不从当前题库回填历史。 */ entry => entry.id === item.case_id)?.prompt && <details><summary>原始需求</summary><pre>{report.cases.find(/* 保留完整原文。 */ entry => entry.id === item.case_id)?.prompt}</pre></details>}
      <details><summary>基线证据</summary><Grade grade={item.baseline} /></details>
      <details><summary>参考修复证据</summary><Grade grade={item.reference} /></details>
    </details>)}</>}
    <h2>逐次证据</h2>
    {trials.map(/* 评分和执行状态分开，原始轨迹沿用现有详情。 */ trial => <details key={trial.run_id} id={'trial-' + trial.run_id}>
      <summary>{trial.case_id} · {strategyName(trial.strategy)} · 第 {trial.repeat + 1} 次 · {trialLabel(trial)}</summary>
      <p>Run <code>{trial.run_id}</code></p>
      <p>执行：{trial.execution_status}；独立评分：{trial.grade.status}{trial.stop_reason && '；' + (stopLabels[trial.stop_reason] ?? trial.stop_reason)}</p>
      {trial.entry && <section aria-label="仓库入口验证">
        {trial.entry.entry_kind && <p>验证范围 · {({http: 'HTTP 请求', admin: '后台页面请求', cli: '命令行', api: '公开 API 调用', self: '自选业务断言'})[trial.entry.entry_kind]}</p>}
        {trial.entry.next_action && <p>{trial.entry.next_action}</p>}
        {trial.entry.coverage_review && <p>{trial.entry.coverage_review}</p>}
        {!!trial.entry.progress?.length && <details><summary>工作树进展</summary>
          <ul>{trial.entry.progress.map(item => <li key={item.batch}>第 {item.batch} 批 · {({
            source: '已有候选文件', auxiliary: '仅有辅助文件', empty: '没有改动', unavailable: '未取得差异',
          } as Record<string, string>)[item.state]} {item.paths?.join('、')}</li>)}</ul></details>}
        <p>Agent 自选用例 · {trial.entry.status === 'verified' ? '复验通过' : trial.entry.status === 'candidate_changed' ? '验证后补丁已变化' : '未验证'}</p>
        {([['基线复现', trial.entry.baseline], ['候选复验', trial.entry.candidate]] as const).map(([label, fact]) =>
          <details key={label}><summary>{label} · {fact == null ? '未运行'
            : (label === '基线复现' ? baselineLabels : candidateLabels)[fact.status] ?? fact.status}</summary>
            {fact && <pre>{fact.command}{'\n'}{fact.output}</pre>}</details>)}
        <p>交付文件 · {trial.entry.files?.join('、') || '无'}</p>
        {!!trial.entry.omitted?.length && <p>未选文件 · {trial.entry.omitted.join('、')}</p>}
        {trial.entry.submitted_sha256 && <code>{trial.entry.submitted_sha256}</code>}
      </section>}
      {trial.host_entry && <details><summary>独立入口复核 · {candidateLabels[trial.host_entry.status]}</summary>
        <pre>{trial.host_entry.output}</pre></details>}
      {trial.grading_recovery && <p>沿用原模型产物补评分 · 新增模型请求 {trial.grading_recovery.external_model_requests} 次 · <Link to={'/evaluations/' + trial.grading_recovery.original_report_id}>查看原评分异常</Link></p>}
      {trial.failure_type && <p>失败归因：{({ execution: 'Agent执行', grading_environment: '评分环境', test_assertion: '测试断言', patch_apply: '补丁无法应用' } as Record<string, string>)[trial.failure_type] ?? trial.failure_type}</p>}
      <p>响应模型：{trial.response_model ?? '未记录'} · {trial.usage_complete ? '' : '至少 '}{trial.known_tokens.toLocaleString()} token
        {!memory && <> · 上下文变换：{trial.compression_applied == null ? '未记录' : trial.compression_applied ? '已触发' : '未触发'}</>}</p>
      {trial.model_calls != null && <p>{trial.model_calls} 次模型调用 / {trial.http_attempts ?? '—'} 次 HTTP
        {trial.duration_seconds != null && <> · {trial.duration_seconds.toFixed(1)} 秒</>}
        {trial.cost && <> · 峰时费用 {dollars(trial.cost.known_usd_picos)}{trial.cost.reserved_usd_picos > 0 && ' · 未结算 ' + dollars(trial.cost.reserved_usd_picos, 2)}</>}</p>}
      {!memory && trial.execution_calls != null && <p>执行 {trial.execution_calls} 次 · 摘要 {trial.summary_calls ?? 0} 次
        {trial.handoff_trigger == null || trial.handoff_input_bytes == null ? ' · 最后放行 ' + count(trial.last_admitted_input_bytes) + ' B' : ''}
        {trial.handoff_trigger == null && <> · 被拒 {count(trial.last_rejected_input_bytes)} B
          {trial.handoff_status != null && <> · 末轮收尾 {handoffLabels[trial.handoff_status]}</>}</>}</p>}
      {trial.handoff_trigger && <p>收尾 {trial.handoff_status == null ? '未记录' : handoffLabels[trial.handoff_status]} · {budgetHandoffTriggerLabels[trial.handoff_trigger]}
        {trial.last_rejected_input_bytes != null && ' · 普通输入被拒 ' + count(trial.last_rejected_input_bytes) + ' B'}
        {trial.handoff_input_bytes != null ? ' · 紧凑输入放行 ' + count(trial.handoff_input_bytes) + ' B'
          : trial.handoff_checked_bytes != null && ' · 紧凑输入未发送 ' + count(trial.handoff_checked_bytes) + ' B'}
        {trial.handoff_skip_reason && ' · ' + publicSelfTestReason(trial.handoff_skip_reason)}
      </p>}
      {trial.public_self_test && <PublicSelfTest check={trial.public_self_test} runId={trial.run_available === false ? undefined : trial.run_id} />}
      {trial.search && <p>搜索 {trial.search.started} 次 · {(trial.search.total_ms / 1000).toFixed(2)} 秒
        {trial.search.local_timeouts > 0 && ' · 单次限时 ' + trial.search.local_timeouts + ' 次'}
        {trial.search.run_timeouts > 0 && ' · Run 截止 ' + trial.search.run_timeouts + ' 次'}</p>}
      {(trial.first_write !== undefined || trial.patch_bytes != null) && <p>
        {trial.first_write
          ? <>首次写入 <code>{trial.first_write.path}</code> · {trial.first_write.model_turn != null && '第 ' + trial.first_write.model_turn + ' 轮 · '}
              {(trial.first_write.elapsed_ms / 1000).toFixed(2)} 秒</>
          : '无成功文件写入回执'}
        {trial.patch_bytes != null && ' · 补丁 ' + count(trial.patch_bytes) + ' B'}</p>}
      {!!trial.context_actions?.length && <p>上下文动作 {Array.from(new Set(trial.context_actions)).map(/* 同一动作仅展示一次。 */ action => contextLabels[action] ?? action).join('、')}
        {trial.execution_calls == null && trial.summary_calls != null && <> · {trial.summary_calls} 次摘要尝试</>}
        {!!trial.budget_projection_actions && <> · 预算投影 {trial.budget_projection_actions} 次</>}</p>}
      {trial.memory && <div>
        <p>历史检索 {trial.memory.searches} 次 · 所需证据 {trial.memory.exposed.length}/{trial.memory.required.length} 进入输入 · 禁用证据 {trial.memory.forbidden_hits.length} 条</p>
        {trial.memory.evidence.map(/* 证据命中可跳回真实来源。 */ (ref) =>
          <p key={ref.id}><Link to={'/runs/' + ref.run_id}>来源 {ref.run_id.slice(0, 8)} · 事件 {ref.seq}</Link></p>)}
      </div>}
      {trial.agent_seconds != null && <p>环境准备 {trial.preparation_seconds?.toFixed(1) ?? '—'} 秒 · Agent {trial.agent_seconds.toFixed(1)} 秒 · 独立评分 {trial.grading_seconds?.toFixed(1) ?? '—'} 秒</p>}
      {trial.trajectory_available && <NativeTrajectory reportId={report.id} runId={trial.run_id} />}
      {trial.run_available !== false && <Link to={'/runs/' + trial.run_id}>查看运行轨迹</Link>}
      {trial.source_url && <> · <a href={trial.source_url} target="_blank" rel="noreferrer">来源 PR</a></>}
      {trial.patch_available && <> · <a href={'/api/evaluations/' + report.id + '/patches/' + trial.run_id}>下载补丁</a></>}
      {trial.grade_artifacts?.score && <> · <a href={'/api/evaluations/' + report.id + '/grades/' + trial.run_id + '/score'}>下载评分 JSON</a></>}
      {trial.grade_artifacts?.log && <> · <a href={'/api/evaluations/' + report.id + '/grades/' + trial.run_id + '/log'}>下载测试日志</a></>}
      <Grade grade={trial.grade} />
    </details>)}
    {report.summary.groups.some(group => group.cost_complete != null) && <details><summary>费用、分层与运行机制</summary>
      {report.summary.groups.map(/* 每组分别核对成功分母及环境失败。 */ group => <div key={group.strategy}>
        <h3>{strategyName(group.strategy)}</h3>
        <p>每个修复成功任务：{group.cost_per_resolved_usd_picos == null ? '—' : dollars(group.cost_per_resolved_usd_picos)} · 每个完整交付：{group.cost_per_delivery_usd_picos == null ? '—' : dollars(group.cost_per_delivery_usd_picos)} · 缓存命中 {percent(group.cache_hit_ratio)}</p>
        <p>摘要 {group.summary_calls ?? 0} 次 · 发生压缩 {group.compression_trials ?? 0} 次 · 输入超限 {group.context_limit_failures ?? 0} 次</p>
        <p>Agent p50 / p95：{group.agent_seconds_p50?.toFixed(1) ?? '—'} / {group.agent_seconds_p95?.toFixed(1) ?? '—'} 秒 · 评分 p50 / p95：{group.grading_seconds_p50?.toFixed(1) ?? '—'} / {group.grading_seconds_p95?.toFixed(1) ?? '—'} 秒</p>
        {Object.entries(group.strata ?? {}).map(/* 仓库和难度采用各自分母。 */ ([axis, rows]) => <table key={axis}><caption>{axis === 'project' ? '按仓库' : '按难度'}</caption><thead><tr><th>分层</th><th>修复/试次</th><th>交付/试次</th></tr></thead><tbody>{rows.map(row => <tr key={row.name}><td>{row.name}</td><td>{row.resolved}/{row.trials}</td><td>{row.deliveries}/{row.trials}</td></tr>)}</tbody></table>)}
        {Object.entries(group.failures ?? {}).map(([reason, amount]) => <p key={reason}>{stopLabels[reason] ?? reason}：{amount}</p>)}
      </div>)}
    </details>}
    <details><summary>冻结协议指纹</summary><code>{report.protocol_sha256}</code></details>
  </>;
}

/** 从URL选择报告或列表，返回只读页面；刷新不会启动模型或恢复中断。 */
export function EvaluationsPage() {
  const { evaluationId } = useParams(); // 当前报告编号，缺省为列表。
  const list = useQuery({ queryKey: ['evaluations'], queryFn: getEvaluations, enabled: !evaluationId }); // 摘要查询。
  const detail = useQuery({ queryKey: ['evaluation', evaluationId], queryFn: /* 编号仅在详情路由存在。 */ () => getEvaluation(evaluationId!), enabled: !!evaluationId }); // 固定报告。
  const error = list.error || detail.error; // 读取失败不显示虚构空报告。
  return <section className="evaluations">
    {error && <p role="alert">{errorMessage(error)}</p>}
    {(list.isLoading || detail.isLoading) && <p role="status">正在读取评测…</p>}
    {evaluationId && detail.data && <Detail report={detail.data} />}
    {!evaluationId && <><h1>评测</h1>
      {list.data?.unreadable ? <p role="alert">{list.data.unreadable} 份记录无法读取</p> : null}
      {list.data?.items.length === 0 && <p>尚无评测报告。使用 harness eval 执行后，可在这里回看结果。</p>}
      {list.data?.items.map(/* 列表保留模型和划分，防止把开发分数误作测试成绩。 */ report => <article key={report.id}>
        <Link to={'/evaluations/' + report.id}>{report.name}</Link>
        <p>{report.model} · {report.experiment.split} · {new Date(report.created_at).toLocaleString()}</p>
        {report.quality_review && <p>题目存在缺陷 · 保留原始记录</p>}
      </article>)}
    </>}
  </section>;
}
