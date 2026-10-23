"""真实Git、ASGI和Runner覆盖检查点恢复、幂等、证据与拒绝边界。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from uuid import UUID, uuid4

import httpx
import pytest

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.sessions import SessionStore, payload_digest
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class CheckpointModel:
    """先真实修复代码，再按开关阻塞模型或启动长命令，供取消和恢复测试。"""

    def __init__(self, *, block: bool = True, command: bool = False) -> None:
        """block控制完成前等待，command控制修复后的真实长命令；均不访问网络。"""
        self.block = block  # False时返回最终回答，恢复同一模型输入即可结束。
        self.command = command  # True在检查点后启动进程，用于副作用拒绝。
        self.entered = asyncio.Event()  # 检查点后的模型等待已进入。
        self.requests: list[list[dict]] = []  # 实际模型输入的完整独立副本。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """以最后用户要求后的工具结果决定写入或回答；返回真实工具调用，无网络请求。"""
        self.requests.append(deepcopy(messages))
        start = max(index for index, item in enumerate(messages) if item["role"] == "user")
        observations = [item for item in messages[start + 1:] if item["role"] == "tool"]
        calls = []
        if not observations:
            calls = [ToolCall("repair-add", "write_file", json.dumps({"path": "app.py", "content": 'def add(a, b):\n    """返回两数之和。"""\n    return a + b\n', "overwrite": True}, ensure_ascii=False))]
        elif self.command:
            calls = [ToolCall("unknown-command", "run_command", json.dumps({"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}))]
        elif self.block:
            self.entered.set()
            await asyncio.Event().wait()
        message = {"role": "assistant", "content": None if calls else "已修复并等待宿主验收。"}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-checkpoint")


def git(root: Path, *arguments: str) -> bytes:
    """在临时root执行固定Git参数并返回stdout；不用shell，失败终止测试。"""
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=10).stdout


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> WebConfig:
    """创建固定Git基线和真实宿主测试，并保留源仓库dirty文件供隔离断言。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-checkpoint-key")
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "check.py").write_text('from app import add\nassert add(2, 3) == 5\nprint("actual recovery test passed")\n', encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "checkpoint baseline")
    (root / "local.txt").write_text("保留源目录未提交内容", encoding="utf-8")
    workspace = tmp_path / "ordinary"
    workspace.mkdir()
    return WebConfig(project_dir=tmp_path, runtime_root=tmp_path / "runtime/copies", frontend_dist=tmp_path / "dist", workspaces=[WebWorkspace(id="ordinary", label="普通", path=workspace)], projects=[ProjectPreset(id="code", label="检查点测试", path=root, test_argv=[sys.executable, "check.py"], test_timeout_seconds=10)])


@asynccontextmanager
async def application(config: WebConfig, model: CheckpointModel, run_config: RunConfig | None = None):
    """按config和离线model启动真实应用，产出HTTP客户端及管理器；退出等待执行清理。"""
    app = create_app(config, run_config or RunConfig(tools=["write_file", "read_file", "run_command"]), config.project_dir / "records", model_client=model)
    app.state.manager._legacy_checkpoint_mode = True  # 显式测试保留协议；默认正式入口关闭，由普通接续测试覆盖。
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False) as client:
            yield client, app.state.manager


async def idle(client: httpx.AsyncClient, run_id: str) -> dict:
    """最多20秒等待run_id结束及代码收尾，返回最终API视图；超时使测试失败。"""
    async with asyncio.timeout(20):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            view = response.json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)


async def cancelled(client: httpx.AsyncClient, model: CheckpointModel, **extra) -> dict:
    """提交Git任务并在安全检查点后的模型等待处取消，返回可信取消终态。"""
    response = await client.post("/api/runs", json={"prompt": "修复add函数", "project_id": "code", **extra})
    assert response.status_code == 202, response.text
    await asyncio.wait_for(model.entered.wait(), 15)
    await client.post(f'/api/runs/{response.json()["run_id"]}/cancel')
    result = await idle(client, response.json()["run_id"])
    assert result["result"]["execution_status"] == "cancelled", result
    return result


