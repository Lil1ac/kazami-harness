"""通过真实Runner、外部评分及HTTP核对评测归因；脚本模型不作为模型质量证据。"""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import pytest
import httpx
from fastapi.testclient import TestClient
from agent_harness.config import load_config
from agent_harness.evaluation.budget import RESERVATION_PICOS
from agent_harness.evaluation.engine import context_profile, evaluate, read_report, save_report, trial_input_evidence, trial_schedule
from agent_harness.evaluation.models import Experiment, preset_groups, Suite
from agent_harness.evaluation.report import summarize
from agent_harness.providers.base import ModelResponse
from agent_harness.schemas import RunEvent
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from tests.helpers.models import configured_run


def suite_file(root: Path) -> Path:
    """在root建立两条互不共享副本的任务，返回JSON路径；评分只接受真实生成文件。"""
    cases = []
    for index in range(2):
        directory = root / str(index)
        for kind in ("source", "reference"):
            (directory / kind).mkdir(parents=True)
            (directory / kind / "result.txt").write_text("ok" if kind == "reference" else "bad", encoding="utf-8")
        (directory / "grader.py").write_text('import sys\nfrom pathlib import Path\nassert (Path(sys.argv[1])/"result.txt").read_text()=="ok"\n', encoding="utf-8")
        cases.append({"id": f"case-{index}", "project": "fixture", "category": "output", "split": "dev", "prompt": "将result.txt写为ok。",
                      "source": f"{index}/source", "reference": f"{index}/reference", "grader": f"{index}/grader.py"})
    path = root / "suite.json"
    path.write_text(json.dumps({"name": "离线链路", "version": "1", "provenance": "自建夹具", "cases": cases}), encoding="utf-8")
    return path


