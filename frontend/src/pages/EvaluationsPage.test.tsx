import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router';
import { it, expect, vi } from 'vitest';
import { EvaluationsPage } from './EvaluationsPage';
import { getEvaluation, getEvaluations, getEvaluationTrajectory } from '../evaluation';

vi.mock('../evaluation', /* 替换只读HTTP，避免页面测试调用模型。 */ () => ({ getEvaluation: vi.fn(), getEvaluations: vi.fn(), getEvaluationTrajectory: vi.fn() }));

/** 在独立查询缓存中挂载path；返回页面，避免不同试次共享查询结果。 */
function show(path: string) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><Routes><Route path="/evaluations/:evaluationId?" element={<EvaluationsPage />} /></Routes></MemoryRouter>
  </QueryClientProvider>);
}

it('补评分保留原运行与请求数，并能回看原评分异常', /* 恢复成绩不伪装成一次新的模型试次。 */ async () => {
  vi.mocked(getEvaluation).mockResolvedValue({ id: 'restored', name: '补评分恢复', created_at: '2026-11-17T00:00:00Z',
    state: 'incomplete', mode: 'real_model', model: 'deepseek-flash', experiment: { split: 'test', repeats: 1 },
    summary: { groups: [], paired: [] }, trials: [{ run_id: 'original-run', case_id: 'repo-1', strategy: 'A', repeat: 0,
      success: true, execution_status: 'completed', stop_reason: null, grade: { status: 'passed' },
      model_calls: 17, http_attempts: 17, known_tokens: 302840, usage_complete: true,
      grading_recovery: { original_report_id: 'original-error', receipt_id: 'receipt', external_model_requests: 0, grading_seconds: 120 } }] });
  show('/evaluations/restored');
  await screen.findByText('补评分恢复');
  fireEvent.click(screen.getByText(/repo-1 · A/));
  expect(screen.getByText(/沿用原模型产物补评分 · 新增模型请求 0 次/)).toBeInTheDocument();
  expect(screen.getByText(/17 次模型调用 \/ 17 次 HTTP/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '查看原评分异常' })).toHaveAttribute('href', '/evaluations/original-error');
});

it('显示复用题数、原始证据和安全暂停，不把采纳显示为新验', /* 只读报告状态与后端恢复字段一致。 */ async () => {
  vi.mocked(getEvaluation).mockResolvedValue({ id: 'recovery', name: '恢复评测', created_at: '2026-11-17T00:00:00Z',
    state: 'incomplete', mode: 'real_model', model: 'deepseek-flash', stop_reason: 'campaign_paused',
    experiment: { split: 'test', repeats: 1 }, summary: { groups: [], paired: [] },
    recovery: { status: 'paused', fresh: 0, reused: 50, recheck: 0,
      evidence: [{ case_id: 'repo-1', report_id: 'original-proof', certificate_sha256: 'abcdef123456789' }] } });
  show('/evaluations/recovery');
  expect(await screen.findByText(/新验 0 题 · 复用 50 题 · 待复验 0 题/)).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('已安全暂停，等待继续');
  fireEvent.click(screen.getByText('核对原始预检证据'));
  expect(screen.getByRole('link', { name: 'repo-1' })).toHaveAttribute('href', '/evaluations/original-proof');
});

it('区分阶段值、未知成本、脚本模型与实际评分，并链接原始Run', /* 页面应如实呈现固定的不完整实验。 */ async () => {
  vi.mocked(getEvaluation).mockResolvedValue({ id: 'batch', name: '冻结实验', created_at: '2026-11-12T00:00:00Z', state: 'stopped', mode: 'scripted', model: 'fixture',
    experiment: { split: 'dev', repeats: 1 }, summary: { paired: [{ baseline: 'off', candidate: 'hybrid', tasks: 10, difference: .1, ci95: [0, .3] }], groups: [{ strategy: 'off', planned: 2, finished: 1, successes: 0, success_rate: 0, complete: false, pass3: null, known_tokens: 10, usage_complete: false, tokens_per_success: null, duration_p50: 1, duration_p95: 1, intervention_rate: 0, model_calls: 1 }] },
    quality_review: { verdict: 'invalid_comparison', reason: '需求未指定返回结构', case_ids: ['case-1'] },
    trials: [{ run_id: 'run-1', case_id: 'case-1', strategy: 'off', repeat: 0, success: false, execution_status: 'completed', stop_reason: null, grade: { status: 'failed', stderr: 'expected current rule' }, known_tokens: 10, usage_complete: false }] });
  show('/evaluations/batch');
  expect(await screen.findByText('冻结实验')).toBeInTheDocument();
  expect(screen.getByText(/仅验证工程链路/)).toBeInTheDocument();
  expect(screen.getByText(/阶段值/)).toBeInTheDocument();
  expect(screen.getByText(/用量不完整/)).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('对照不可采信');
  expect(screen.queryByText(/hybrid 相对 off/)).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: '查看运行轨迹' })).toHaveAttribute('href', '/runs/run-1');
});