def evidence(root: Path, run_id: str) -> dict[str, str]:
    """计算run_id原始记录和封存检查点全部SHA，确认恢复与读取不改旧证据。"""
    folder = root / run_id
    paths = [folder / name for name in ("run.json", "events.jsonl", "summary.json")]
    paths.extend((folder / "checkpoints").glob("*"))
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_resume_git_isolated_budget_and_attempts_then_followup(project: WebConfig):
    """真实取消后从页面API恢复，核验旧证据不变、同轮聚合、累计预算及新轮序号。"""
    async def scenario():
        """保留真实代码后恢复并重新验收，重复请求不执行，再追加新用户要求。"""
        model = CheckpointModel()
        source_status = git(project.projects[0].path, "status", "--porcelain=v1")
        async with application(project, model) as (client, manager):
            first = await cancelled(client, model)
            before = evidence(manager.runs_root, first["run_id"])
            ready = (await client.get(f'/api/runs/{first["run_id"]}/recovery')).json()
            assert ready["supported"] and ready["can_resume"], ready
            assert ready["checkpoint"]["model_turns"] == 2
            body = {"request_id": str(uuid4()), "checkpoint_id": ready["checkpoint"]["checkpoint_id"]}
            model.block = False
            responses = await asyncio.gather(*[client.post(f'/api/runs/{first["run_id"]}/resumes', json=body) for _ in range(2)])
            assert all(response.status_code == 202 for response in responses), [response.text for response in responses]
            assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
            second = await idle(client, responses[0].json()["run_id"])
            assert second["result"]["execution_status"] == "completed", second
            assert second["result"]["validation_status"] == "passed", second
            assert second["attempt_index"] == 2 and second["turn_index"] == 1
            assert second["parent_run_id"] == first["run_id"] and second["session_id"] == first["run_id"]
            assert second["resume_source"]["source_run_id"] == first["run_id"]
            assert second["result"]["resume_budget"]["model_turns"] == 2
            assert second["result"]["resume_budget"]["tool_calls"] == 1
            assert not second["result"]["resume_budget"]["usage_complete"]
            assert second["result"]["tool_calls"] == 0
            assert Path(second["workspace"]) != Path(first["workspace"])
            assert "return a + b" in (Path(second["workspace"]) / "app.py").read_text(encoding="utf-8")
            assert evidence(manager.runs_root, first["run_id"]) == before
            assert [item["content"] for item in model.requests[-1] if item["role"] == "user"] == ["修复add函数"]
            conversation = (await client.get(f'/api/runs/{first["run_id"]}/conversation')).json()
            assert conversation["latest_run_id"] == second["run_id"]
            assert len(conversation["turns"]) == 1 and len(conversation["turns"][0]["attempts"]) == 2
            assert conversation["turns"][0]["run_id"] == second["run_id"]
            count = len(model.requests)
            duplicate = await client.post(f'/api/runs/{first["run_id"]}/resumes', json=body)
            assert duplicate.status_code == 202 and duplicate.json()["run_id"] == second["run_id"]
            assert len(model.requests) == count
            stale = (await client.get(f'/api/runs/{first["run_id"]}/recovery')).json()
            assert not stale["can_resume"] and stale["blocked_code"] == "recovery_stale_attempt"
            completed = (await client.get(f'/api/runs/{second["run_id"]}/recovery')).json()
            assert not completed["can_resume"]
            response = await client.post(f'/api/runs/{second["run_id"]}/followups', json={"request_id": str(uuid4()), "prompt": "保留修改再检查"})
            assert response.status_code == 202, response.text
            third = await idle(client, response.json()["run_id"])
            assert third["turn_index"] == 2 and third["attempt_index"] == 1 and third["resume_source"] is None
            conversation = (await client.get(f'/api/runs/{third["run_id"]}/conversation')).json()
            assert len(conversation["turns"]) == 2 and len(conversation["turns"][0]["attempts"]) == 2
            assert git(project.projects[0].path, "status", "--porcelain=v1") == source_status
    asyncio.run(scenario())