class Writer:
    """按协议写文件的离线模型；每个试次单独实例，防止跨试次共享状态。"""

    async def complete(self, messages, tools):
        """按messages是否已有工具结果返回写调用或结束；tools只提供协议，不读取参考答案。"""
        from agent_harness.providers.base import ToolCall
        if any(message.get("role") == "tool" for message in messages):
            return ModelResponse(message={"role": "assistant", "content": "完成"}, tool_calls=[], response_model="fixture", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, finish_reason="stop")
        call = ToolCall(call_id="write", name="write_file", arguments_json=json.dumps({"path": "result.txt", "content": "ok", "overwrite": True}))
        return ModelResponse(message={"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}]}, tool_calls=[call], response_model="fixture", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, finish_reason="tool_calls")


def configuration():
    """返回关闭混淆因素的测试配置；仅开放写文件，实际Runner仍执行全部宿主检查。"""
    return configured_run(tools=["write_file"], memory={"use": False, "capture": False}, limits={"max_model_turns": 4})


def test_full_pipeline_and_read_only_api(tmp_path):
    """实际执行四试次，核对隔离、评分、未知pass3及HTTP读取不重跑。"""
    root = tmp_path / "runs"
    suite = suite_file(tmp_path / "suite")
    report = asyncio.run(evaluate(suite, configuration(), Experiment(), root, tmp_path / "work", "fixture", client_factory=Writer))
    assert report["state"] == "completed"
    assert len(report["trials"]) == 4 and all(row["success"] for row in report["trials"])
    assert len({row["run_id"] for row in report["trials"]}) == 4
    assert all(group["pass3"] is None and group["tokens_per_success"] == 30 for group in report["summary"]["groups"])
    assert (suite.parent / "0/source/result.txt").read_text() == "bad"
    web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist")
    web.frontend_dist.mkdir()
    (web.frontend_dist / "index.html").write_text("<html>evaluation-shell</html>")
    app = create_app(web, configuration(), root)
    with TestClient(app) as client:
        assert "evaluation-shell" in client.get("/evaluations").text
        assert "evaluation-shell" in client.get("/evaluations/" + report["id"]).text
        assert client.get("/api/evaluations").json()["items"][0]["id"] == report["id"]
        assert client.get("/api/evaluations/" + report["id"]).json()["trials"] == report["trials"]
        assert client.get("/api/evaluations/" + str(uuid4())).status_code == 404
        assert client.get(f'/api/evaluations/{report["id"]}/patches/{report["trials"][0]["run_id"]}').status_code == 404
    assert read_report(root, report["id"])["trials"] == report["trials"]
    review_path = root / ".evaluations" / report["id"] / "review.json"
    review = {"protocol_sha256": report["protocol_sha256"], "verdict": "invalid_comparison", "case_ids": ["case-0"], "reason": "输入要求有歧义"}
    review_path.write_text(json.dumps(review), encoding="utf-8")
    reviewed = read_report(root, report["id"])
    assert reviewed["quality_review"]["reason"] == "输入要求有歧义"
    assert reviewed["trials"] == report["trials"]
    review["protocol_sha256"] = "0" * 64
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ValueError):
        read_report(root, report["id"])


@pytest.mark.parametrize("strategies", [["hybrid", "hybrid_handoff"], ["hybrid_handoff", "hybrid_budgeted"]])
def test_hybrid_arms_share_compression_and_record_root_handoff(tmp_path, strategies):
    """用 tmp_path 的两条评分夹具校验真实执行中的 A/B 唯一开关和报告回执，无返回值。"""
    from agent_harness.storage.runs import RunStore

    class HandoffWriter(Writer):
        """模拟能识别无工具末轮协议的模型。"""

        async def complete(self, messages, tools):
            """有工具时用正常写入/答复协议；无工具时响应末轮 JSON，返回有完整用量的回执。"""
            if tools:
                return await super().complete(messages, tools)
            assert any("宿主核验的本段工具回执" in item.get("content", "") for item in messages)
            return ModelResponse(message={"role": "assistant", "content": '{"status":"ready","answer":"已交付"}'},
                                 tool_calls=[], response_model="fixture",
                                 usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, finish_reason="stop")

    config = configuration()
    config.limits.max_model_turns = 2
    runs = tmp_path / "r"
    report = asyncio.run(evaluate(suite_file(tmp_path / "s"), config, Experiment(groups=preset_groups(strategies)),
                                  runs, tmp_path / "w", "fixture", client_factory=HandoffWriter))
    assert report["state"] == "completed" and len(report["trials"]) == 4
    for trial in report["trials"]:
        handoff = trial["strategy"] in {"hybrid_handoff", "hybrid_budgeted"}
        history = RunStore.read(runs, trial["run_id"])
        assert history.manifest.config.context.compression == "hybrid"
        assert history.manifest.config.context.root_handoff is handoff
        assert history.manifest.config.context.budget_projection is (trial["strategy"] == "hybrid_budgeted")
        assert trial["first_write"]["path"] == "result.txt" and trial["first_write"]["model_turn"] == 1
        assert trial["grade"]["status"] == "passed" and trial["success"]
        assert trial["handoff_status"] == ("ready" if handoff else "not_triggered")
        # 注入 ModelClient 已调用两轮，但没有经过 HTTP 客户端。
        assert trial["execution_calls"] == 2 and trial["http_attempts"] == trial["summary_calls"] == 0
        assert trial["last_admitted_input_bytes"] > 0 and trial["last_rejected_input_bytes"] is None


def test_same_round_input_rejection_and_handoff_are_reported_independently():
    """核对同一模型轮的普通拒绝与已发收尾独立记账，并保留紧凑请求被拒的事实；无外部请求。"""
    identifier = uuid4()
    items = [
        ("context_checked", {"purpose": "execution", "phase": "execution", "model_turn": 21,
                             "input_bytes": 65642, "allowed": False}),
        ("budget_handoff_started", {"mode": "root", "trigger": "input_rejected"}),
        ("context_checked", {"purpose": "execution", "phase": "handoff", "model_turn": 21,
                             "input_bytes": 4100, "allowed": True}),
        ("model_request_started", {"purpose": "execution", "model_turn": 21, "input_check_seq": 3}),
        ("budget_handoff_finished", {"mode": "root", "status": "ready", "trigger": "input_rejected"}),
    ]
    events = [RunEvent(run_id=identifier, seq=index, elapsed_ms=index, type=kind, payload=payload)
              for index, (kind, payload) in enumerate(items, 1)]
    result = trial_input_evidence(events)
    assert result["last_rejected_input_bytes"] == result["input_bytes_peak"] == 65642
    assert result["last_admitted_input_bytes"] == result["handoff_input_bytes"] == result["handoff_checked_bytes"] == 4100
    assert result["execution_calls"] == 1 and result["handoff_status"] == "ready"
    assert result["handoff_trigger"] == "input_rejected"
    events[2].payload.update(input_bytes=66000, allowed=False)
    stopped = trial_input_evidence(events[:3])
    assert stopped["last_rejected_input_bytes"] == 65642 and stopped["handoff_checked_bytes"] == 66000
    assert stopped["handoff_input_bytes"] is None and stopped["execution_calls"] == 0


def test_verified_handoff_only_changes_the_rejection_trigger():
    """核对冻结的对照策略共用 hybrid、预算投影与根收尾；未配对候选不可作为独立对照。"""
    names = ["hybrid_budgeted", "hybrid_budgeted_verified_handoff"]
    experiment = Experiment(groups=preset_groups(names))
    original, candidate = (context_profile(name) for name in experiment.group_ids)
    assert candidate == {**original, "root_handoff_after_input_rejection": True}
    assert original["budget_projection"] and original["root_handoff"]
    with pytest.raises(ValueError, match="同样使用预算投影"):
        Experiment(groups=preset_groups(["hybrid_handoff", names[1]]))


def test_invalid_reference_never_calls_model(tmp_path):
    """错误参考解让整批在预检阶段停止，不能把坏题造成的失败归因模型。"""
    suite = suite_file(tmp_path / "suite")
    (suite.parent / "0/reference/result.txt").write_text("bad")
    report = asyncio.run(evaluate(suite, configuration(), Experiment(), tmp_path / "runs", tmp_path / "work", "fixture", client_factory=Writer))
    assert report["stop_reason"] == "invalid_case" and not report["trials"]


def test_batch_limit_preserves_unfinished_denominator(tmp_path):
    """调用额度不足下一试次时停止，未运行数保持可见，禁止发布成对提升。"""
    report = asyncio.run(evaluate(suite_file(tmp_path / "suite"), configuration(), Experiment(max_model_calls=4), tmp_path / "runs", tmp_path / "work", "fixture", client_factory=Writer))
    assert report["stop_reason"] == "batch_model_limit"
    assert len(report["trials"]) == 1
    assert not report["summary"]["paired"]
    assert all(not group["complete"] for group in report["summary"]["groups"])


def test_statistics_count_failure_cost_and_task_pairing():
    """三次中一失败与全部通过分别统计，失败成本保留，未知用量不能除成功次数。"""
    rows = []
    for strategy in ("off", "hybrid"):
        for repeat in range(3):
            rows.append({"strategy": strategy, "case_id": "a", "repeat": repeat, "success": strategy == "hybrid" or repeat != 2,
                         "known_tokens": 100, "usage_complete": strategy == "off", "duration_seconds": 1, "needs_input": False, "model_calls": 1})
    rows += [{**row, "case_id": "b"} for row in rows]
    report = {"experiment": {"groups": [g.model_dump() for g in preset_groups(["off", "hybrid"])], "repeats": 3, "seed": 1}, "schedule": rows, "trials": rows, "cases": [{"id": "a"}, {"id": "b"}]}
    result = summarize(report)
    assert result["groups"][0]["pass3"] == 0
    assert result["groups"][1]["pass3"] == 1
    assert result["groups"][0]["tokens_per_success"] == 150
    assert result["groups"][1]["tokens_per_success"] is None
    assert result["paired"][0]["difference"] == pytest.approx(1 / 3)


def test_manifest_rejects_duplicate_and_escape(tmp_path):
    """重复题号和越界引用均在执行前拒绝，不允许覆盖或读取其他题库。"""
    data = json.loads(suite_file(tmp_path / "suite").read_text())
    data["cases"][1]["id"] = data["cases"][0]["id"]
    with pytest.raises(ValueError):
        Suite.model_validate(data)
    data["cases"].pop()
    data["cases"][0]["source"] = "../outside"
    with pytest.raises(ValueError):
        Suite.model_validate(data)


def test_unknown_usage_stops_without_discarding_attempt(tmp_path):
    """缺失供应商用量的已完成任务仍计分，但停止下一试次且不发布完整成本。"""
    class UnknownWriter(Writer):
        """输出内容正常但不返回usage的供应商替身，不暗示免费请求。"""

        async def complete(self, messages, tools):
            """按父类生成messages/tools对应响应，返回usage缺失的同一响应。"""
            from dataclasses import replace
            return replace(await super().complete(messages, tools), usage=None)

    report = asyncio.run(evaluate(suite_file(tmp_path / "suite"), configuration(), Experiment(), tmp_path / "runs", tmp_path / "work", "fixture", client_factory=UnknownWriter))
    assert report["stop_reason"] == "unknown_usage" and len(report["trials"]) == 1
    assert report["trials"][0]["success"] and not report["trials"][0]["usage_complete"]
    assert all(group["tokens_per_success"] is None for group in report["summary"]["groups"])


def test_cancelled_attempt_is_persisted(tmp_path, monkeypatch):
    """Runner被取消时保留已启动试次，不因异常从分母和冻结计划消失。"""
    async def cancelled(*args, **kwargs):
        """模拟args/kwargs指定的Runner启动后取消，抛出取消信号而不返回成功结果。"""
        raise asyncio.CancelledError()

    monkeypatch.setattr("agent_harness.evaluation.engine.run_task", cancelled)
    root = tmp_path / "runs"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(evaluate(suite_file(tmp_path / "suite"), configuration(), Experiment(), root, tmp_path / "work", "fixture", client_factory=Writer))
    identifier = next((root / ".evaluations").iterdir()).name
    report = read_report(root, identifier)
    assert report["stop_reason"] == "cancelled"
    assert report["active_trial"] is None
    assert len(report["trials"]) == 1 and report["trials"][0]["execution_status"] == "cancelled"
    assert report["trials"][0]["grade"]["status"] == "not_scored"


@pytest.mark.parametrize(("scenario", "expected_calls", "expected_reason"), [
    ("cost", 1, "batch_cost_limit"), ("call", 1, "batch_model_limit"),
    ("token", 1, "batch_known_token_threshold"), ("unknown_last", 8, "unknown_usage"),
    ("disconnect", 1, "unknown_usage"), ("length", 4, None), ("changed", 1, "evaluation_model_changed"),
])
def test_real_gateway_checks_batch_before_http_and_preserves_receipts(tmp_path, scenario, expected_calls, expected_reason):
    """以HTTP SSE模拟 scenario，验证挡单不访问供应商，完整或未知回执与实际请求次数相符。"""
    suite = suite_file(tmp_path / "suite")
    root = tmp_path / "runs"
    config = load_config(Path(__file__).resolve().parents[2] / "evals/swe-pilot/deepseek.toml")
    config.tools = ["write_file"]
    experiment = Experiment(max_cost_cents=35 if scenario == "cost" else 300,
                            max_model_calls=1 if scenario == "call" else 360,
                            max_known_tokens=15 if scenario == "token" else 5000000)
    http_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        """在模拟 HTTP 入口先核对落盘预留，再返回场景要求的 SSE。"""
        nonlocal http_calls
        http_calls += 1
        report = read_report(root, next((root / ".evaluations").iterdir()).name)
        active = report["active_trial"]
        budget = report["budget"]
        assert budget["calls_admitted"] == http_calls and budget["reserved_usd_picos"] == RESERVATION_PICOS
        receipt = budget["requests"][-1]
        assert receipt["run_id"] == active["run_id"] and receipt["status"] == "reserved"
        body = json.loads(request.content)
        assert body["max_tokens"] == 8192
        has_result = any(message["role"] == "tool" for message in body["messages"])
        if scenario == "disconnect":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: [DONE]\n\n")
        usage = {"prompt_tokens": 10, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10,
                 "completion_tokens": 5, "total_tokens": 15}
        if scenario == "unknown_last" and http_calls == 8:
            usage = None
        delta = {"role": "assistant", "content": "完成"}
        reason = "length" if scenario == "length" else "stop"
        if not has_result and reason != "length":
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "write", "type": "function",
                      "function": {"name": "write_file", "arguments": '{"path":"result.txt","content":"ok","overwrite":true}'}}]}
            reason = "tool_calls"
        model = "unexpected-model" if scenario == "changed" else "deepseek-flash"
        frame = {"id": str(http_calls), "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": reason}], "usage": usage}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")

    report = asyncio.run(evaluate(suite, config, experiment, root, tmp_path / "work", "fixture", transport=httpx.MockTransport(respond)))
    assert http_calls == expected_calls == report["budget"]["calls_admitted"]
    assert report["stop_reason"] == expected_reason
    assert report["state"] == ("stopped" if expected_reason else "completed")
    assert report["budget"]["known_usd_picos"] + report["budget"]["reserved_usd_picos"] <= report["budget"]["limit_usd_picos"]
    assert report["budget"]["reserved_usd_picos"] == (RESERVATION_PICOS if scenario in {"unknown_last", "disconnect", "changed"} else 0)
    if scenario == "unknown_last":
        assert len(report["trials"]) == 4 and report["trials"][-1]["grade"]["status"] == "passed"
        assert not report["trials"][-1]["cost"]["complete"]
    if scenario == "length":
        assert all(row["stop_reason"] == "model_output_truncated" and row["cost"]["complete"] for row in report["trials"])
        assert report["budget"]["known_tokens"] == 60