it('空列表明确显示未评测，不编造零分', /* 没有报告时只展示入口说明。 */ async () => {
  vi.mocked(getEvaluations).mockResolvedValue({ items: [], unreadable: 0 });
  show('/evaluations');
  expect(await screen.findByText(/尚无评测报告/)).toBeInTheDocument();
});

it('展示仓库预检、镜像、失败测试及可核对的补丁入口', /* 环境和模型失败分别呈现，避免把坏环境算成模型能力。 */ async () => {
  vi.mocked(getEvaluation).mockResolvedValue({ id: 'repo-batch', name: '仓库试点', created_at: '2026-11-12T00:00:00Z', state: 'completed', mode: 'real_model', model: 'deepseek-flash',
    experiment: { split: 'dev', repeats: 1 }, summary: { groups: [], paired: [] },
    environments: { 'repo-1': { repository: 'org/repo', base_commit: 'base-sha', image: 'official', image_id: 'sha256:immutable', grader: 'swebench==4.1.0', source_sha256: 'source-sha' } },
    preflight: [{ case_id: 'repo-1', baseline_valid: true, baseline: { status: 'failed' }, reference: { status: 'passed' } }],
    trials: [{ run_id: 'run-1', case_id: 'repo-1', strategy: 'hybrid_handoff', repeat: 0, success: false, execution_status: 'completed', stop_reason: null,
      host_entry: { status: 'failed', output: 'GET /admin/ 200，后台只读字段仍转义' },
      entry: { status: 'candidate_changed', entry_kind: 'admin', coverage_review: '本次只核对了可编辑字段',
        progress: [{batch: 8, state: 'empty', paths: []}],
        files: ['django/forms/fields.py'], omitted: ['notes.txt'],
        baseline: { status: 'failed', command: 'python /probe/repro.py', output: 'readonly field escaped' } },
      execution_calls: 28, summary_calls: 2, last_admitted_input_bytes: 50000, last_rejected_input_bytes: null, handoff_status: 'ready',
      grade: { status: 'failed', official: { resolved: false, tests_status: { FAIL_TO_PASS: { success: [], failure: ['test_bug'] }, PASS_TO_PASS: { success: ['test_existing'], failure: [] } } } },
      known_tokens: 100, usage_complete: true, failure_type: 'test_assertion', source_url: 'https://github.com/org/repo/pull/1', patch_available: true }] });
  show('/evaluations/repo-batch');
  expect(await screen.findByText('仓库试点')).toBeInTheDocument();
  fireEvent.click(screen.getByText('基线复现 · 已复现'));
  expect(screen.getByText(/readonly field escaped/)).toBeInTheDocument();
  expect(screen.getByText('Agent 自选用例 · 验证后补丁已变化')).toBeInTheDocument();
  expect(screen.getByText('验证范围 · 后台页面请求')).toBeInTheDocument();
  expect(screen.getByText('本次只核对了可编辑字段')).toBeInTheDocument();
  fireEvent.click(screen.getByText('工作树进展'));
  expect(screen.getByText(/第 8 批 · 没有改动/)).toBeInTheDocument();
  fireEvent.click(screen.getByText('独立入口复核 · 问题仍存在'));
  expect(screen.getByText(/后台只读字段仍转义/)).toBeInTheDocument();
  expect(screen.getByText('候选复验 · 未运行')).toBeInTheDocument();
  expect(screen.getByText(/未选文件 · notes.txt/)).toBeInTheDocument();
  expect(screen.getByText(/repo-1 · 预检通过/)).toBeInTheDocument();
  expect(screen.getByText('sha256:immutable')).toBeInTheDocument();
  expect(screen.getByText('FAIL test_bug')).toBeInTheDocument();
  expect(screen.getByText('失败归因：测试断言')).toBeInTheDocument();
  expect(screen.getByText(/执行 28 次 · 摘要 2 次/)).toHaveTextContent(/最后放行 .* B · 被拒 — B · 末轮收尾 已答复/);
  expect(screen.getByRole('link', { name: '下载补丁' })).toHaveAttribute('href', '/api/evaluations/repo-batch/patches/run-1');
});