def test_restart_read_has_no_git_and_committed_request_stays_idempotent(project: WebConfig, monkeypatch):
    """跨应用重启读取不调用Git或模型，已提交恢复请求再次提交仍返回原尝试。"""
    async def scenario():
        """第一次取消，第二次读取及恢复，第三次重试同一持久请求，旧证据不变。"""
        model = CheckpointModel()
        async with application(project, model) as (client, manager):
            first = await cancelled(client, model)
            before = evidence(manager.runs_root, first["run_id"])
        resumed_model = CheckpointModel(block=False)
        async with application(project, resumed_model) as (client, manager):
            def forbidden(*args, **kwargs):
                """只读期间禁止任何Git调用；若访问到Git直接令测试失败。"""
                raise AssertionError("GET recovery must not invoke Git")
            with monkeypatch.context() as scoped:
                scoped.setattr(manager.projects, "_git", forbidden)
                response = await client.get(f'/api/runs/{first["run_id"]}/recovery')
                assert response.status_code == 200 and response.json()["can_resume"], response.text
                assert resumed_model.requests == []
            body = {"request_id": str(uuid4()), "checkpoint_id": response.json()["checkpoint"]["checkpoint_id"]}
            response = await client.post(f'/api/runs/{first["run_id"]}/resumes', json=body)
            assert response.status_code == 202, response.text
            second = await idle(client, response.json()["run_id"])
            assert second["result"]["validation_status"] == "passed"
        last_model = CheckpointModel(block=False)
        async with application(project, last_model) as (client, manager):
            response = await client.post(f'/api/runs/{first["run_id"]}/resumes', json=body)
            assert response.status_code == 202 and response.json()["run_id"] == second["run_id"]
            assert last_model.requests == [] and evidence(manager.runs_root, first["run_id"]) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["workspace", "checkpoint", "verifier"])
def test_changed_or_corrupt_source_rejected_before_model(project: WebConfig, damage: str):
    """代码、封存文件或宿主测试变动均拒绝恢复，不调用新模型、不改原始日志。"""
    async def scenario():
        """真实保存检查点后分别破坏来源，显式提交必须409并保留旧日志证据。"""
        model = CheckpointModel()
        async with application(project, model) as (client, manager):
            first = await cancelled(client, model)
            ready = (await client.get(f'/api/runs/{first["run_id"]}/recovery')).json()
            identifier = ready["checkpoint"]["checkpoint_id"]
            if damage == "workspace":
                (Path(first["workspace"]) / "app.py").write_text("# 用户的新修改", encoding="utf-8")
            elif damage == "checkpoint":
                (manager.runs_root / first["run_id"] / "checkpoints" / (identifier + ".json")).write_text("{}", encoding="utf-8")
            else:
                project.projects[0].test_argv = [sys.executable, "-c", "print('changed')"]
            before = evidence(manager.runs_root, first["run_id"])
            count = len(model.requests)
            response = await client.post(f'/api/runs/{first["run_id"]}/resumes', json={"request_id": str(uuid4()), "checkpoint_id": identifier})
            assert response.status_code == 409, response.text
            assert len(model.requests) == count and evidence(manager.runs_root, first["run_id"]) == before
    asyncio.run(scenario())


