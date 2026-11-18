"""用真实 Runner 与离线 HTTP 证明自测收尾的证据和发单边界。"""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
from pathlib import Path

import httpx
import pytest

from agent_harness.runtime.runner import run_task
from agent_harness.schemas import ContextPolicy, RunConfig
from agent_harness.storage.runs import RunStore
from agent_harness.tools.registry import ToolExecutor
from tests.context.test_context_run import make_manifest, model_reply, scripted_run, tool_call
from tests.helpers.streaming import model_stream_response


class PublicSelfTestExecutor(ToolExecutor):
    """保留真实文件工具，仅用可核对候选的夹具替代容器自测。"""

    public_self_test_available = True  # 表示开了两组共用的公开测试入口。

    def __init__(self, workspace: Path, runs_root: Path, config: RunConfig, status: str, *, existing: bool = False):
        """运行位置与配置沿用 Runner；status 为夹具结果，existing 时提供已有通过回执。"""
        super().__init__(workspace, runs_root, config)
        self.candidate = workspace / "result.txt"  # 目标文件本身用正常文件工具创建。
        self.status = status  # 本次夹具执行结果，error 模拟共享测试环境故障。
        self.started = []  # 记录宿主自测启动参数，用于验证不重复执行。
        self.fact = self._fact("passed", "model", sha256(b"changed").hexdigest()) if existing else None
        self.emit_self_test = None  # 运行开始后由真实 Runner 绑定落盘入口。

    def bind_public_self_test_events(self, emit) -> None:
        """绑定 emit 以持久记录真实调用边界；不触发自测。"""
        self.emit_self_test = emit

    def _fact(self, status: str, source: str, digest: str) -> dict:
        """返回夹具测试事实；status 控制状态，source 标明来源，digest 绑定当前候选。"""
        return {"status": status, "source": source, "candidate_sha256": digest,
                "tests_run": 0 if status in {"no_tests", "all_skipped", "error"} else 1,
                "exit_code": 0 if status in {"passed", "no_tests"} else 1,
                "selector": "testing/test_capture.py::test_capture_binary_output",
                "reason": "self_test_environment" if status == "error" else None}

    def public_self_test_facts(self) -> dict | None:
        """返回仍匹配当前候选内容的自测事实，旧回执或候选已被删除时为空。"""
        if self.fact and self.candidate.is_file() and sha256(self.candidate.read_bytes()).hexdigest() == self.fact["candidate_sha256"]:
            return self.fact.copy()
        return None

    async def run_public_self_test(self, *, source: str, timeout_seconds: float) -> dict:
        """记录 source 与正的 timeout_seconds，返回绑定真实测试候选的预定夹具状态。"""
        self.started.append((source, timeout_seconds))
        digest = sha256(self.candidate.read_bytes()).hexdigest()
        self.emit_self_test("public_self_test_started", {"source": source})
        self.fact = self._fact(self.status, source, digest)
        self.emit_self_test("public_self_test_finished", self.fact)
        return self.fact


class RequestLedger:
    """记录被真正接纳的模型请求，与离线 HTTP 调用和事件序号对账。"""

    def __init__(self) -> None:
        """初始化空预留列表，无副作用。"""
        self.authorized = []  # 按发生顺序记录轮号与用途，普通输入拒绝没有预留。

    def authorize(self, run_id: str, turn: int, purpose: str) -> None:
        """为 run_id 的第 turn 轮登记 purpose，返回 None。"""
        self.authorized.append((turn, purpose))

    def settle(self, run_id: str, turn: int, usage: dict | None, model: str | None,
               error_code: str | None = None) -> str | None:
        """接受 run_id/turn 的离线回执；模型名和用量仅用于兼容真实账本接口。"""
        return None

    def release_not_sent(self, run_id: str, turn: int, error_code: str) -> None:
        """本夹具不模拟已预留后未发出的请求；出现对此轮的释放即抛断言异常。"""
        raise AssertionError((run_id, turn, error_code))


