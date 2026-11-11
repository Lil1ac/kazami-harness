"""真实命令、HTTP 收件箱和安全边界回归，验证副作用及预算不会因异步接续重复。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import UUID, uuid4, uuid5

import httpx

from agent_harness.runtime.runner import run_task
from agent_harness.runtime.control import ControlStore
from agent_harness.schemas import RunManifest, TaskSpec
from agent_harness.storage.runs import RunStore
from agent_harness.execution.commands import command_records, output_page
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.helpers.permissions import trusted_config
from tests.web.test_planning_api import ScriptModel, call, reply, finished
from tests.web.job_helpers import submit_run, submit_followup


class GatedModel(ScriptModel):
    """在指定模型请求返回前等待测试释放，以真实事件控制输入和暂停的竞争时机。"""

    def __init__(self, responses, gate_turn=1):
        """responses 为有限响应，gate_turn 从 1 计数；构造不请求模型或执行工具。"""
        super().__init__(responses)
        self.gate_turn = gate_turn  # 需要拦住的唯一实际请求编号。
        self.ready = asyncio.Event()  # 已到达被测竞态边界。
        self.release = asyncio.Event()  # 测试明确释放后才返回响应。

    async def complete(self, messages, tools):
        """记录 messages/tools 并在 gate_turn 阻塞；释放后返回父类的真实脚本响应。"""
        if len(self.requests) + 1 == self.gate_turn:
            self.ready.set()
            await self.release.wait()
        return await super().complete(messages, tools)


def web_setup(tmp_path, monkeypatch, model):
    """为 tmp_path 创建独立可信本机应用；monkeypatch 只设置离线密钥，返回应用及工作目录。"""
    monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", "offline-long-running")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("源内容", encoding="utf-8")
    config = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist",
                       workspaces=[WebWorkspace(id="sample", label="长任务", path=workspace)])
    return create_app(config, trusted_config(tools=["read_file", "write_file", "run_command"]), tmp_path / "records", model_client=model), workspace


def test_background_command_blocks_writes_and_finalizes_before_artifacts(tmp_path):
    """后台返回句柄；活动命令阻止同目录写入，等待后可写入且命令结束先于产物快照。"""
    async def scenario():
        """运行有限离线模型及真实 Python 子进程，返回 None，以日志和磁盘内容核验。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="生成长任务产物", workspace=workspace),
                               config=trusted_config(tools=["run_command", "write_file"]))
        start = call("run_command", argv=[sys.executable, "-c", "import time; from pathlib import Path; print('中文输出',flush=True); time.sleep(.3); Path('from-command.txt').write_text('done')"], background=True)
        identifier = str(uuid5(manifest.run_id, "1:" + start.call_id))
        model = ScriptModel([reply(start, call("write_file", path="blocked.txt", content="不应写入")),
                             reply(call("wait_command", command_id=identifier)),
                             reply(call("write_file", path="after.txt", content="已结束")), reply(text="完成")])
        root = tmp_path / "records"
        result = await run_task(manifest, root, "offline-key", model_client=model)
        assert result.result.execution_status == "completed"
        assert not (workspace / "blocked.txt").exists()
        assert (workspace / "from-command.txt").read_text() == "done"
        assert (workspace / "after.txt").read_text(encoding="utf-8") == "已结束"
        history = RunStore.read(root, str(manifest.run_id))
        record = command_records(history)[0]
        assert record["status"] == "completed" and record["result"]["exit_code"] == 0
        assert "中文输出" in output_page(result.run_dir, record, "stdout", 0, 65536)["content"]
        snapshots = [event.seq for event in history.events if event.type == "command_snapshot"]
        assert snapshots[-1] < next(event.seq for event in history.events if event.type == "artifact_collection_started")
        blocked = [event for event in history.events if event.type == "tool_result" and event.payload["name"] == "write_file"][0]
        assert blocked.payload["result"]["error"]["code"] == "workspace_command_active"
    asyncio.run(scenario())


