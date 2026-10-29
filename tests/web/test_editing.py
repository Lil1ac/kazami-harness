"""真实 API、局部工具、原目录、pytest 与重启接续的端到端回归。"""

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import httpx

from agent_harness.store import RunStore, StorageError
from tests.helpers.serve_editing_fixture import SOURCE, fixture_app


async def finish(client: httpx.AsyncClient, response: httpx.Response) -> dict:
    """接收已提交响应，在30秒内等到后台收尾结束；返回真实 RunView，不推断验收通过。"""
    assert response.status_code == 202, response.text
    async with asyncio.timeout(30):
        while True:
            view = (await client.get("/api/runs/" + response.json()["run_id"])).json()
            if not view["active"]:
                return view
            await asyncio.sleep(.01)


def test_local_editing_and_tests_continue_after_restart_without_overwriting_old_evidence(tmp_path: Path, monkeypatch):
    """两轮局部修改和真实测试通过，外部编辑与无关字节保留，旧产物/差异/事件保持。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-editing-tests")
    root = tmp_path / "fixture"
    app = fixture_app(root, tmp_path / "dist")
    selected = root / "局部编辑项目"
    index = (selected / ".git/index").read_bytes()
    user_note = (selected / "user-note.txt").read_bytes()

    async def scenario():
        """首轮从配置启用工具，重启后续轮继承权限并修改已生成测试，不重写旧记录。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finish(client, await client.post("/api/runs", json={
                    "project_id": app.state.fixture_project_id, "prompt": "修复加法函数并添加测试",
                    "config": {"tools": ["read_file", "search_files", "write_file", "edit_file", "run_command"]},
                }))
                assert first["result"]["execution_status"] == "completed", first
                assert first["result"]["validation_status"] == "passed", first
                assert first["execution_mode"] == "local" and Path(first["workspace"]) == selected
                assert (selected / "calculator.py").read_bytes() == SOURCE.encode("utf-8").replace(b"return a - b", b"return a + b")
                events = (await client.get("/api/runs/" + first["run_id"] + "/events", params={"limit": 200})).json()["items"]
                edits = [event for event in events if event["type"] == "tool_result" and event["payload"]["name"] == "edit_file"]
                assert len(edits) == 1 and edits[0]["payload"]["result"]["ok"]
                assert edits[0]["payload"]["result"]["data"]["replacements"] == 1
                reads = [event for event in events if event["type"] == "tool_result" and event["payload"]["name"] == "read_file"]
                assert reads[0]["payload"]["result"]["data"]["newline"] == "crlf"
                started = next(event for event in events if event["type"] == "tool_started" and event["payload"]["name"] == "edit_file")
                assert "\\n" in started["payload"]["arguments_json"] and "\\r" not in started["payload"]["arguments_json"]
                old_diff = (await client.get("/api/runs/" + first["run_id"] + "/diff")).json()
                old_artifact = (await client.get("/api/runs/" + first["run_id"] + "/artifacts", params={"location": "workspace", "path": "calculator.py", "preview": True})).json()
                files = ["run.json", "events.jsonl", "summary.json"]
                evidence = {name: hashlib.sha256((root / "runs" / first["run_id"] / name).read_bytes()).hexdigest() for name in files}
        # 两轮之间模拟用户在原文件尾部手工补充，第二轮必须读取当前版本后局部编辑。
        with (selected / "calculator.py").open("ab") as stream:
            stream.write("# 用户手动补充保持\r\n".encode("utf-8"))
        restarted = fixture_app(root, tmp_path / "dist")
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
                second = await finish(client, await client.post("/api/runs/" + first["run_id"] + "/followups", json={"prompt": "增加空值检查，补充 None 的测试", "request_id": str(uuid4())}))
                assert second["result"]["execution_status"] == "completed", second
                assert second["result"]["validation_status"] == "passed", second
                assert second["turn_index"] == 2 and second["workspace"] == first["workspace"]
                assert "edit_file" in second["config"]["tools"]
                assert "raise ValueError" in (selected / "calculator.py").read_text(encoding="utf-8")
                assert (selected / "calculator.py").read_bytes().endswith("# 用户手动补充保持\r\n".encode("utf-8"))
                assert SOURCE.split("\r\n\r\ndef untouched", 1)[1].encode("utf-8") in (selected / "calculator.py").read_bytes()
                code_bytes = (selected / "calculator.py").read_bytes()
                assert b"\n" not in code_bytes.replace(b"\r\n", b"")
                assert (await client.get("/api/runs/" + first["run_id"] + "/diff")).json() == old_diff
                assert (await client.get("/api/runs/" + first["run_id"] + "/artifacts", params={"location": "workspace", "path": "calculator.py", "preview": True})).json() == old_artifact
                assert evidence == {name: hashlib.sha256((root / "runs" / first["run_id"] / name).read_bytes()).hexdigest() for name in files}
                conversation = (await client.get("/api/runs/" + second["run_id"] + "/conversation")).json()
                assert len(conversation["turns"]) == 2
    asyncio.run(scenario())
    assert (selected / ".git/index").read_bytes() == index
    assert (selected / "user-note.txt").read_bytes() == user_note


def test_failed_start_event_does_not_execute_edit(tmp_path: Path, monkeypatch):
    """编辑开始事实未能落盘时，不执行副作用；模型的读取成功不能绕过事件持久化门槛。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-editing-tests")
    root = tmp_path / "fixture"
    app = fixture_app(root, tmp_path / "dist")
    original_append = RunStore.append

    def fail_edit_start(self, kind, payload=None):
        """只拒绝 edit_file 开始事件，其他真实存储保持，模拟写入工具前的记录失败。"""
        if kind == "tool_started" and payload and payload.get("name") == "edit_file":
            raise StorageError("模拟记录失败")
        return original_append(self, kind, payload)

    monkeypatch.setattr(RunStore, "append", fail_edit_start)

    async def scenario():
        """从真实 API 提交，等待后台退出；记录失败时保留未确认状态，不补造终态。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                view = await finish(client, await client.post("/api/runs", json={"project_id": app.state.fixture_project_id, "prompt": "修复加法函数并添加测试", "config": {"tools": ["read_file", "write_file", "edit_file", "run_command"]}}))
                assert view["interrupted"] and not view["active"]
                assert view["result"]["execution_status"] == "running"
                assert view["warnings"]
    asyncio.run(scenario())
    assert (root / "局部编辑项目/calculator.py").read_bytes() == SOURCE.encode("utf-8")
    assert not (root / "局部编辑项目/test_calculator.py").exists()
