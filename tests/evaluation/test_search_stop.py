"""使用实际 Windows 搜索子进程与本地 SSE 流验证停批准则。"""

import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
import pytest

from agent_harness.config import load_config
from agent_harness.evaluation.budget import BatchBudget, RESERVATION_PICOS
from agent_harness.evaluation.engine import evaluate
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.execution import search_worker
from agent_harness.execution.processes import ProcessRunner
from agent_harness.execution.scheduling import HostLimits, host_limits
from agent_harness.runtime.runner import run_task
from agent_harness.storage.runs import RunStore
from tests.evaluation.test_evaluation import suite_file
from tests.helpers.streaming import model_stream_response
from tests.tools.test_tool_workflow import _call, _manifest, _reply


def _still_running(pid: int) -> bool:
    """查询本测试创建的 pid 是否仍在运行；不向 PID 发送结束信号。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87  # 已回收的进程不再具有有效 PID。
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def replay_batch(root: Path, scenario: str, patch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """在 root 隔离运行 scenario（search、http 或 cleanup），返回评测报告与进程、回执证据。

    patch 仅替换测试 worker 启动参数或模拟清理证据丢失，保留真实进程树回收。
    模型请求全部经过 MockTransport，评分器为本地可信 Python 脚本。
    """
    root.mkdir(parents=True, exist_ok=True)
    suite = suite_file(root / scenario)
    single = json.loads(suite.read_text(encoding="utf-8"))
    single.update(name=f"离线搜索停批回放 {scenario}", cases=single["cases"][:1])
    suite.write_text(json.dumps(single, ensure_ascii=False), encoding="utf-8")
    runs_root = root / "r"
    pids = root / (scenario + ".pid")
    args = {"query": "needle", "glob": "**/*.txt"}

    def slow_worker() -> list[str]:
        """返回真实慢进程的 argv，读取宿主 stdin 后记下实际 PID，再阻塞等待取消。"""
        program = ("import os,sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
                   "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); time.sleep(30)")
        return [sys.executable, "-I", "-B", "-c", program, str(pids.resolve())]

    patch.setattr(search_worker, "worker_argv", slow_worker)
    if scenario == "cleanup":
        original_cleanup = ProcessRunner._cleanup

        async def unverifiable(self, process, deadline=None):
            """真实回收 process，再模拟没有取得确认回执；返回 unknown，触发停批。"""
            await original_cleanup(self, process, deadline)
            return "unknown"

        patch.setattr(ProcessRunner, "_cleanup", unverifiable)

    config = load_config(Path(__file__).resolve().parents[2] / "evals/swe-pilot/deepseek.toml")
    config.limits.run_timeout_seconds = 3.5
    config.tools = ["search_files", "write_file"]
    experiment = Experiment(groups=preset_groups(["hybrid", "hybrid_handoff"]), max_model_calls=60, max_cost_cents=100)
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        """记录真实网关发来的离线 request，首轮返回两个检索工具或挂起 HTTP；其余轮交付。"""
        requests.append(str(request.url))
        if len(requests) == 1 and scenario == "http":
            await asyncio.Event().wait()
        if len(requests) == 1:
            payload = _reply(_call("first", "search_files", args), _call("second", "search_files", args))
        elif len(requests) == 2:
            payload = _reply(_call("write", "write_file", {"path": "result.txt", "content": "ok", "overwrite": True}))
        else:
            payload = _reply(answer="已交付。")
        payload.update(model="deepseek-flash", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                       "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10})
        return model_stream_response(200, json=payload)

    report = await evaluate(suite, config, experiment, runs_root, root / "w", "offline-test-key",
                            transport=httpx.MockTransport(respond))
    first = report["trials"][0]
    history = RunStore.read(runs_root, first["run_id"])
    starts = [item for item in history.events if item.type == "tool_started"]
    receipts = [item.payload for item in history.events if item.type == "tool_result"]
    pid = int(pids.read_text(encoding="ascii")) if pids.exists() else None
    by_call = {item["call_id"]: item["result"] for item in receipts}
    activated = [item.payload["call_id"] for item in starts]
    http_attempts = [item.payload for item in history.events if item.type == "model_http_attempt"]
    start_time = next(item.elapsed_ms for item in history.events if item.type == "run_started")
    proof = {
        "report_id": report["id"], "first_run_id": first["run_id"], "baseline_timeout_seconds": 3.5,
        "cleanup_status": history.result.cleanup_status, "pid": pid, "pid_alive_after_run": _still_running(pid) if pid else None,
        "started_calls": activated, "receipts": by_call, "receipt_count": len(receipts),
        "tool_start_ms": [item.elapsed_ms - start_time for item in starts],
        "tool_result_ms": [item.elapsed_ms - start_time for item in history.events if item.type == "tool_result"],
        "cleanup_grace_seconds": config.limits.cleanup_grace_seconds,
        "http_attempts_first": http_attempts, "mock_http_requests": len(requests), "external_requests": 0,
        "trial_states": [trial["execution_status"] for trial in report["trials"]],
        "stop_reason": report["stop_reason"], "budget": report["budget"],
    }
    assert len(http_attempts) == 1 and first["execution_status"] in {"timed_out", "failed"}
    if scenario == "http":
        assert not receipts and len(requests) == 1 and not first["usage_complete"]
        assert report["state"] == "stopped" and report["stop_reason"] == "unknown_usage" and len(report["trials"]) == 1
        assert report["budget"]["requests"][0]["status"] == "unknown"
        assert report["budget"]["reserved_usd_picos"] == RESERVATION_PICOS
    else:
        assert pid is not None and proof["pid_alive_after_run"] is False
        assert proof["tool_result_ms"][0] < (3.5 + config.limits.cleanup_grace_seconds + 1.0) * 1000
        assert first["usage_complete"] and first["cost"]["complete"] and len(receipts) == 2 and activated == ["first"]
        assert by_call["first"]["error"]["code"] == "tool_timed_out"
        if scenario == "cleanup":
            assert by_call["first"]["data"]["cleanup_status"] == "unknown"
            assert report["state"] == "stopped" and report["stop_reason"] == "cleanup_unconfirmed" and len(report["trials"]) == 1
            assert first["grade"]["status"] == "not_scored" and len(requests) == 1
        else:
            assert by_call["first"]["data"]["cleanup_status"] == "complete"
            assert by_call["second"]["error"]["code"] == "skipped_timeout"
            assert report["state"] == "completed" and report["stop_reason"] is None and len(report["trials"]) == 2
            assert first["stop_reason"] == "run_timeout" and first["grade"]["status"] == "failed"
            assert report["trials"][1]["grade"]["status"] == "passed" and len(requests) == 3
    return report, proof


@pytest.mark.parametrize("scenario", ["search", "http", "cleanup"])
def test_local_timeout_vs_unknown_usage_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str):
    """验证 scenario 的独立评分和下一试次是否启动，且进程、用量和预留证据一致。"""
    asyncio.run(replay_batch(tmp_path, scenario, monkeypatch))


@pytest.mark.parametrize("capacity_available", [False, True])
def test_deadline_before_http_does_not_mark_usage_unknown(tmp_path: Path, capacity_available: bool):
    """模型槽位耗尽或预留落盘跨过截止时间时，无 HTTP 发送和悬空预留。"""
    config = load_config(Path(__file__).resolve().parents[2] / "evals/swe-pilot/deepseek.toml")
    value = _manifest(tmp_path).model_copy(update={"config": config})
    config.limits.run_timeout_seconds = 0.5
    budget_report = {}

    def delayed_save():
        """容量充足场景只在生成预留时占用时间，模拟同步落盘跨过截止时间。"""
        if capacity_available and budget_report["budget"]["requests"][-1]["status"] == "reserved":
            import time
            time.sleep(0.6)

    guard = BatchBudget(Experiment(max_cost_cents=100), budget_report, delayed_save)

    def unexpected(_: httpx.Request) -> httpx.Response:
        """调用到此表明截止门禁失效，直接让测试失败。"""
        pytest.fail("截止后不应发送 HTTP")

    async def scenario():
        """固定本次模型容量，返回 Run 结果并还原作用域。"""
        token = host_limits.set(HostLimits(asyncio.Semaphore(int(capacity_available)), asyncio.Semaphore(1)))
        try:
            return await run_task(value, tmp_path / "r", "offline-test-key", transport=httpx.MockTransport(unexpected),
                                  model_request_guard=guard)
        finally:
            host_limits.reset(token)

    outcome = asyncio.run(scenario())
    assert outcome.result.stop_reason == "run_timeout" and outcome.result.usage_complete
    assert outcome.result.http_attempts == 0 and guard.state["reserved_usd_picos"] == 0
    assert guard.stop_reason is None and guard.run_cost(str(value.run_id))["complete"]
    assert [row["status"] for row in guard.state["requests"]] == (["not_sent"] if capacity_available else [])

@pytest.mark.parametrize("occupied", [False, True])
def test_single_search_deadline_keeps_run_and_accounting_live(tmp_path: Path, monkeypatch, occupied: bool):
    """单次限时只结束当前搜索；占槽等待不创建进程，真实在途搜索能回收且保留已知用量。"""
    pid_file = tmp_path / "worker.pid"

    def slow_worker() -> list[str]:
        """返回读取私有 stdin 后长时间阻塞的本地进程，便于核验真实回收。"""
        code = ("import os,sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
                "Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii');time.sleep(30)")
        return [sys.executable, "-I", "-c", code, str(pid_file)]

    monkeypatch.setattr(search_worker, "worker_argv", slow_worker)
    value = _manifest(tmp_path, run_timeout_seconds=8.0, search_timeout_seconds=1.4)
    count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """发送一次搜索；后轮核对真实回执之后向模型循环返回完成。"""
        nonlocal count
        count += 1
        if count == 1:
            reply = _reply(_call("s", "search_files", {"query": "needle"}))
        else:
            receipt = json.loads(json.loads(request.content)["messages"][-1]["content"])
            assert receipt["error"]["code"] == "search_timeout"
            assert receipt["data"]["cleanup_status"] == ("not_needed" if occupied else "complete")
            reply = _reply(answer="可继续执行")
        reply["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return model_stream_response(200, json=reply)

    async def scenario():
        """按 occupied 保持或释放搜索槽位，复用宿主调度与真实文件工具。"""
        token = host_limits.set(HostLimits(asyncio.Semaphore(1), asyncio.Semaphore(0 if occupied else 1)))
        try:
            return await run_task(value, tmp_path / "r", "offline", transport=httpx.MockTransport(handler))
        finally:
            host_limits.reset(token)

    outcome = asyncio.run(scenario())
    history = RunStore.read(tmp_path / "r", str(value.run_id))
    assert outcome.result.execution_status == "completed" and outcome.result.usage_complete
    assert count == 2 and outcome.result.usage.total_tokens == 30
    assert len([event for event in history.events if event.type == "tool_started"]) == int(not occupied)
    assert pid_file.exists() is (not occupied)
    if pid_file.exists():
        assert not _still_running(int(pid_file.read_text(encoding="ascii")))

