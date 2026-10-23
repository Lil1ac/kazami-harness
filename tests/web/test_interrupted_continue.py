"""通过已有followups验证中断记录接续，不使用独立检查点入口。"""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.context import ContextBuilder
from agent_harness.providers.base import ModelResponse
from agent_harness.schemas import RunConfig, RunManifest, RunOrigin, TaskSpec, VerifierSpec
from agent_harness.sessions import SessionError, history_messages
from agent_harness.store import RunStore
from agent_harness.tools.registry import ToolExecutor
from agent_harness.web.api import create_app
from agent_harness.workspace import Workspace
from continuation_support import project, idle


class ContinueModel:
    """只根据真实继承消息完成本轮，记录请求以核对未重放已完成工具。"""

    def __init__(self):
        """初始化请求列表，不运行模型服务或操作文件。"""
        self.requests = []  # 每次complete实际收到的完整输入。

    async def complete(self, messages, tools):
        """保留messages供外部断言；返回最终回答，让真实宿主验收检查继承代码。"""
        self.requests.append(messages)
        return ModelResponse(message={"role": "assistant", "content": "基于已有修改继续完成。"}, tool_calls=[], finish_reason="stop", response_model="offline-continue")


async def interrupted_run(manager, *, project_mode=True, unresolved=False):
    """创建真实工作区和已保存执行前缀；unresolved表示末批命令开始但无结果，不运行该命令。"""
    identifier = uuid4()
    if project_mode:
        preset = manager.web_config.projects[0]
        review = manager.projects.prepare(preset, identifier)
        root = Path(review.worktree)
        verifier = VerifierSpec(argv=preset.test_argv, cwd=root, timeout_seconds=preset.test_timeout_seconds, python_paths=[root / value for value in preset.test_python_paths])
        origin = RunOrigin(kind="project", preset_id=preset.id, registered_path=preset.path)
    else:
        preset = manager.web_config.workspaces[0]
        root = preset.path
        verifier = None
        origin = RunOrigin(kind="workspace", preset_id=preset.id, registered_path=preset.path)
    manifest = RunManifest(run_id=identifier, session_id=identifier, task=TaskSpec(prompt="修复加法并保留已有修改", workspace=root, verifier=verifier), config=manager.run_config, origin=origin)
    store = RunStore.create(manager.runs_root, manifest)
    builder = ContextBuilder(Workspace(root, manager.runs_root), manifest.task, manifest.config)
    executor = ToolExecutor(root, manager.runs_root, manifest.config)
    executor.bind_run(store.run_dir)
    store.append("context_prepared", {"instruction": builder.instruction.model_dump(mode="json"), "serialization": "json_utf8_v1"})
    store.append("run_started", {"messages": builder.initial_messages(), "tools": executor.definitions, "conversation_start": 1})
    arguments = json.dumps({"path": "app.py", "content": "def add(a, b):\n    return a + b\n", "overwrite": True})
    call = {"id": "saved-write", "type": "function", "function": {"name": "write_file", "arguments": arguments}}
    store.append("assistant_message", {"message": {"role": "assistant", "content": None, "tool_calls": [call]}})
    store.append("tool_started", {"call_id": "saved-write", "name": "write_file", "arguments_json": arguments})
    result = await executor.execute("write_file", arguments)
    assert result["ok"], result
    store.append("tool_result", {"call_id": "saved-write", "name": "write_file", "result": result})
    if unresolved:
        calls = [{"id": value, "type": "function", "function": {"name": "run_command", "arguments": '{"argv":["python","not-run.py"]}'}} for value in ("unknown-command", "unstarted-command")]
        store.append("assistant_message", {"message": {"role": "assistant", "content": None, "tool_calls": calls}})
        store.append("tool_started", {"call_id": "unknown-command", "name": "run_command", "arguments_json": calls[0]["function"]["arguments"]})
    return identifier, store