def test_steering_skips_stale_tool_batch_and_is_durable_and_idempotent(tmp_path, monkeypatch):
    """模型已生成旧参数时接收补充，旧写入被宿主跳过；重复提交只保存一次且历史可重建。"""
    async def scenario():
        """通过真实 HTTP 在模型响应前投递纠偏，返回 None，断言实际副作用及消费者回执。"""
        model = GatedModel([reply(call("write_file", path="stale.txt", content="旧要求")), reply(text="遵循新要求")])
        app, workspace = web_setup(tmp_path, monkeypatch, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await submit_run(client, json={"workspace_id": "sample", "prompt": "执行任务"})
            identifier = response.json()["run_id"]
            await asyncio.wait_for(model.ready.wait(), 5)
            body = {"request_id": str(uuid4()), "prompt": "不要创建 stale.txt", "delivery": "steer"}
            first = await client.post(f"/api/runs/{identifier}/inputs", json=body)
            assert first.status_code == 202, first.text
            assert (await client.post(f"/api/runs/{identifier}/inputs", json=body)).json()["id"] == body["request_id"]
            assert (await client.post(f"/api/runs/{identifier}/inputs", json={**body, "delivery": "next"})).status_code == 409
            model.release.set()
            view = await finished(client, response)
            assert view["result"]["execution_status"] == "completed"
            assert not (workspace / "stale.txt").exists()
            state = (await client.get(f"/api/runs/{identifier}/runtime")).json()["state"]
            assert len(state["messages"]) == 1 and state["messages"][0]["status"] == "processed"
            assert any("不要创建 stale.txt" in str(item) for item in model.requests[-1]["messages"])
            assert (await client.delete(f"/api/runs/{identifier}/inputs/{body['request_id']}")).status_code == 409
            from agent_harness.storage.sessions import history_messages
            assert history_messages(RunStore.read(tmp_path / "records", identifier))
    asyncio.run(scenario())


def test_pause_resume_keeps_budget_and_old_evidence(tmp_path, monkeypatch):
    """暂停不执行已生成的旧调用，明确恢复创建新段，预算累计且旧事件字节保持。"""
    async def scenario():
        """控制模型返回边界并通过原作业队列恢复；返回 None，核对检查点和运行消费。"""
        model = GatedModel([reply(call("write_file", path="stale.txt", content="不执行")),
                            reply(call("write_file", path="resumed.txt", content="已恢复")), reply(text="完成")])
        app, workspace = web_setup(tmp_path, monkeypatch, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await submit_run(client, json={"workspace_id": "sample", "prompt": "执行长任务"})
            identifier = response.json()["run_id"]
            await asyncio.wait_for(model.ready.wait(), 5)
            next_input = {"request_id": str(uuid4()), "prompt": "待恢复后决定的下一轮", "delivery": "next"}
            assert (await client.post(f"/api/runs/{identifier}/inputs", json=next_input)).status_code == 202
            pause = await client.post(f"/api/runs/{identifier}/pause", json={"request_id": str(uuid4())})
            assert pause.status_code == 202 and pause.json()["status"] == "pausing"
            model.release.set()
            old = await finished(client, response)
            assert old["result"]["stop_reason"] == "user_paused"
            state = (await client.get(f"/api/runs/{identifier}/runtime")).json()["state"]
            assert state["status"] == "paused" and state["checkpoint"]["spent_model_turns"] == 1
            assert state["checkpoint"]["spent_seconds"] == old["result"]["active_duration_seconds"]
            assert not (workspace / "stale.txt").exists()
            path = tmp_path / "records" / identifier / "events.jsonl"
            original = path.read_bytes()
            wrong = await submit_followup(client, f"/api/runs/{identifier}/followups", json={"request_id": str(uuid4()), "prompt": "继续"})
            assert wrong.status_code == 409
            model.gate_turn = 2
            model.ready.clear()
            model.release.clear()
            following = await submit_followup(client, f"/api/runs/{identifier}/followups", json={"request_id": str(uuid4()), "prompt": "恢复暂停前任务，先核对当前代码与未完成项，不重放旧工具。", "resume": True})
            await asyncio.wait_for(model.ready.wait(), 5)
            carried = (await client.get(f"/api/runs/{following.json()['run_id']}/runtime")).json()["state"]["messages"]
            assert any(item["id"] == next_input["request_id"] for item in carried)
            assert (await client.delete(f"/api/runs/{identifier}/inputs/{next_input['request_id']}")).status_code == 409
            assert (await client.delete(f"/api/runs/{following.json()['run_id']}/inputs/{next_input['request_id']}")).status_code == 200
            model.release.set()
            latest = await finished(client, following)
            assert latest["result"]["permissions"]["spent_model_turns"] == 1
            assert latest["result"]["model_turns"] == 2
            assert (workspace / "resumed.txt").exists()
            assert path.read_bytes() == original
    asyncio.run(scenario())


def test_next_input_dispatches_once_after_normal_finish(tmp_path, monkeypatch):
    """下一轮要求在本轮完成后自动交给原队列，重复提交不增加会话运行。"""
    async def scenario():
        """使用真实队列等待两段完成，返回 None，核对新要求及作业编号绑定。"""
        model = GatedModel([reply(text="首轮完成"), reply(call("write_file", path="next.txt", content="下一轮")), reply(text="后续完成")])
        app, workspace = web_setup(tmp_path, monkeypatch, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await submit_run(client, json={"workspace_id": "sample", "prompt": "首轮"})
            identifier = response.json()["run_id"]
            await asyncio.wait_for(model.ready.wait(), 5)
            body = {"request_id": str(uuid4()), "prompt": "生成 next.txt", "delivery": "next"}
            assert (await client.post(f"/api/runs/{identifier}/inputs", json=body)).status_code == 202
            model.release.set()
            async with asyncio.timeout(15):
                while not (workspace / "next.txt").exists():
                    await asyncio.sleep(.02)
            await app.state.manager.wait_idle()
            state = (await client.get(f"/api/runs/{identifier}/runtime")).json()["state"]
            assert state["messages"][0]["job_id"] == body["request_id"]
            assert (await client.post(f"/api/runs/{identifier}/inputs", json=body)).status_code == 202
            session = app.state.manager.conversations.store.read(UUID(identifier))
            assert len(session.run_ids) == 2
    asyncio.run(scenario())


def test_cancel_collects_background_process_before_terminal_and_restart_is_read_only(tmp_path, monkeypatch):
    """取消根段会真实清理后台命令；重建服务只读取固定证据，不重启命令或模型。"""
    async def scenario():
        """在真实命令产生输出后取消；返回 None，检查进程结束、原始日志和重启读取。"""
        start = call("run_command", argv=[sys.executable, "-c", "import time; from pathlib import Path; print('READY',flush=True); time.sleep(20); Path('late.txt').write_text('bad')"], background=True)
        model = GatedModel([reply(start), reply(text="不应到达")], gate_turn=2)
        app, workspace = web_setup(tmp_path, monkeypatch, model)
        identifier = None
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await submit_run(client, json={"workspace_id": "sample", "prompt": "等待后台命令"})
            identifier = response.json()["run_id"]
            await asyncio.wait_for(model.ready.wait(), 5)
            async with asyncio.timeout(10):
                while True:
                    runtime = (await client.get(f"/api/runs/{identifier}/runtime")).json()
                    if runtime["commands"] and runtime["commands"][0]["status"] == "running":
                        command = runtime["commands"][0]
                        output = (await client.get(f"/api/runs/{identifier}/commands/{command['id']}/output")).json()
                        if "READY" in output["output"]["content"]:
                            break
                    await asyncio.sleep(.02)
            assert (await client.post(f"/api/runs/{identifier}/cancel")).status_code == 200
            view = await finished(client, response)
            assert view["result"]["execution_status"] == "cancelled"
            final = (await client.get(f"/api/runs/{identifier}/runtime")).json()["commands"][0]
            assert final["status"] == "cancelled" and final["cleanup_status"] == "complete"
            assert not (workspace / "late.txt").exists()
        path = tmp_path / "records" / identifier / "events.jsonl"
        original = path.read_bytes()
        dormant = ScriptModel([])
        restarted = create_app(app.state.manager.web_config, trusted_config(), tmp_path / "records", model_client=dormant)
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1") as client:
            history = (await client.get(f"/api/runs/{identifier}/runtime")).json()
            assert history["commands"][0] == final
            assert (await client.post(f"/api/runs/{identifier}/commands/{final['id']}/stop")).status_code == 409
            assert path.read_bytes() == original and not dormant.requests
    asyncio.run(scenario())


def test_restart_unknown_command_never_uses_old_pid_or_replays(tmp_path, monkeypatch):
    """缺命令终态的日志只显示待核对，旧 PID 不会成为新宿主的停止目标。"""
    async def scenario():
        """构造无真实进程的中断前缀再启动应用，返回 None，核对 GET/POST 不执行工具。"""
        model = ScriptModel([])
        app, workspace = web_setup(tmp_path, monkeypatch, model)
        manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="未知命令", workspace=workspace), config=trusted_config(tools=["run_command"]))
        store = RunStore.create(tmp_path / "records", manifest)
        ControlStore(tmp_path / "records").create(SimpleNamespace(manifest=manifest))
        command_id = str(uuid4())
        store.append("command_snapshot", {"command": {"id": command_id, "run_id": str(manifest.run_id), "status": "running", "pid": 1, "cleanup_status": "not_needed"}})
        original = (store.run_dir / "events.jsonl").read_bytes()
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.get(f"/api/runs/{manifest.run_id}/runtime")
            assert response.status_code == 200, response.text
            assert response.json()["state"]["status"] == "needs_review"
            assert response.json()["commands"][0]["status"] == "needs_review"
            assert (await client.post(f"/api/runs/{manifest.run_id}/commands/{command_id}/stop")).status_code == 409
            assert not model.requests
            assert (store.run_dir / "events.jsonl").read_bytes() == original
    asyncio.run(scenario())


def test_pause_resume_cannot_reset_exhausted_model_budget(tmp_path, monkeypatch):
    """暂停发生在末次请求时，恢复也不能得到新的模型轮次或再次执行旧工具。"""
    async def scenario():
        """把样例限制为一个模型请求后暂停恢复；返回 None，以请求数量核对累计限额。"""
        model = GatedModel([reply(call("write_file", path="never.txt", content="不执行"))])
        app, workspace = web_setup(tmp_path, monkeypatch, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await submit_run(client, json={"workspace_id": "sample", "prompt": "一次请求", "config": {"limits": {"max_model_turns": 1}}})
            identifier = response.json()["run_id"]
            await asyncio.wait_for(model.ready.wait(), 5)
            await client.post(f"/api/runs/{identifier}/pause", json={"request_id": str(uuid4())})
            model.release.set()
            previous = await finished(client, response)
            assert previous["result"]["stop_reason"] == "user_paused"
            following = await submit_followup(client, f"/api/runs/{identifier}/followups", json={"request_id": str(uuid4()), "prompt": "恢复", "resume": True})
            latest = await finished(client, following)
            assert latest["result"]["stop_reason"] == "max_model_turns"
            assert len(model.requests) == 1 and not (workspace / "never.txt").exists()
    asyncio.run(scenario())


def test_pause_resumes_readonly_child_with_history_and_remaining_budget(tmp_path, monkeypatch):
    """父暂停收拢只读子项；恢复保留旧段来源、跳过未执行调用并按子剩余额度继续。"""
    from agent_harness.runtime.runner import REQUEST_CONTEXT
    from tests.subagents.test_subagents import report

    class ChildModel:
        """按主/子身份分别响应，首个子请求只在用户暂停后释放。"""

        def __init__(self):
            """初始化有限主子请求及事件，返回 None，不执行文件操作。"""
            self.parent_calls = 0  # 主运行请求数，恢复不会重新委派已经接纳的工作。
            self.child_calls = 0  # 全部子段的实际请求数。
            self.entered = asyncio.Event()  # 首个子模型进入安全竞态窗口。
            self.release = asyncio.Event()  # 测试确认暂停后放行原响应。
            self.reading = call("read_file", path="source.txt")  # 接续新段的真实证据编号。

        async def complete(self, messages, tools):
            """依据可信上下文回复；messages/tools 为实际投影，返回有限响应且不模拟文件结果。"""
            context = REQUEST_CONTEXT.get()
            if context.manifest.delegation:
                self.child_calls += 1
                if self.child_calls == 1:
                    self.entered.set()
                    await self.release.wait()
                    return reply(call("read_file", path="source.txt"))
                if self.child_calls == 2:
                    assert context.manifest.delegation.previous_run_id is not None
                    assert context.result.permissions.spent_model_turns == 1
                    return reply(self.reading)
                return reply(text=report("已读取真实来源", ids=[self.reading.call_id]))
            self.parent_calls += 1
            if self.parent_calls == 1:
                return reply(call("delegate_readonly", title="读取来源", objective="核对 source.txt", role="explore", background=True))
            if self.parent_calls == 2:
                return reply(call("wait_subagents", timeout_seconds=30))
            return reply(text="已核对接续报告")

    async def scenario():
        """通过真实 HTTP 暂停并恢复父子执行；返回 None，核对新子段和累计预算。"""
        model = ChildModel()
        app, _ = web_setup(tmp_path, monkeypatch, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await submit_run(client, json={"workspace_id": "sample", "prompt": "研究来源"})
            identifier = response.json()["run_id"]
            await asyncio.wait_for(model.entered.wait(), 5)
            assert (await client.post(f"/api/runs/{identifier}/pause", json={"request_id": str(uuid4())})).status_code == 202
            model.release.set()
            previous = await finished(client, response)
            assert previous["result"]["stop_reason"] == "user_paused"
            child = previous["result"]["subagents"][0]
            assert child["status"] == "waiting_input" and child["tool_calls"] == 0
            following = await submit_followup(client, f"/api/runs/{identifier}/followups", json={"request_id": str(uuid4()), "prompt": "恢复研究", "resume": True})
            latest = await finished(client, following)
            assert latest["result"]["execution_status"] == "completed", latest["result"]
            resumed = latest["result"]["subagents"][0]
            assert resumed["status"] == "completed" and resumed["run_id"] != child["run_id"]
            assert model.child_calls == 3
    asyncio.run(scenario())
