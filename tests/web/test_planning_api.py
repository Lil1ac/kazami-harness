"""真实API、文件与事件验证Agent主动规划、持久等待、用户决定和受控接续。"""

from tests.web.job_helpers import submit_run, submit_followup

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.web.test_projects import project_config


PLAN = {"title": "交付结果文件", "summary": "读取需求并在当前目录生成结果。", "scope": ["result.txt"],
        "steps": ["生成并核对结果"], "verification": ["从网页查看本轮文件与固定下载"]}  # 模型提交正文，不携带批准状态。


def call(name: str, **arguments) -> ToolCall:
    """用name和业务arguments生成唯一调用；返回值交给真实工具执行器，不模拟文件副作用。"""
    return ToolCall(str(uuid4()), name, json.dumps(arguments, ensure_ascii=False))


def reply(*calls: ToolCall, text: str = "已完成") -> ModelResponse:
    """将calls或最终text封装为模型响应；返回标准消息及一致的工具调用编号。"""
    message = {"role": "assistant", "content": None if calls else text}
    if calls:
        message["tool_calls"] = [{"id": item.call_id, "type": "function", "function": {
            "name": item.name, "arguments": item.arguments_json}} for item in calls]
    return ModelResponse(message=message, tool_calls=list(calls), finish_reason="tool_calls" if calls else "stop", response_model="offline-planning")


class ScriptModel:
    """按预先给定响应驱动真实Runner；记录请求以核对等待、拒绝与重启不会调用模型。"""

    def __init__(self, responses: list[ModelResponse], *, gated: bool = False) -> None:
        """responses为本次服务的有限响应；gated=True在响应耗尽后阻塞，以便测试取消。"""
        self.responses = responses  # 尚未返回的模型响应，耗尽后测试会失败或明确阻塞。
        self.requests: list[dict] = []  # 保存完整输入，验证历史与工具结果配对。
        self.gated = gated  # 是否允许在脚本末尾等待取消。
        self.entered = asyncio.Event()  # 真正进入阻塞时置位，不用固定睡眠猜测。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """捕获messages/tools后返回下一项响应；脚本意外耗尽抛断言，避免测试假通过。"""
        self.requests.append(deepcopy({"messages": messages, "tools": tools}))
        if self.responses:
            return self.responses.pop(0)
        if self.gated:
            self.entered.set()
            await asyncio.Event().wait()
        raise AssertionError("模型收到非预期请求")


