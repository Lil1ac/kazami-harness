"""区分用户 SIGINT、上游任务取消与网络内部泄漏的取消异常。"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx

from agent_harness.runner import run_task
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.store import RunStore


def _manifest(workspace: Path) -> RunManifest:
    """创建只读任务，使取消测试使用与真实 CLI 相同的运行契约。"""
    return RunManifest(run_id=uuid4(), config=RunConfig(), task=TaskSpec(prompt="读取 app.log", workspace=workspace))


def test_transport_cancellation_without_task_cancel_is_not_user_action(tmp_path: Path):
    """直接泄漏的取消异常应记为执行故障，并仅保存不含异常正文的安全栈。"""
    secret = "dummy-cancel-message-must-not-persist"
    value = _manifest(tmp_path)

    async def handler(request):
        """模拟底层传输抛出取消异常，但不向运行任务发送 cancel 请求。"""
        raise asyncio.CancelledError(secret)

    outcome = asyncio.run(run_task(value, tmp_path / "records", "dummy-key", transport=httpx.MockTransport(handler)))
    assert outcome.result.execution_status == "failed"
    assert outcome.result.stop_reason == "unexpected_cancellation"
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    diagnostic = next(event.payload for event in history.events if event.type == "cancellation_received")
    assert diagnostic["source"] == "unexpected_exception"
    assert diagnostic["task_cancelling"] == 0
    assert diagnostic["phase"] == "model_request"
    assert diagnostic["trace"]
    assert all(set(frame) == {"file", "function", "line"} for frame in diagnostic["trace"])
    assert secret not in (outcome.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert [event.type for event in history.events].count("run_finished") == 1


def test_upstream_task_cancel_has_neutral_source(tmp_path: Path):
    """上游调用 task.cancel 不能在缺少 SIGINT 证据时归因为用户操作。"""
    value = _manifest(tmp_path)

    async def scenario():
        """等到请求进入传输层再取消运行任务，确保重现真实等待中的取消。"""
        entered = asyncio.Event()

        async def handler(request):
            """挂起模拟 HTTP 请求，等待上游取消。"""
            entered.set()
            await asyncio.sleep(5)

        running = asyncio.create_task(run_task(value, tmp_path / "records", "dummy-key", transport=httpx.MockTransport(handler)))
        await entered.wait()
        running.cancel()
        return await running

    outcome = asyncio.run(scenario())
    assert outcome.result.execution_status == "cancelled"
    assert outcome.result.stop_reason == "task_cancelled"
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    diagnostic = next(event.payload for event in history.events if event.type == "cancellation_received")
    assert diagnostic["source"] == "task_cancel"
    assert diagnostic["task_cancelling"] >= 1


def test_real_cli_sigint_still_records_user_cancelled(tmp_path: Path):
    """在独立 CLI 进程中触发 SIGINT，验证 Ctrl+C 仍保存唯一终态并返回 130。"""
    script = '''
import asyncio
import signal
import sys
import httpx
from agent_harness import cli
from agent_harness.providers.deepseek import DeepSeekClient

original = DeepSeekClient.__init__

async def handler(request):
    """用真实 SIGINT 模拟用户在请求等待中按下 Ctrl+C。"""
    signal.raise_signal(signal.SIGINT)
    await asyncio.sleep(5)

def offline_init(self, *args, **kwargs):
    """只替换网络传输，保留 CLI、事件循环和运行收尾。"""
    kwargs['transport'] = httpx.MockTransport(handler)
    original(self, *args, **kwargs)

DeepSeekClient.__init__ = offline_init
raise SystemExit(cli.main(['run', '--task', '读取 app.log', '--workspace', sys.argv[1], '--runs-dir', sys.argv[2], '--json']))
'''
    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = "dummy-sigint-key"
    env["PYTHONUTF8"] = "1"
    records = tmp_path / "records"
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(records)], env=env, capture_output=True, encoding="utf-8", timeout=15)
    assert completed.returncode == 130, completed.stderr
    result = json.loads(completed.stdout)
    assert result["execution_status"] == "cancelled"
    assert result["stop_reason"] == "user_cancelled"
    history = RunStore.read(records, result["run_id"])
    diagnostic = next(event.payload for event in history.events if event.type == "cancellation_received")
    assert diagnostic["source"] == "user_sigint"
    assert [event.type for event in history.events].count("run_finished") == 1
