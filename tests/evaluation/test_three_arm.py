"""三组评测的离线接线与预算边界；所有HTTP均用本地 MockTransport。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest

from agent_harness.config import load_config
from agent_harness.context.archive import ContextArchive, ReadContextArguments
from agent_harness.context.compression import message_groups
from agent_harness.evaluation.budget import RESERVATION_PICOS, USD_PICOS
from agent_harness.evaluation.campaign_budget import CampaignBudget
from agent_harness.evaluation.engine import context_profile, trial_schedule
from agent_harness.evaluation.mini import run_mini
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.runtime.runner import AgentStopped, run_task
from agent_harness.storage.runs import RunStore
from tests.context.test_compression import CompressionServer, _manifest


def test_custom_limits_share_one_persistent_balance(tmp_path):
    """两阶段共享 tmp_path 内的账本，未知预留在重启后仍占额度，不能更换阶段限额重置。"""
    phases = {"calibration": (10 * USD_PICOS, 240), "formal": (10 * USD_PICOS, 2400)}
    path = tmp_path / "ledger.json"
    options = {"total_limit": 10 * USD_PICOS, "total_calls": 2640, "phases": phases}
    budget = CampaignBudget(path, "frozen", **options)
    budget.bind("calibration", {}, lambda: None)  # 独立测试无需写第二份投影。
    budget.authorize("dev", 1, "execution")
    restored = CampaignBudget(path, "frozen", **options)
    assert restored.state["reserved_usd_picos"] == RESERVATION_PICOS
    assert restored.state["limit_usd_picos"] == 10 * USD_PICOS
    assert restored.has_pending_request()
    with pytest.raises(ValueError, match="账本协议改变"):
        CampaignBudget(path, "frozen", **{**options, "phases": {**phases, "formal": (9 * USD_PICOS, 2400)}})


def ledger(tmp_path: Path, phase: str = "calibration") -> CampaignBudget:
    """在 tmp_path 创建隔离账本并绑定 phase，返回守卫；不调用外部服务。"""
    value = CampaignBudget(tmp_path / "ledger.json", "fixed-test-protocol")
    value.bind(phase, {}, lambda: None)
    return value


def usage(miss: int = 100, output: int = 20) -> dict:
    """返回可核对的供应商用量；miss 和 output 单位是token，默认计费很小。"""
    return {"prompt_tokens": miss, "prompt_cache_miss_tokens": miss, "prompt_cache_hit_tokens": 0,
            "completion_tokens": output, "total_tokens": miss + output}


def test_campaign_restart_keeps_unknown_reservation(tmp_path: Path):
    """模拟发单后崩溃，重新构造账本不能清零费用或再次请求。"""
    budget = ledger(tmp_path)
    budget.authorize("one", 1, "execution")
    restored = CampaignBudget(tmp_path / "ledger.json", "fixed-test-protocol")
    restored.bind("formal", {}, lambda: None)
    assert restored.state["reserved_usd_picos"] == RESERVATION_PICOS
    assert restored.state["calls_admitted"] == 1
    with pytest.raises(AgentStopped, match="unknown_usage"):
        restored.authorize("two", 1, "execution")
    with pytest.raises(ValueError, match="协议"):
        CampaignBudget(tmp_path / "ledger.json", "changed")


def test_campaign_settlement_is_idempotent_and_shared_across_phases(tmp_path: Path):
    """重复回执不双计费；进入正式阶段仍保留开发阶段的请求和费用。"""
    budget = ledger(tmp_path)
    budget.authorize("one", 1, "summary")
    budget.settle("one", 1, usage(), "deepseek-flash")
    cost = budget.state["known_usd_picos"]
    budget.settle("one", 1, usage(), "deepseek-flash")
    assert budget.state["known_usd_picos"] == cost
    restored = CampaignBudget(tmp_path / "ledger.json", "fixed-test-protocol")
    restored.bind("formal", {}, lambda: None)
    restored.authorize("two", 1, "execution")
    restored.settle("two", 1, usage(), "deepseek-flash")
    assert restored.state["calls_admitted"] == 2
    assert restored.state["known_usd_picos"] == 2 * cost
    assert restored.phase_totals("calibration")["calls"] == 1
    with pytest.raises(ValueError, match="重复请求"):
        restored.authorize("one", 1, "execution")


def test_trial_and_phase_limits_block_before_send(tmp_path: Path):
    """每题及阶段费用不足预留时拒绝发单；新题仍共用同一阶段计数。"""
    budget = ledger(tmp_path)
    for turn in range(1, 4):
        budget.authorize("one", turn, "execution")
        budget.settle("one", turn, usage(1_000_000, 0), "deepseek-flash")
    with pytest.raises(AgentStopped, match="trial_cost_limit"):
        budget.authorize("one", 4, "execution")
    assert budget.stop_reason is None
    for index in range(13):
        budget.authorize("other-" + str(index), 1, "execution")
        budget.settle("other-" + str(index), 1, usage(1_000_000, 0), "deepseek-flash")
    with pytest.raises(AgentStopped, match="phase_cost_limit"):
        budget.authorize("last", 1, "execution")
    assert budget.state["known_usd_picos"] == 4.8 * USD_PICOS
    assert budget.state["calls_admitted"] == 16


def test_balanced_three_arm_orders_and_context_only_axis():
    """六题覆盖全部六种组顺序；B/C共享收尾，只有上下文相关字段不同。"""
    experiment = Experiment(groups=preset_groups(["A", "B", "C"]), seed=20261115)
    rows = trial_schedule([str(index) for index in range(6)], experiment, True)
    orders = [tuple(row["strategy"] for row in rows[offset:offset+3]) for offset in range(0, 18, 3)]
    assert len(set(orders)) == 6
    assert all({*order} == {"A", "B", "C"} for order in orders)
    simple, hybrid = context_profile("window"), context_profile("hybrid_budgeted")
    assert simple["root_handoff"] == hybrid["root_handoff"]
    assert simple["max_summary_calls"] == 0 and simple["keep_recent_groups"] == 5


def test_window_keeps_five_complete_batches_and_retrievable_history(tmp_path: Path):
    """真实Runner连续七次工具调用后，原需求和最近五组完整配对，旧组可回读且无摘要请求。"""
    manifest = _manifest(tmp_path)
    manifest.config.context.compression = "window"
    manifest.config.context.keep_recent_groups = 5
    manifest.config.context.max_summary_calls = 0
    server = CompressionServer(observations=7)
    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "fixture", transport=httpx.MockTransport(server.handle)))
    assert outcome.result.execution_status == "completed"
    assert not server.summaries
    final = server.execution[-1]["messages"]
    units = message_groups(final)
    assert sum(bool(unit.messages[0].get("tool_calls")) for unit in units) == 5
    assert any("USER_CONSTRAINT" in message.get("content", "") for message in final if message["role"] == "user")
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    actions = [event.payload for event in history.events if event.type == "context_action" and event.payload.get("reason") == "sliding_window"]
    assert len(actions) == 2
    archive = ContextArchive(tmp_path / "runs" / str(manifest.run_id), manifest)
    original = archive.read(ReadContextArguments(reference=actions[0]["reference"], max_bytes=16384))
    assert "read-1" in original["content"]


def test_window_archive_obeys_thinking_tool_protocol(tmp_path: Path):
    """真实Runner跨过五组窗口时，所有assistant消息保持思考工具协议且不伪造历史推理。"""
    from tests.context.test_compression import _call, _response
    from tests.helpers.streaming import model_stream_response

    manifest = _manifest(tmp_path)
    manifest.config.context.compression = "window"
    manifest.config.context.keep_recent_groups = 5
    manifest.config.context.max_summary_calls = 0
    manifest.config.generation.thinking = "enabled"
    requests = []  # 保存实际HTTP请求体，覆盖压缩器到供应商适配器的完整边界。

    def handle(request: httpx.Request) -> httpx.Response:
        """校验request中的思考工具字段；缺失返回400，否则提供七轮读取与最终回答，不访问网络。"""
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "enabled"}
        if body.get("tools") and any(message["role"] == "assistant" and
                not isinstance(message.get("reasoning_content"), str) for message in body["messages"]):
            return httpx.Response(400, json={"error": {"message": "Missing reasoning_content"}})
        index = len(requests)
        response = _response("已读取。", _call("read-" + str(index), "read_file", {"path": "small.txt"})) if index <= 7 else _response("完成。")
        response["choices"][0]["message"]["reasoning_content"] = "原始推理-" + str(index)
        return model_stream_response(200, json=response)

    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "fixture", transport=httpx.MockTransport(handle)))
    assert outcome.result.execution_status == "completed", outcome.result.stop_reason
    assert len(requests) == 8
    final = requests[-1]["messages"]
    retained = [message for message in final if message.get("tool_calls")]
    assert [message["reasoning_content"] for message in retained] == ["原始推理-" + str(index) for index in range(3, 8)]
    references = [message for message in final if message["role"] == "assistant" and not message.get("tool_calls")]
    assert len(references) == 2 and all(message["reasoning_content"] == "" for message in references)
    assert len([unit for unit in message_groups(final) if unit.messages[0].get("tool_calls")]) == 5


def test_mini_native_format_repair_submission_and_billing(tmp_path: Path):
    """原生循环先修复格式再提交；两次HTTP计费且轨迹与终止规则保持上游格式。"""
    config = load_config(Path("evals/swe-three-arm/deepseek.toml"))
    budget = ledger(tmp_path)
    bodies = []

    async def respond(request: httpx.Request) -> httpx.Response:
        """返回一条无工具格式错误和一条真实原生 Bash 提交；记录实际请求参数。"""
        bodies.append(json.loads(request.content))
        message = {"role": "assistant", "content": "thinking", "reasoning_content": "reason"}
        if len(bodies) == 2:
            message["tool_calls"] = [{"id": "submit", "type": "function", "function": {"name": "bash",
                "arguments": json.dumps({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})}}]
        return httpx.Response(200, json={"model": "deepseek-v4.1-flash-1017", "choices": [{
            "message": message, "finish_reason": "tool_calls" if len(bodies) == 2 else "stop"}], "usage": usage()})

    class Environment:
        """脚本化Docker边界；原生循环、请求门禁和轨迹均使用生产实现。"""
        image = "sha256:offline"  # 固定假镜像身份，仅用于隔离离线验收。
        directory = tmp_path  # 原始过程证据位置。

        async def command(self, path, command, seconds, *, deadline=None, env=None):
            """核对固定命令限制，返回提交输出；不启动Docker或外部进程。"""
            assert seconds <= 120 and deadline is not None
            assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command[-1]
            return {"exit_code": 0, "stdout": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch", "stderr": ""}

        def full_stdout(self, result):
            """返回离线完整输出供原生提交检测，不追加隐藏数据。"""
            return result["stdout"]

    result, evidence = asyncio.run(run_mini(config, Environment(), tmp_path, str(uuid4()), "fix sample", budget,
        "offline", tmp_path / "trajectory.json", transport=httpx.MockTransport(respond)))
    assert result.execution_status == "completed", result.stop_reason
    assert result.final_answer == "patch"
    assert result.model_turns == result.http_attempts == budget.state["calls_admitted"] == 2
    assert result.tool_calls == 1 and result.usage_complete
    assert all(body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high" and body["top_p"] == .95 for body in bodies)
    assert evidence["trajectory_sha256"] and evidence["summary_calls"] == 0
    trajectory = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert trajectory["info"]["mini_version"] == "2.4.6"
    assert trajectory["trajectory_format"] == "mini-swe-agent-1.1"

def test_campaign_runs_204_new_trials_and_reuses_formal_first_repeat(tmp_path: Path, monkeypatch):
    """完整阶段调度使用假执行边界，验证18+150+36次调用、同一账本和六题三次统计。"""
    import agent_harness.evaluation.campaign as campaign_module
    from agent_harness.evaluation.campaign import Campaign
    from agent_harness.evaluation.engine import read_report, save_report

    campaign = Campaign(tmp_path / 'round', Path('evals/swe-three-arm'), tmp_path / 'work', Path('.runtime/swe-env/Scripts/python.exe'))
    monkeypatch.setattr(Campaign, 'docker', lambda self, args: '')  # 仅模拟宿主库存，不连接Docker。
    monkeypatch.setattr(Campaign, '_preflight_passed', lambda self, identifier, receipt: True)  # 仅替代已单独验收的证书边界。
    monkeypatch.setattr(Campaign, 'cleanup', lambda self, report: None)  # 夹具没有真实镜像或仓库目录。
    campaign.initialize()
    dev = json.loads(Path('evals/swe-three-arm/calibration.json').read_text(encoding='utf-8'))['cases']
    formal = [row[0] for row in campaign.selection['selected']]
    campaign.state['formal_ids'] = formal
    campaign.state['stability_ids'] = [row[0] for row in campaign.selection['sentinels']]
    for phase, ids in [('calibration', [row['id'] for row in dev]), ('formal', formal)]:
        for identifier in ids:
            campaign.state['preflight'][phase + ':' + identifier] = {'passed': True, 'report_id': str(uuid4()), 'certificate_sha256': 'fixture'}

    async def evaluate_fixture(suite_path, config, experiment, runs_root, work_root, api_key, **options):
        """按冻结槽位模拟一次合法HTTP回执；真实使用共享预算与阶段报告保存，不启动模型。"""
        slots = options['frozen_schedule']
        phase = options['campaign_phase']
        budget = options['campaign_budget']
        batch = str(uuid4())
        folder = runs_root / '.evaluations' / batch
        folder.mkdir(parents=True)
        report = {'id': batch, 'name': phase, 'created_at': '2026-11-15T00:00:00Z', 'state': 'completed',
            'stop_reason': None, 'mode': 'scripted', 'model': 'deepseek-flash', 'protocol_sha256': 'fixture',
            'experiment': experiment.model_dump(), 'cases': json.loads(suite_path.read_text(encoding='utf-8'))['cases'],
            'schedule': slots, 'trials': [], 'preflight': [], 'environments': {}, 'active_trial': None}
        budget.bind(phase, report, lambda: save_report(folder, report))  # 保存回调模拟发送前落盘。
        for slot in slots:
            budget.authorize(slot['run_id'], 1, 'execution')
            budget.settle(slot['run_id'], 1, usage(), 'deepseek-flash')
            report['trials'].append({**slot, 'success': True, 'comparison_valid': True, 'usage_complete': True,
                'known_tokens': 120, 'model_calls': 1, 'http_attempts': 1, 'needs_input': False,
                'execution_status': 'completed', 'stop_reason': None, 'grade': {'status': 'passed'},
                'duration_seconds': 1, 'summary_calls': 0, 'response_model': 'deepseek-flash',
                'cost': budget.run_cost(slot['run_id']), 'last_admitted_input_bytes': 2000})
        options['progress'](report)
        save_report(folder, report)
        return report

    monkeypatch.setattr(campaign_module, 'evaluate', evaluate_fixture)
    asyncio.run(campaign.run('offline-key'))
    assert campaign.state['completed'] and campaign.state['calibration_passed']
    assert campaign.budget.state['calls_admitted'] == 204
    assert [campaign.budget.phase_totals(phase)['calls'] for phase in ('calibration', 'formal', 'stability')] == [18, 150, 36]
    report = read_report(campaign.runs_root, campaign.state['phases']['stability']['report_id'])
    assert len(report['trials']) == len(report['schedule']) == 54
    assert len({row['case_id'] for row in report['trials']}) == 6
    assert all(group['planned'] == 18 and group['pass3'] == 1 for group in report['summary']['groups'])
    assert {row['repeat'] for row in report['trials']} == {0, 1, 2}


def test_campaign_cleanup_only_removes_owned_workspace(tmp_path: Path):
    """核对本轮临时副本可回收，记录目录与相邻用户文件不受影响。"""
    from agent_harness.evaluation.campaign import Campaign
    import stat
    campaign = Campaign(tmp_path / 'round', Path('evals/swe-three-arm'), tmp_path / 'work', Path('.runtime/swe-env/Scripts/python.exe'))
    own = tmp_path / 'work' / str(uuid4())
    own.mkdir(parents=True)
    tracked = own / 'git-object'
    tracked.write_text('owned')
    tracked.chmod(stat.S_IREAD)
    outside = tmp_path / 'round'
    outside.mkdir()
    (outside / 'ledger.json').write_text('retain')
    with pytest.raises(ValueError, match='越界'):
        campaign.remove_workspace(outside)
    campaign.remove_workspace(own)
    assert not own.exists()
    assert (outside / 'ledger.json').read_text() == 'retain'
