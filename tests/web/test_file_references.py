"""以真实队列、模型输入和运行记录验证项目文件引用。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx

from agent_harness.permissions.models import PermissionPolicy
from tests.web.test_jobs_api import application, wait_job
from tests.web.test_planning_api import ScriptModel, reply


async def preview(client: httpx.AsyncClient, target: dict, path: str, start: int | None = None, end: int | None = None) -> dict:
    """通过实际 HTTP 预览 target 的 path；start/end 为包含两端的行号，返回服务端版本及正文。"""
    response = await client.post("/api/file-references/preview", json={
        "target": target, "selection": {"path": path, "start_line": start, "end_line": end},
    })
    assert response.status_code == 200, response.text
    return response.json()


def selection(snapshot: dict) -> dict:
    """从 snapshot 取浏览器允许提交的字段；模型正文由宿主重新读取。"""
    return {key: snapshot[key] for key in ("path", "start_line", "end_line", "version")}


def last_model_reference(messages: list[dict]) -> dict:
    """从真实模型输入 messages 解码本轮用户资料；返回第一项引用。"""
    user = next(message for message in reversed(messages) if message["role"] == "user")
    return json.loads(user["content"].split("【用户引用的项目文件】\n", 1)[1])[0]


def test_reference_persists_at_admission_and_history_uses_the_frozen_range(tmp_path: Path) -> None:
    """预览后改动会拒绝旧版本；入队后改动仍执行同一版本，续轮和历史各按真实来源读取。"""

    async def scenario() -> None:
        """从普通目录预览、入队、修改原文和续聊；用离线模型捕获两次真实请求。"""
        model = ScriptModel([reply(text="第一轮回答"), reply(text="第二轮回答")])
        app, workspace = application(tmp_path, model)
        source = workspace / "说明.txt"
        source.write_bytes("头部\r\n已审阅的第二行\r\n尾部\r\n".encode())
        target = {"workspace_id": "files"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                old = (await preview(client, target, "说明.txt", 2, 2))["snapshot"]
                source.write_bytes("头部\r\n确认后版本\r\n尾部\r\n".encode())
                body = {"request_id": str(uuid4()), "create": {
                    "workspace_id": "files", "prompt": "按所选行处理", "file_references": [selection(old)],
                }}
                rejected = await client.post("/api/jobs", json=body)
                assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "reference_changed"
                assert not model.requests
                current = (await preview(client, target, "说明.txt", 2, 2))["snapshot"]
                body["create"]["file_references"] = [selection(current)]
                app.state.manager.jobs.paused = True
                queued = (await client.post("/api/jobs", json=body)).json()
                assert queued["status"] == "queued" and "payload" not in queued
                source.write_bytes("现场第一行\r\n现场第二行\r\n".encode())
                assert (await client.post("/api/jobs", json=body)).json()["id"] == queued["id"]
                assert (await client.post("/api/queue/resume")).status_code == 200
                assert (await wait_job(client, queued["id"]))["status"] == "completed"
                assert last_model_reference(model.requests[0]["messages"])["content"] == "确认后版本\r\n"
                first = queued["run_id"]
                saved = (await client.get(f"/api/runs/{first}/file-references/0")).json()
                assert saved == current
                live = (await preview(client, {"run_id": first}, "说明.txt", 1, 1))["snapshot"]
                request_id = str(uuid4())
                followup = (await client.post("/api/jobs", json={
                    "request_id": request_id, "parent_run_id": first, "followup": {
                        "request_id": request_id, "prompt": "核对实际目录", "file_references": [selection(live)],
                    },
                })).json()
                assert (await wait_job(client, followup["id"]))["status"] == "completed"
                assert last_model_reference(model.requests[1]["messages"])["content"] == "现场第一行\r\n"
                turns = (await client.get(f"/api/runs/{first}/conversation")).json()["turns"]
                assert [turn["file_references"] for turn in turns] == [[selection(current)], [selection(live)]]
                assert turns[0]["prompt"] == "按所选行处理"
                assert (await client.get(f"/api/runs/{first}/file-references/0")).json() == current
    asyncio.run(scenario())


def test_reference_preview_respects_read_roots_and_attachment_budget(tmp_path: Path) -> None:
    """直接调用预览也不能读取受限路径，长文件可选行段，预算不足时不会建立作业。"""

    async def scenario() -> None:
        """构造一个可读子目录及不可读旁支，核对宿主限制和实际请求结果。"""
        model = ScriptModel([])
        app, workspace = application(tmp_path, model)
        app.state.manager.run_config = app.state.manager.run_config.model_copy(update={
            "permission_policy": PermissionPolicy(read_roots=["docs"]),
        })
        (workspace / "docs").mkdir()
        (workspace / "private.txt").write_text("权限外", encoding="utf-8")
        (workspace / "docs" / "long.txt").write_bytes(b"first\n" + b"x" * (33 * 1024) + b"\n")
        target = {"workspace_id": "files"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                listing = await client.post("/api/file-references/list", json={"target": target})
                assert listing.json()["entries"] == [{"path": "docs", "kind": "directory"}]
                denied = await client.post("/api/file-references/preview", json={
                    "target": target, "selection": {"path": "private.txt"},
                })
                assert denied.json()["error"]["code"] == "reference_read_denied"
                traversal = await client.post("/api/file-references/preview", json={
                    "target": target, "selection": {"path": "docs/../private.txt"},
                })
                assert traversal.status_code == 422
                long = await preview(client, target, "docs/long.txt")
                assert long["snapshot"] is None and long["line_count"] == 2
                short = (await preview(client, target, "docs/long.txt", 1, 1))["snapshot"]
                assert short["content"] == "first\n"
                rejected = await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {
                    "workspace_id": "files", "prompt": "读取选择", "file_references": [selection(short)],
                    "config": {"context": {"max_input_bytes": 32}},
                }})
                assert rejected.json()["error"]["code"] == "reference_input_budget"
                assert not (await client.get("/api/jobs")).json()["items"] and not model.requests
    asyncio.run(scenario())