@pytest.fixture
def planning_config(tmp_path: Path, monkeypatch) -> WebConfig:
    """建立独立普通目录与离线密钥；返回配置，不执行用户项目或访问供应商网络。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-planning-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("原始内容", encoding="utf-8")
    return WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "worktrees", frontend_dist=tmp_path / "dist",
                     workspaces=[WebWorkspace(id="files", label="规划案例", path=workspace)])


def application(config: WebConfig, model: ScriptModel):
    """为config/model创建真实应用，记录保存在测试根records；返回值尚未进入生命周期。"""
    return create_app(config, RunConfig(tools=["read_file", "search_files", "write_file", "edit_file", "run_command"]),
                      config.project_dir / "records", model_client=model)


async def finished(client: httpx.AsyncClient, response: httpx.Response) -> dict:
    """核对response成功接纳并等待最多20秒收尾；返回真实RunView，失败保留响应原文。"""
    assert response.status_code == 202, response.text
    async with asyncio.timeout(20):
        while True:
            view = (await client.get("/api/runs/" + response.json()["run_id"])).json()
            if not view["active"]:
                return view
            await asyncio.sleep(.01)


def decision(view: dict, action: str) -> dict:
    """从view当前请求构造用户action载荷；请求编号固定一次，用于并发与网络重试验证。"""
    pending = view["result"]["planning"]["pending"]
    return {"request_id": str(uuid4()), "prompt": "用户明确" + action, "decision": {
        "interaction_id": pending["id"], "action": action, "plan_version": pending["plan_version"]}}


def saved_files(config: WebConfig, run_id: str) -> dict[str, str]:
    """返回run_id三份固定证据的SHA256；只读历史，不含可变会话索引。"""
    root = config.project_dir / "records" / run_id
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("run.json", "events.jsonl", "summary.json")}


def test_agent_enters_planning_waits_restarts_and_executes_only_approved_version(planning_config) -> None:
    """主动规划时所有写入口均被拒绝；问题与批准跨重启保留，批准重试只启动一轮。"""
    config = planning_config
    root = config.workspaces[0].path
    first_model = ScriptModel([
        reply(call("enter_plan_mode", reason="需要确定新功能需求"),
              call("write_file", path="forbidden.txt", content="不能写"),
              call("edit_file", path="source.txt", expected_sha256="0" * 64, old_text="原始内容", new_text="错误"),
              call("run_command", argv=[sys.executable, "-c", "from pathlib import Path; Path('command.txt').write_text('bad')"])),
        reply(call("ask_user_question", question="结果使用哪种格式？", options=["纯文本", "JSON"]),
              call("write_file", path="late.txt", content="等待后也不能写")),
    ])

    async def scenario():
        """逐次明确发送回答与批准；服务重新建立后GET只读，工具与代码使用真实实现。"""
        app = application(config, first_model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "增加结果生成功能"}))
                assert first["result"]["execution_status"] == "waiting_input"
                assert first["result"]["planning"]["phase"] == "planning"
                assert first["result"]["validation_status"] == "unverified"
                events = (await client.get("/api/runs/" + first["run_id"] + "/events")).json()["items"]
                errors = [item["payload"]["result"]["error"]["code"] for item in events
                          if item["type"] == "tool_result" and not item["payload"]["result"]["ok"]]
                assert errors == ["planning_read_only"] * 3 + ["skipped_user_input"]
                assert (await client.get("/api/options")).json()["busy_run_id"] is None
                assert len(first_model.requests) == 2
                original = saved_files(config, first["run_id"])
        model = ScriptModel([reply(call("submit_plan", **PLAN)),
                             reply(call("write_file", path="result.txt", content="真实交付")), reply()])
        restarted = application(config, model)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
                loaded = (await client.get("/api/runs/" + first["run_id"])).json()
                assert loaded["result"]["planning"] == first["result"]["planning"] and not model.requests
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json=decision(first, "answer")))
                assert second["result"]["planning"]["pending"]["kind"] == "plan_approval"
                assert sorted(item.name for item in root.iterdir()) == ["source.txt"]
                bad = decision(second, "approve")
                bad["decision"]["plan_version"] = 2
                rejected = await submit_followup(client, "/api/runs/" + second["run_id"] + "/followups", json=bad)
                assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "plan_version_conflict"
                approved = decision(second, "approve")
                response = await submit_followup(client, "/api/runs/" + second["run_id"] + "/followups", json=approved)
                final = await finished(client, response)
                repeated = await submit_followup(client, "/api/runs/" + second["run_id"] + "/followups", json=approved)
                assert repeated.status_code == 202 and repeated.json()["run_id"] == final["run_id"]
                assert final["result"]["execution_status"] == "completed"
                assert final["result"]["planning"]["plans"][-1]["status"] == "approved"
                assert final["result"]["planning"]["plans"][-1]["steps"][0]["status"] == "pending"  # 模型未更新工作项，文件写入不冒充步骤完成。
                assert (root / "result.txt").read_text(encoding="utf-8") == "真实交付"
                assert saved_files(config, first["run_id"]) == original
                assert len(model.requests) == 3
                statuses = (await client.get("/api/conversations", params={"status": "completed"})).json()
                assert statuses["total"] == 1
                assert (root / "source.txt").read_text(encoding="utf-8") == "原始内容"
    asyncio.run(scenario())


def test_plain_approval_revises_plan_and_rejection_runs_no_model(planning_config) -> None:
    """普通文字无法批准；新版使旧请求失效，明确拒绝不调用模型，之后仍能重新规划。"""
    config = planning_config
    model = ScriptModel([reply(call("submit_plan", **PLAN)),
                         reply(call("write_file", path="forbidden.txt", content="错误")),
                         reply(call("submit_plan", **{**PLAN, "summary": "修改后的完整方案"})),
                         reply(call("ask_user_question", question="重新规划时需要哪种结果？"))])

    async def scenario():
        """通过当前API处理修订、过期批准与拒绝，核对模型计数和原目录零写入。"""
        app = application(config, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "先规划", "mode": "plan"}))
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json={"request_id": str(uuid4()), "prompt": "批准"}))
                assert second["result"]["planning"]["plans"][-1]["version"] == 2
                assert not (config.workspaces[0].path / "forbidden.txt").exists()
                stale = await submit_followup(client, "/api/runs/" + second["run_id"] + "/followups", json=decision(first, "approve"))
                assert stale.status_code == 409 and stale.json()["error"]["code"] == "interaction_stale"
                count = len(model.requests)
                third = await finished(client, await submit_followup(client, "/api/runs/" + second["run_id"] + "/followups", json=decision(second, "reject")))
                assert third["result"]["stop_reason"] == "plan_rejected" and len(model.requests) == count
                assert third["result"]["verification"] is None
                fourth = await finished(client, await submit_followup(client, "/api/runs/" + third["run_id"] + "/followups", json={"request_id": str(uuid4()), "prompt": "重新规划"}))
                assert fourth["result"]["execution_status"] == "waiting_input"
                assert len(model.requests) == count + 1
                assert any(item.get("content") == "先规划" for item in model.requests[-1]["messages"])
    asyncio.run(scenario())


def test_cancelled_planning_cannot_be_unlocked_by_auto_mode(planning_config) -> None:
    """规划被取消后继承只读状态；改选auto不能越过计划批准，未知动作不会重放。"""
    config = planning_config
    model = ScriptModel([reply(call("enter_plan_mode", reason="先研究复杂实现"))], gated=True)

    async def scenario():
        """取消真实阻塞中的Runner，然后另建服务继续并尝试写入；检查工具拒绝和项目原文。"""
        app = application(config, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                response = await submit_run(client, json={"workspace_id": "files", "prompt": "实现复杂功能"})
                await asyncio.wait_for(model.entered.wait(), 10)
                await client.post("/api/runs/" + response.json()["run_id"] + "/cancel")
                first = await finished(client, response)
                assert first["result"]["execution_status"] == "cancelled"
        continued = ScriptModel([reply(call("write_file", path="forbidden.txt", content="错误")), reply(text="尚未获得批准")])
        restarted = application(config, continued)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json={"request_id": str(uuid4()), "prompt": "继续", "mode": "auto"}))
                assert second["result"]["planning"]["phase"] == "planning"
                assert second["result"]["stop_reason"] == "planning_complete"
                assert not (config.workspaces[0].path / "forbidden.txt").exists()
    asyncio.run(scenario())


def test_cancel_question_prevents_late_answer_and_preserves_history(planning_config) -> None:
    """取消问题等待不调用模型；旧问题的迟到回答被拒绝，历史请求仍保留可读。"""
    config = planning_config
    model = ScriptModel([reply(call("ask_user_question", question="需要哪种业务规则？"))])

    async def scenario():
        """先保存问题再明确取消，之后尝试旧答复；核对零新调用及最新取消事实。"""
        app = application(config, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "先询问业务规则", "mode": "plan"}))
                original = saved_files(config, first["run_id"])
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json=decision(first, "cancel")))
                assert second["result"]["execution_status"] == "cancelled"
                assert second["result"]["stop_reason"] == "interaction_cancelled"
                assert len(model.requests) == 1
                late = await submit_followup(client, "/api/runs/" + second["run_id"] + "/followups", json=decision(first, "answer"))
                assert late.status_code == 409 and late.json()["error"]["code"] == "interaction_stale"
                assert saved_files(config, first["run_id"]) == original
    asyncio.run(scenario())


def test_interrupted_wait_rebuilds_request_without_replaying_tools(planning_config) -> None:
    """模拟待批准事件保存后缺失终态；重启重建原请求，读取与回答不重放历史动作。"""
    config = planning_config
    model = ScriptModel([reply(call("submit_plan", **PLAN))])

    async def scenario():
        """仅裁剪测试专属运行的结束事件与缓存，保留真实规划状态事件，再显式批准接续。"""
        app = application(config, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "提交方案", "mode": "plan"}))
        directory = config.project_dir / "records" / first["run_id"]
        lines = (directory / "events.jsonl").read_bytes().splitlines(keepends=True)
        (directory / "events.jsonl").write_bytes(b"".join(line for line in lines if json.loads(line)["type"] != "run_finished"))
        (directory / "summary.json").unlink()
        continued = ScriptModel([reply(call("write_file", path="result.txt", content="已核对后执行")), reply()])
        restarted = application(config, continued)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
                view = (await client.get("/api/runs/" + first["run_id"])).json()
                assert view["interrupted"] and view["result"]["planning"]["pending"] == first["result"]["planning"]["pending"]
                assert not continued.requests
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json=decision(view, "approve")))
                assert second["result"]["execution_status"] == "completed"
                assert len(continued.requests) == 2
    asyncio.run(scenario())


def test_host_required_policy_guards_auto_entry_and_cannot_be_overridden(planning_config) -> None:
    """宿主强制策略在Agent主动进入前就拒绝写入和命令；网页无权覆盖该配置。"""
    config = planning_config
    model = ScriptModel([reply(call("list_files"), call("write_file", path="early.txt", content="bad"),
                              call("run_command", argv=[sys.executable, "-c", "print('not allowed')"])),
                         reply(call("submit_plan", **PLAN))])

    async def scenario():
        """使用auto入口但强制先批准，核对列表真实返回、越界动作拒绝和配置覆盖422。"""
        app = create_app(config, RunConfig(planning_policy="required", tools=["read_file", "write_file", "run_command"]),
                         config.project_dir / "records", model_client=model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                invalid = await submit_run(client, json={"workspace_id": "files", "prompt": "越权", "config": {"planning_policy": "auto"}})
                assert invalid.status_code == 422 and not model.requests
                view = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "自动判断实现", "mode": "auto"}))
                observations = [json.loads(item["content"]) for item in model.requests[-1]["messages"] if item["role"] == "tool"]
                assert observations[0]["data"]["entries"] == [{"path": "source.txt", "kind": "file"}]
                assert [item["error"]["code"] for item in observations[1:]] == ["planning_read_only"] * 2
                assert view["result"]["stop_reason"] == "plan_approval"
                assert not (config.workspaces[0].path / "early.txt").exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["local", "isolated"])
def test_planning_skips_project_tests_until_explicit_approval(project_config, monkeypatch, mode: str) -> None:
    """真实Git的mode覆盖原目录与隔离；规划不执行可能写文件的项目测试，批准后才检查。"""
    config = project_config
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-planning-key")
    config.projects[0].test_argv = [sys.executable, "-c", "from pathlib import Path; Path('TEST_RAN').write_text('yes')"]
    original = (config.projects[0].path / "src/app.py").read_bytes()
    model = ScriptModel([reply(call("submit_plan", **PLAN)), reply(call("write_file", path="result.txt", content="通过批准")), reply()])

    async def scenario():
        """从页面API规划、批准并回读真实检查；隔离模式保持同一工作树及来源dirty字节。"""
        app = application(config, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"project_id": "backend", "prompt": "生成交付", "execution_mode": mode, "mode": "plan"}))
                assert not (Path(first["workspace"]) / "TEST_RAN").exists()
                assert first["result"]["verification_attempts"] == []
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json=decision(first, "approve")))
                assert first["workspace"] == second["workspace"]
                assert second["result"]["validation_status"] == "passed"
                assert (Path(second["workspace"]) / "TEST_RAN").read_text() == "yes"
                assert (config.projects[0].path / "src/app.py").read_bytes() == original
    asyncio.run(scenario())