@pytest.mark.parametrize("project_mode", [True, False])
@pytest.mark.parametrize("unresolved", [False, True])
def test_send_continue_inherits_interrupted_record_and_current_code(project, project_mode, unresolved):
    """Git与普通目录均可直接发送继续；保留原记录、标记未知工具，正常测试且请求不重复执行。"""
    async def scenario():
        """复用真实API和Runner，固定模型仅用于核对接续机制，不代表模型修复能力。"""
        model = ContinueModel()
        app = create_app(project, RunConfig(tools=["write_file", "read_file", "run_command"]), project.project_dir / "records", model_client=model)
        async with app.router.lifespan_context(app):
            manager = app.state.manager
            identifier, store = await interrupted_run(manager, project_mode=project_mode, unresolved=unresolved)
            before = {name: hashlib.sha256((store.run_dir / name).read_bytes()).hexdigest() for name in ("run.json", "events.jsonl")}
            history = RunStore.read(manager.runs_root, str(identifier))
            with pytest.raises(SessionError):
                history_messages(history)
            inherited = history_messages(history, allow_interrupted=True)
            if unresolved:
                outcomes = {message["tool_call_id"]: json.loads(message["content"]) for message in inherited if message["role"] == "tool"}
                assert outcomes["unknown-command"]["error"]["code"] == "tool_execution_unknown"
                assert outcomes["unstarted-command"]["error"]["code"] == "tool_not_started"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False) as client:
                conversation = (await client.get(f"/api/runs/{identifier}/conversation")).json()
                assert conversation["can_continue"], conversation
                assert "继续" in conversation["continuation_notice"]
                assert not (store.run_dir / "checkpoints").exists()
                body = {"request_id": str(uuid4()), "prompt": "继续"}
                first, duplicate = await asyncio.gather(*[client.post(f"/api/runs/{identifier}/followups", json=body) for _ in range(2)])
                assert first.status_code == duplicate.status_code == 202, (first.text, duplicate.text)
                assert first.json()["run_id"] == duplicate.json()["run_id"]
                final = await idle(client, first.json()["run_id"])
                assert final["result"]["execution_status"] == "completed", final
                assert final["result"]["tool_calls"] == 0
                assert final["turn_index"] == 2
                assert len(model.requests) == 1
                assert [item["content"] for item in model.requests[0] if item["role"] == "user"] == ["修复加法并保留已有修改", "继续"]
                assert "必须先核对当前文件" in json.dumps(model.requests[0], ensure_ascii=False)
                if project_mode:
                    assert final["result"]["validation_status"] == "passed", final
                    assert "return a + b" in (Path(final["workspace"]) / "app.py").read_text()
                final_manifest = RunStore.read(manager.runs_root, final["run_id"]).manifest
                assert final_manifest.history_source.interrupted is True
                assert not (manager.runs_root / final["run_id"] / "checkpoints").exists()
                conversation = (await client.get(f"/api/runs/{identifier}/conversation")).json()
                assert len(conversation["turns"]) == 2
                assert conversation["turns"][0]["interrupted"] is True
                assert conversation["turns"][1]["prompt"] == "继续"
                old = await client.get(f"/api/runs/{identifier}/recovery")
                assert old.status_code == 404
                old = await client.post(f"/api/runs/{identifier}/resumes", json={"request_id": str(uuid4()), "checkpoint_id": str(uuid4())})
                assert old.status_code == 404
            assert all(hashlib.sha256((store.run_dir / name).read_bytes()).hexdigest() == value for name, value in before.items())
    asyncio.run(scenario())


def test_damaged_interrupted_record_still_blocks_continue(project):
    """记录损坏时拒绝继承，不把任意坏前缀补成合法会话。"""
    async def scenario():
        """在专属测试前缀附加坏行，核对GET和POST均不启动模型。"""
        model = ContinueModel()
        app = create_app(project, RunConfig(tools=["write_file"]), project.project_dir / "records", model_client=model)
        async with app.router.lifespan_context(app):
            identifier, store = await interrupted_run(app.state.manager)
            with (store.run_dir / "events.jsonl").open("ab") as output:
                output.write(b"broken\n")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False) as client:
                view = (await client.get(f"/api/runs/{identifier}/conversation")).json()
                assert not view["can_continue"]
                response = await client.post(f"/api/runs/{identifier}/followups", json={"request_id": str(uuid4()), "prompt": "继续"})
                assert response.status_code == 409
            assert model.requests == []
    asyncio.run(scenario())


def test_git_continue_with_windows_checkout_policy(project):
    """Windows换行配置下普通接续同样保留已有修改，不能在补丁应用阶段丢失代码。"""
    import subprocess

    root = project.projects[0].path
    # 明确建立LF提交，再以autocrlf=true派生CRLF工作树，避免只测到原提交就为CRLF的情况。
    for name in ("app.py", "check.py", ".gitignore"):
        path = root / name
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    subprocess.run(["git", "-C", str(root), "add", "app.py", "check.py", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "LF baseline"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "true"], check=True)
    test_send_continue_inherits_interrupted_record_and_current_code(project, True, False)