it('保留同轮原拒绝和紧凑收尾，公开自测日志独立于官方评分', /* 通过的自测不应覆盖独立评分不通过。 */ async () => {
  vi.mocked(getEvaluation).mockResolvedValue({
    id: 'handoff', name: '收尾对照', created_at: '2026-11-12T00:00:00Z', state: 'completed', mode: 'scripted', model: 'fixture',
    experiment: { split: 'dev', repeats: 1 }, summary: { groups: [], paired: [] },
    preflight: [{ case_id: 'pytest-5262', baseline_valid: true, baseline: { status: 'failed' }, reference: { status: 'passed' },
      public_self_test: { status: 'error', reason: 'self_test_environment' } }],
    trials: [{
      run_id: 'run-1', case_id: 'pytest-5262', strategy: 'hybrid_budgeted_verified_handoff', repeat: 0,
      success: false, execution_status: 'completed', stop_reason: null, grade: { status: 'failed' },
      known_tokens: 12, usage_complete: true, execution_calls: 21, summary_calls: 2,
      last_admitted_input_bytes: 4402, last_rejected_input_bytes: 65642,
      handoff_status: 'ready', handoff_trigger: 'input_rejected', handoff_input_bytes: 4402,
      public_self_test: {
        status: 'passed', tests_run: 1, elapsed_ms: 805, source: 'handoff',
        selector: 'testing/test_capture.py::test_capture_binary_output',
        artifact_refs: [{ location: 'run', path: 'artifacts/self-test/stdout.txt', kind: 'stdout' }],
      },
    }],
  });
  show('/evaluations/handoff');
  expect(await screen.findByText('收尾对照')).toBeInTheDocument();
  expect(screen.getByText('pytest-5262 · 预检未通过')).toBeInTheDocument();
  expect(screen.getByText(/普通输入被拒/)).toHaveTextContent('普通输入被拒 65,642 B · 紧凑输入放行 4,402 B');
  expect(screen.getByText('公开自测 通过 · 实际执行 1 项 · 0.81 秒')).toBeInTheDocument();
  expect(screen.getByText(/执行：completed；独立评分：failed/)).toBeInTheDocument();
  expect(screen.getByText('下载自测标准输出')).toHaveAttribute(
    'href', '/api/runs/run-1/artifacts?location=run&path=artifacts%2Fself-test%2Fstdout.txt',
  );
});

it('停批仍展示费用预留、未启动项和同一 Run 的独立证据', /* 校验费用和试次均来自冻结报告。 */ async () => {
  const slot = { run_id: 'one', case_id: 'repo', strategy: 'off', repeat: 0 }; // 已评分的基线。
  vi.mocked(getEvaluation).mockResolvedValue({
    id: 'paid', name: '付费停批', created_at: '2026-11-12T00:00:00Z', state: 'stopped', mode: 'real_model', model: 'deepseek-flash',
    experiment: { split: 'dev', repeats: 1 }, stop_reason: 'unknown_usage',
    budget: { known_usd_picos: 1e10, reserved_usd_picos: 35e10, limit_usd_picos: 3e12, reservation_usd_picos: 35e10, calls_admitted: 2,
      max_calls: 360, known_tokens: 100, max_known_tokens: 5000000, pricing_url: 'https://api-docs.deepseek.com/' },
    cases: [{ id: 'repo', prompt: '修复缺陷' }], schedule: [slot, { ...slot, strategy: 'hybrid', run_id: 'two' }],
    summary: { paired: [], groups: ['off', 'hybrid'].map(/* 两种策略都应该出现在同题对照。 */ strategy => ({
      strategy, planned: 1, finished: 0, successes: 0, success_rate: null, complete: false, pass3: null,
      known_tokens: 0, usage_complete: false, tokens_per_success: null, duration_p50: null, duration_p95: null, intervention_rate: null, model_calls: 0,
    })) },
    trials: [{ ...slot, success: false, execution_status: 'failed', stop_reason: 'unknown_usage',
      grade: { status: 'passed' }, grade_artifacts: { score: 'sha1', log: 'sha2' }, known_tokens: 100, usage_complete: false }],
  });
  show('/evaluations/paid');
  expect(await screen.findByRole('table', { name: '同题对照' })).toHaveTextContent('未启动');
  expect(screen.getByText('1 / 2 已结束')).toBeInTheDocument();
  expect(screen.getByText('$0.35')).toBeInTheDocument();
  expect(screen.getByText(/停止原因：模型用量未确认/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '下载评分 JSON' })).toHaveAttribute('href', '/api/evaluations/paid/grades/one/score');
  expect(screen.getByRole('link', { name: '下载测试日志' })).toHaveAttribute('href', '/api/evaluations/paid/grades/one/log');
});