def test_schedule_has_adjacent_and_balanced_trials():
    """同题的两个策略相邻且各有三题先跑，运行编号仍全局独立。"""
    schedule = trial_schedule([f"case-{i}" for i in range(6)], Experiment(), True)
    assert len({slot["run_id"] for slot in schedule}) == 12
    assert all(left["case_id"] == right["case_id"] and {left["strategy"], right["strategy"]} == {"off", "hybrid"}
               for left, right in zip(schedule[::2], schedule[1::2]))
    assert [slot["strategy"] for slot in schedule[::2]].count("off") == 3


def test_independent_grading_download_checks_run_and_hash(tmp_path):
    """用真实只读 API 核对评分 JSON 和日志的归属，篡改后拒绝下载。"""
    root = tmp_path / "runs"
    report = asyncio.run(evaluate(suite_file(tmp_path / "suite"), configuration(), Experiment(), root, tmp_path / "work", "fixture", client_factory=Writer))
    row = report["trials"][0]
    folder = root / ".evaluations" / report["id"]
    grade = folder / "grades" / row["run_id"]
    score, log = grade / "official.json", grade / "test_output.txt"
    score.write_bytes(b'{"resolved":true}')
    log.write_bytes(b'Independent output')
    row["grade_artifacts"] = {"score": hashlib.sha256(score.read_bytes()).hexdigest(),
                              "log": hashlib.sha256(log.read_bytes()).hexdigest()}
    save_report(folder, report)
    web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist")
    with TestClient(create_app(web, configuration(), root)) as client:
        url = f'/api/evaluations/{report["id"]}/grades/{row["run_id"]}'
        assert client.get(url + "/score").json() == {"resolved": True}
        assert client.get(url + "/log").content == b'Independent output'
        assert client.get(f'/api/evaluations/{report["id"]}/grades/{uuid4()}/score').status_code == 404
        log.write_bytes(b'Altered output')
        assert client.get(url + "/log").status_code == 404