def test_exhausted_budget_and_unknown_tool_result_block_recovery(project: WebConfig):
    """累计模型额度耗尽及封存后的真实长命令取消均不可恢复，不回退旧工具副作用。"""
    async def scenario():
        """先耗尽两轮模型预算，再运行检查点后的真实长命令并取消，核验明确阻断。"""
        model = CheckpointModel()
        async with application(project, model) as (client, manager):
            first = await cancelled(client, model, config={"limits": {"max_model_turns": 2}})
            recovery = (await client.get(f'/api/runs/{first["run_id"]}/recovery')).json()
            assert not recovery["can_resume"] and recovery["blocked_code"] == "checkpoint_budget_exhausted", recovery
        model = CheckpointModel(command=True)
        async with application(project, model) as (client, manager):
            response = await client.post("/api/runs", json={"prompt": "修复并执行命令", "project_id": "code"})
            assert response.status_code == 202, response.text
            identifier = response.json()["run_id"]
            async with asyncio.timeout(15):
                while True:
                    events = (await client.get(f"/api/runs/{identifier}/events")).json()["items"]
                    if any(event["type"] == "tool_started" and event["payload"].get("name") == "run_command" for event in events):
                        break
                    await asyncio.sleep(0.01)
            await client.post(f"/api/runs/{identifier}/cancel")
            await idle(client, identifier)
            recovery = (await client.get(f"/api/runs/{identifier}/recovery")).json()
            assert not recovery["can_resume"] and recovery["blocked_code"] == "checkpoint_side_effect_uncertain", recovery
    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_resume_intent_is_not_replayed_after_restart(project: WebConfig, committed: bool):
    """恢复准备或提交后缺少运行记录的遗留请求均不重放模型或重新创建工作树。"""
    async def scenario():
        """手工登记持久恢复意图模拟准备边界崩溃，重启后相同请求始终拒绝自动重放。"""
        model = CheckpointModel()
        async with application(project, model) as (client, manager):
            first = await cancelled(client, model)
            ready = (await client.get(f'/api/runs/{first["run_id"]}/recovery')).json()
            body = {"request_id": str(uuid4()), "checkpoint_id": ready["checkpoint"]["checkpoint_id"]}
            digest = payload_digest({"kind": "resume", "source_run_id": first["run_id"], "checkpoint_id": body["checkpoint_id"]})
            store = SessionStore(manager.runs_root)
            store.begin(UUID(first["session_id"]), UUID(body["request_id"]), UUID(first["run_id"]), uuid4(), digest, first["prompt"], kind="resume", checkpoint_id=UUID(body["checkpoint_id"]), turn_index=1, attempt_index=2)
            if committed:
                store.commit(UUID(first["session_id"]), UUID(body["request_id"]))
        fresh = CheckpointModel(block=False)
        async with application(project, fresh) as (client, manager):
            response = await client.post(f'/api/runs/{first["run_id"]}/resumes', json=body)
            assert response.status_code == 409 and response.json()["error"]["code"] == "request_uncertain", response.text
            assert fresh.requests == []
            conversation = (await client.get(f'/api/runs/{first["run_id"]}/conversation')).json()
            assert not conversation["can_continue"]
            assert len(conversation["turns"]) == 1
            if committed:
                assert len(conversation["turns"][0]["attempts"]) == 2
    asyncio.run(scenario())


def test_workspace_task_reports_unsupported_without_checkpoint_or_execution(project: WebConfig):
    """普通工作区无恢复能力，读取接口不生成检查点或额外模型执行。"""
    async def scenario():
        """完成普通任务后多次读取恢复条件，始终返回受控不支持提示。"""
        model = CheckpointModel(block=False)
        async with application(project, model) as (client, manager):
            response = await client.post("/api/runs", json={"prompt": "写说明", "workspace_id": "ordinary"})
            view = await idle(client, response.json()["run_id"])
            count = len(model.requests)
            for _ in range(2):
                response = await client.get(f'/api/runs/{view["run_id"]}/recovery')
                assert response.status_code == 200
                assert response.json()["supported"] is False and response.json()["checkpoint"] is None
            assert len(model.requests) == count
    asyncio.run(scenario())


def test_cancel_waits_for_checkpoint_thread_before_run_finished(project: WebConfig, monkeypatch):
    """封存线程进行时取消会等待其退出，终态后不再出现悬空写入或模型调用。"""
    async def scenario():
        """在首次安全边界阻塞封存，发取消后释放线程，核验run_finished保持事件末项。"""
        model = CheckpointModel()
        async with application(project, model) as (client, manager):
            entered, release = threading.Event(), threading.Event()
            original = manager.recovery.checkpoints.capture

            def blocked_capture(*args, **kwargs):
                """收到原capture参数后等待release最多10秒，再原样封存并返回检查点。"""
                entered.set()
                assert release.wait(10), "测试未及时释放检查点封存线程"
                return original(*args, **kwargs)
            monkeypatch.setattr(manager.recovery.checkpoints, "capture", blocked_capture)
            response = await client.post("/api/runs", json={"prompt": "封存时取消", "project_id": "code"})
            assert response.status_code == 202, response.text
            identifier = response.json()["run_id"]
            try:
                async with asyncio.timeout(15):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                stopped = await client.post(f"/api/runs/{identifier}/cancel")
                assert stopped.status_code == 200 and stopped.json()["cancelling"]
            finally:
                release.set()
            view = await idle(client, identifier)
            assert view["result"]["execution_status"] == "cancelled" and model.requests == []
            events = (await client.get(f"/api/runs/{identifier}/events")).json()["items"]
            assert events[-1]["type"] == "run_finished"
            before = evidence(manager.runs_root, identifier)
            await asyncio.sleep(0.05)
            assert evidence(manager.runs_root, identifier) == before
    asyncio.run(scenario())