@pytest.mark.parametrize("after_rejection,existing", [(False, False), (True, False), (True, True)])
def test_verified_root_handoff_after_rejection(tmp_path: Path, after_rejection: bool, existing: bool) -> None:
    """普通输入超限时只让候选策略收尾；existing 时复用同一候选的通过证据，无需补测。"""
    runs = tmp_path / ".runs"
    run = make_manifest(tmp_path, budget=48000)
    run.config.context = ContextPolicy(max_input_bytes=48000, root_handoff=True,
                                       root_handoff_after_input_rejection=after_rejection)
    run.config.tools.append("run_command")
    run.config.permission_policy.command = "allow"
    executor = PublicSelfTestExecutor(tmp_path, runs, run.config, "passed", existing=existing)
    ledger = RequestLedger()
    write = model_reply(tool_call("write", "write_file", {"path": "result.txt", "content": "changed"}),
                        reasoning="X" * 24000)
    close = model_reply(content='{"status":"ready","answer":"已完成公开自测。"}')
    outcome, requests, history = scripted_run(run, runs, [write, close], executor=executor, guard=ledger)
    checks = [event for event in history.events if event.type == "context_checked"]
    assert [(event.payload["phase"], event.payload["allowed"]) for event in checks[:2]] == [
        ("execution", True), ("execution", False)]
    assert checks[1].payload["input_bytes"] > run.config.context.max_input_bytes
    if not after_rejection:
        assert len(requests) == len(ledger.authorized) == 1
        assert outcome.result.stop_reason == "max_model_input_bytes"
        assert len(checks) == 2 and not executor.started
        return
    assert outcome.result.execution_status == "completed"
    assert outcome.result.model_turns == outcome.result.http_attempts == len(requests) == 2
    assert ledger.authorized == [(1, "execution"), (2, "execution")]
    assert len(checks) == 3 and checks[-1].payload["allowed"] and checks[-1].payload["phase"] == "handoff"
    starts = [event for event in history.events if event.type == "model_request_started"]
    assert [event.payload["input_check_seq"] for event in starts] == [checks[0].seq, checks[-1].seq]
    assert checks[-1].payload["model_turn"] == checks[1].payload["model_turn"] == starts[-1].payload["model_turn"] == 2
    assert bool(requests[0]["tools"]) and "tools" not in requests[-1]
    body = str(requests[-1]["messages"])
    assert "test_capture_binary_output" in body and "X" * 24000 not in body
    assert outcome.result.tool_calls == (1 if existing else 2)
    assert len(executor.started) == int(not existing)
    if executor.started:
        source, timeout = executor.started[0]
        assert source == "handoff" and 0 < timeout <= 30
    assert sum(event.type == "tool_requested" for event in history.events) == 1
    assert [event.payload["trigger"] for event in history.events
            if event.type in {"budget_handoff_started", "budget_handoff_finished"}] == ["input_rejected"] * 2


@pytest.mark.parametrize("status,expected", [("failed", "max_model_input_bytes"), ("no_tests", "max_model_input_bytes"),
                                               ("all_skipped", "max_model_input_bytes"), ("error", "self_test_environment"),
                                               ("no_tool_budget", "max_model_input_bytes"),
                                               ("time_budget", "max_model_input_bytes"),
                                               ("command_not_allowed", "max_model_input_bytes")])