it('三组报告区分原生轨迹与Run并显示累计费用及分段耗时', /* 只读下载不得跳向不存在的Run。 */ async () => {
  vi.mocked(getEvaluationTrajectory).mockResolvedValue({ messages: [{ role: 'assistant', content: '原生固定轨迹' }] });
  vi.mocked(getEvaluation).mockResolvedValue({ id: 'three', name: '三组开发校准', created_at: '2026-11-15T00:00:00Z',
    state: 'completed', mode: 'scripted', model: 'deepseek-flash', campaign_phase: 'calibration',
    budget: { known_usd_picos: 31863000000, reserved_usd_picos: 350000000000, limit_usd_picos: 40000000000000,
      reservation_usd_picos: 350000000000, calls_admitted: 41, max_calls: 12240, known_tokens: 539729,
      max_known_tokens: 1000000000000, pricing_url: 'https://api-docs.deepseek.com/',
      approved_holds: [{ run_id: 'old', turn: 7, reserved_usd_picos: 350000000000, receipt_sha256: 'receipt', approval_sha256: 'approved' }] },
    experiment: { split: 'dev', repeats: 1 }, summary: { groups: [], paired: [],
      patch_pairs: [{ baseline: 'B', candidate: 'C', metric: 'resolved', wins: ['one'], losses: [], ties: [], valid_pairs: 1, planned_pairs: 1 }] },
    trials: [{ run_id: 'native', case_id: 'one', strategy: 'A', repeat: 0, executor: 'mini_swe_agent', context_policy: 'native',
      run_available: false, trajectory_available: true, success: true, execution_status: 'completed', stop_reason: null,
      grade: { status: 'passed' }, known_tokens: 12, usage_complete: true, preparation_seconds: 2, agent_seconds: 3, grading_seconds: 4 }] });
  show('/evaluations/three');
  expect(await screen.findByText('三组开发校准')).toBeInTheDocument();
  expect(screen.getByText(/费用为整轮累计/)).toBeInTheDocument();
  expect(screen.getByText('$0.35（其中获批保留 $0.35）')).toBeInTheDocument();
  expect(screen.getByText(/独立修复 · C 相对 B/)).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: '查看运行轨迹' })).not.toBeInTheDocument();
  expect(screen.getByText(/环境准备 2.0 秒 · Agent 3.0 秒 · 独立评分 4.0 秒/)).toBeInTheDocument();
  const details = screen.getByText('查看 mini-SWE-agent 原生轨迹').parentElement!;
  details.setAttribute('open', '');
  fireEvent(details, new Event('toggle'));
  expect(await screen.findByText('原生固定轨迹')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '下载原生轨迹 JSON' })).toHaveAttribute('href', '/api/evaluations/three/trajectories/native');
});

it.each([
  ['budget_checkpoint_error', '评测检查点保存失败'],
  ['internal_error', '运行宿主内部异常'],
  ['mini_adapter_error', '外部基线适配器异常'],
])('宿主停止原因 %s 使用可读说明', /* 从后端现有原因字段展示异常，页面不得触发续跑。 */ async (reason, label) => {
  vi.mocked(getEvaluation).mockResolvedValue({ id: 'checkpoint', name: '保存失败停轮',
    created_at: '2026-11-16T00:00:00Z', state: 'stopped', mode: 'real_model', model: 'deepseek-flash',
    stop_reason: reason, experiment: { split: 'dev', repeats: 1 }, cases: [], schedule: [], trials: [],
    summary: { groups: [], paired: [] },
  });
  show('/evaluations/checkpoint');
  expect(await screen.findByText('停止原因：' + label)).toBeInTheDocument();
});