def test_verified_root_handoff_does_not_send_without_valid_self_test(tmp_path: Path, status: str, expected: str) -> None:
    """从同一拒绝边界验证 status 各类不可信事实；expected 是应保留的真实停止原因。"""
    runs = tmp_path / ".runs"
    run = make_manifest(tmp_path, budget=48000)
    run.config.context = ContextPolicy(max_input_bytes=48000, root_handoff=True, root_handoff_after_input_rejection=True)
    run.config.tools.append("run_command")
    run.config.permission_policy.command = "allow"
    if status == "no_tool_budget":
        run.config.limits.max_tool_calls = 1
    elif status == "time_budget":
        run.config.limits.model_turn_timeout_seconds = run.config.limits.run_timeout_seconds
    elif status == "command_not_allowed":
        run.config.permission_policy.command = "ask"
    executor = PublicSelfTestExecutor(tmp_path, runs, run.config, status)
    ledger = RequestLedger()
    write = model_reply(tool_call("write", "write_file", {"path": "result.txt", "content": "changed"}),
                        reasoning="X" * 24000)
    outcome, requests, history = scripted_run(run, runs, [write], executor=executor, guard=ledger)
    assert outcome.result.stop_reason == expected
    assert len(requests) == outcome.result.http_attempts == 1 and ledger.authorized == [(1, "execution")]
    skipped, = [event.payload for event in history.events if event.type == "budget_handoff_skipped"]
    assert skipped["reason"] == {
        "failed": "self_test_not_passed", "no_tests": "self_test_not_passed", "all_skipped": "self_test_not_passed",
        "error": "self_test_environment", "no_tool_budget": "tool_budget_exhausted",
        "time_budget": "time_budget_exhausted", "command_not_allowed": "self_test_permission_denied",
    }[status]
    assert outcome.result.tool_calls == (1 if status in {"no_tool_budget", "time_budget", "command_not_allowed"} else 2)
    assert not any(event.type in {"budget_handoff_started", "execution_completed"} for event in history.events)


def test_verified_root_last_turn_uses_same_self_test_and_keeps_partial(tmp_path: Path) -> None:
    """对照组也在末轮收尾前核验自测；模型报告部分完成时保留限额终态。"""
    runs = tmp_path / ".runs"
    run = make_manifest(tmp_path)
    run.config.context.root_handoff = True
    run.config.limits.max_model_turns = 2
    run.config.tools.append("run_command")
    run.config.permission_policy.command = "allow"
    executor = PublicSelfTestExecutor(tmp_path, runs, run.config, "passed")
    write = model_reply(tool_call("write", "write_file", {"path": "result.txt", "content": "changed"}))
    close = model_reply(content='{"status":"partial","answer":"还需要项目方核对。"}')
    outcome, requests, history = scripted_run(run, runs, [write, close], executor=executor)
    assert outcome.result.stop_reason == "max_model_turns" and outcome.result.final_answer == "还需要项目方核对。"
    assert len(requests) == 2 and "tools" not in requests[-1] and len(executor.started) == 1
    done, = [event.payload for event in history.events if event.type == "budget_handoff_finished"]
    assert done["status"] == "partial" and done["trigger"] == "last_model_turn"


def test_verified_handoff_without_write_does_not_test_or_request(tmp_path: Path) -> None:
    """只有读回执的候选超限时仍保留原拒绝；不应启动补测或读取未来响应。"""
    runs = tmp_path / ".runs"
    run = make_manifest(tmp_path, budget=48000)
    run.config.context = ContextPolicy(max_input_bytes=48000, root_handoff=True, root_handoff_after_input_rejection=True)
    run.config.tools.append("run_command")
    run.config.permission_policy.command = "allow"
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    executor = PublicSelfTestExecutor(tmp_path, runs, run.config, "passed")
    read = model_reply(tool_call("read", "read_file", {"path": "source.txt"}), reasoning="X" * 24000)
    outcome, requests, history = scripted_run(run, runs, [read], executor=executor)
    assert outcome.result.stop_reason == "max_model_input_bytes" and len(requests) == 1 and not executor.started
    skipped, = [event.payload for event in history.events if event.type == "budget_handoff_skipped"]
    assert skipped["reason"] == "no_verified_write"


@pytest.mark.parametrize("condition", ["new_write", "tool_request"])
def test_verified_handoff_keeps_true_incomplete_reasons(tmp_path: Path, condition: str) -> None:
    """在收尾发单后模拟外部文件变化或工具请求；两类 condition 都不能发布可信完成。"""
    runs = tmp_path / ".runs"
    run = make_manifest(tmp_path, budget=48000)
    run.config.context = ContextPolicy(max_input_bytes=48000, root_handoff=True, root_handoff_after_input_rejection=True)
    run.config.tools.append("run_command")
    run.config.permission_policy.command = "allow"
    executor = PublicSelfTestExecutor(tmp_path, runs, run.config, "passed")
    requests = []  # 捕获紧凑请求发送边界，不允许测试夹具替代真实模型网关。
    write = model_reply(tool_call("write", "write_file", {"path": "result.txt", "content": "changed"}),
                        reasoning="X" * 24000)

    def handler(request: httpx.Request) -> httpx.Response:
        """接收 request 后才制造恰当的异常并构造离线 HTTP 回应；普通轮返回真实文件工具调用。"""
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return model_stream_response(200, json=write)
        assert len(requests) == 2 and "tools" not in body
        if condition == "new_write":
            (tmp_path / "result.txt").write_text("changed again outside the run", encoding="utf-8")
            return model_stream_response(200, json=model_reply(content='{"status":"ready","answer":"候选已验证。"}'))
        forbidden = tool_call("extra", "write_file", {"path": "extra.txt", "content": "should never execute"})
        return model_stream_response(200, json=model_reply(forbidden))

    outcome = asyncio.run(run_task(run, runs, "dummy-key", transport=httpx.MockTransport(handler), executor=executor))
    history = RunStore.read(runs, str(run.run_id))
    done, = [event.payload for event in history.events if event.type == "budget_handoff_finished"]
    assert len(requests) == outcome.result.http_attempts == 2 and outcome.result.tool_calls == 2
    assert outcome.result.execution_status == "limit_exceeded" and not (tmp_path / "extra.txt").exists()
    assert done["trigger"] == "input_rejected"
    if condition == "new_write":
        assert outcome.result.stop_reason == "max_model_input_bytes"
        assert done["status"] == "partial" and done["reason"] == "self_test_not_current"
        assert outcome.result.final_answer is None
    else:
        assert outcome.result.stop_reason == "handoff_tools_disabled"
        assert done["status"] == "invalid" and done["reason"] == "handoff_tools_disabled"


def test_budget_handoff_rechecks_queued_candidate_before_cost_reservation(tmp_path: Path, monkeypatch) -> None:
    """在紧凑输入已放行、模型获得槽位时修改 tmp_path 的候选，仍只让首轮预留费用。"""
    runs = tmp_path / ".runs"
    run = make_manifest(tmp_path, budget=48000)
    run.config.context = ContextPolicy(max_input_bytes=48000, root_handoff=True, root_handoff_after_input_rejection=True)
    run.config.tools.append("run_command")
    run.config.permission_policy.command = "allow"
    executor = PublicSelfTestExecutor(tmp_path, runs, run.config, "passed")
    ledger = RequestLedger()
    acquired = 0  # 模拟第一次用于执行，第二次收尾队列醒来前现场变化。

    @asynccontextmanager
    async def slot(kind: str):
        """为本测试申请 kind 槽位，第二次取得模型槽位前变更候选字节。"""
        nonlocal acquired
        assert kind == "models"
        acquired += 1
        if acquired == 2:
            (tmp_path / "result.txt").write_text("queued candidate became stale", encoding="utf-8")
        yield

    monkeypatch.setattr("agent_harness.runtime.model_gateway.capacity", slot)
    write = model_reply(tool_call("write", "write_file", {"path": "result.txt", "content": "changed"}),
                        reasoning="X" * 24000)
    outcome, requests, history = scripted_run(run, runs, [write], executor=executor, guard=ledger)
    assert acquired == 2 and len(requests) == outcome.result.http_attempts == 1
    assert ledger.authorized == [(1, "execution")]
    assert outcome.result.stop_reason == "max_model_input_bytes"
    skipped, = [event.payload for event in history.events if event.type == "budget_handoff_skipped"]
    assert skipped["reason"] == "self_test_not_current"
    assert skipped["trigger"] == "input_rejected"
