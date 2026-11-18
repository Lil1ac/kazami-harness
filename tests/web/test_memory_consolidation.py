"""通过真实 HTTP、SQLite、队列和 Runner 验证跨会话记忆闭环。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import httpx

from tests.web.test_jobs_api import wait_job
from tests.web.test_memory_api import application, run
from tests.web.test_planning_api import call, reply


class ConsolidationModel:
    """用确定性模型验证真实请求中的证据、发布后的采用及取消边界。"""

    def __init__(self, *, block: bool = False, retry_invalid: bool = False):
        """block=True 等待取消；retry_invalid=True 在首次请求返回错误来源以验证校验修复。"""
        self.block = block  # 用于验证整理不会占用项目源码锁。
        self.retry_invalid = retry_invalid  # 首次模型输出使用不存在的证据短编号。
        self.started = asyncio.Event()  # 收到一次真实整理请求后置位。
        self.dreams = 0  # 已启动的整理请求数，不含普通运行。

    async def complete(self, messages, tools):
        """从 messages 读取受控证据，按宿主工具接口返回草案或实际文件调用。"""
        prompt = messages[-1]["content"] or ""
        if messages[0]["content"].startswith("你是项目记忆整理器"):
            assert tools == []
            self.dreams += 1
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            rows = json.loads(messages[1]["content"])["historical_evidence"]
            refs = [row["id"] for row in rows if row["kind"] == "user" and "V2" in row["text"]]
            assert refs
            if self.retry_invalid and self.dreams == 1:
                refs = ["E999"]
            if self.retry_invalid and self.dreams > 1:
                assert "evidence_ids" in messages[-1]["content"]
            result = {"changes": [{"action": "add", "topic": "退款导出", "reason": "两次明确确认",
                "evidence_ids": refs, "content": {"type": "decision", "title": "V2 退款金额",
                    "details": {"topic": "退款格式", "choice": "保留两位小数且退款为负", "rationale": "兼容账单接口"},
                    "tags": ["退款", "V2"]}}]}
            return replace(reply(text=json.dumps(result, ensure_ascii=False)), usage={"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        if messages[-1]["role"] == "tool" or not prompt.startswith("实现"):
            return reply(text="已记录项目决定。")
        return reply(call("write_file", path="result.json", overwrite=True,
                          content='{"amount":"-1.25"}' if any("退款为负" in (message.get("content") or "") for message in messages[:-1]) else '{"status":"needs_confirmation"}'))


async def enqueue(client, run_ids, identifier=None):
    """经 client 选择 run_ids 发起整理，identifier 可复用以验证幂等，返回队列编号。"""
    identifier = identifier or str(uuid4())
    response = await client.post("/api/projects/one/memory-consolidations", json={"request_id": identifier, "run_ids": run_ids})
    assert response.status_code == 202, response.text
    assert response.json()["id"] == identifier
    return identifier


def test_draft_review_sources_and_later_run(tmp_path: Path):
    """两个历史 Run 形成草案，发布前不可用、发布后新轮采用，隔离与撤回即时生效。"""
    async def scenario():
        """用离线供应商走接纳、发布及真实产物检查，返回 None。"""
        model = ConsolidationModel(retry_invalid=True)
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = await run(client, "确认 V2 金额为两位小数字符串")
            second = await run(client, "确认 V2 退款为负")
            library = (await client.get("/api/projects/one/memory-library")).json()
            sources = [first["run_id"], second["run_id"]]
            assert {item["run_id"] for item in library["sources"]} == set(sources)
            identifier = await enqueue(client, sources)
            await enqueue(client, sources, identifier)
            assert (await wait_job(client, identifier))["status"] == "completed"
            assert model.dreams == 2  # 一次校验失败及一次格式修复，同一作业没有重复接纳。
            url = "/api/projects/one/memory-consolidations/" + identifier
            draft = (await client.get(url)).json()
            assert draft["state"] == "draft" and draft["usage"]["total_tokens"] == 250
            references = draft["changes"][0]["evidence_ids"]
            assert len(references) == 2
            assert (await client.get("/api/projects/two/memory-evidence/" + references[0])).status_code == 404
            source = await client.get("/api/projects/one/memory-evidence/" + references[0])
            assert source.status_code == 200 and source.json()["run_id"] in sources
            assert (await client.get("/api/projects/one/memories?status=active")).json()["items"] == []
            decision = {"request_id": str(uuid4()), "selected": [0]}
            approved = await client.post(url + "/review", json=decision)
            assert approved.status_code == 200, approved.text
            assert (await client.post(url + "/review", json=decision)).json() == approved.json()
            assert (await client.get("/api/projects/one/memory-library")).json()["topics"][0]["title"] == "退款导出"
            used = await run(client, "实现 V2 退款")
            assert json.loads((tmp_path / "one/result.json").read_text()) == {"amount": "-1.25"}
            assert used["context"]["memory"]["uses"][0]["topics"][0]["title"] == "退款导出"
            item = used["context"]["memory"]["selected"][0]
            assert {ref["run_id"] for ref in item["source"]["refs"]} == set(sources)
            removed = await client.patch("/api/projects/one/memories/" + item["id"],
                json={"request_id": str(uuid4()), "action": "delete", "expected_revision": item["revision"]})
            assert removed.status_code == 200
            assert (await client.get("/api/projects/one/memory-evidence/" + references[0])).status_code == 404
            assert (await client.get("/api/runs/" + used["run_id"])).json()["context"]["memory"] == used["context"]["memory"]
    asyncio.run(scenario())


def test_queued_consolidation_cancel_does_not_lock_source(tmp_path: Path):
    """整理等待模型时同项目普通运行可执行；停止保留状态且不发布任何条目。"""
    async def scenario():
        """启动离线应用并并行执行普通 Run，返回 None。"""
        model = ConsolidationModel(block=True)
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            source = await run(client, "确认 V2 金额为两位小数")
            identifier = await enqueue(client, [source["run_id"]])
            async with asyncio.timeout(10):
                await model.started.wait()
            assert (await run(client, "普通检查"))["result"]["execution_status"] == "completed"
            stopped = await client.post("/api/jobs/" + identifier + "/cancel")
            assert stopped.json()["status"] == "cancelled"
            assert (await client.get("/api/projects/one/memories?status=active")).json()["items"] == []
    asyncio.run(scenario())


def test_stale_draft_and_trashed_sources(tmp_path: Path):
    """编辑后拒绝基线过期的发布，回收后的源证据和草案正文均不可访问。"""
    async def scenario():
        """用真实管理接口改变版本及会话状态，返回 None。"""
        app, _ = application(tmp_path, ConsolidationModel())
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            source = await run(client, "确认 V2 退款格式")
            run_id = source["run_id"]
            identifier = await enqueue(client, [run_id])
            assert (await wait_job(client, identifier))["status"] == "completed"
            url = "/api/projects/one/memory-consolidations/" + identifier
            saved = await client.post("/api/projects/one/memories", json={"request_id": str(uuid4()), "type": "fact",
                "title": "样例事实", "details": {"subject": "样例", "attribute": "版本", "value": "2"}})
            assert saved.status_code == 200
            decision = {"request_id": str(uuid4()), "selected": [0]}
            assert (await client.post(url + "/review", json=decision)).json()["error"]["code"] == "memory_revision_conflict"
            info = (await client.get("/api/conversations/" + run_id)).json()
            removed = await client.patch("/api/conversations/" + run_id, json={"request_id": str(uuid4()),
                "expected_revision": info["revision"], "action": "trash"})
            assert removed.status_code == 200, removed.text
            detail = (await client.get(url)).json()
            assert detail["state"] == "source_unavailable" and not detail["changes"]
            cleared = await client.request("DELETE", "/api/conversations/" + run_id, json={"request_id": str(uuid4()),
                "expected_revision": removed.json()["revision"], "confirm": True})
            assert cleared.status_code == 200 and cleared.json()["state"] == "purged", cleared.text
            assert (await client.get("/api/projects/one/memory-library")).json()["sources"] == []
            assert app.state.manager.jobs.get(identifier)["memory_draft"]["snapshot"] is None
    asyncio.run(scenario())


def test_idle_consolidation_only_acts_after_opt_in_and_reconciles(tmp_path: Path):
    """显式开启后只整理一次新历史；重启时已认领的中断不会重发或要求核对源码。"""
    async def scenario():
        """依次使用两个真实宿主读取同一数据库，返回 None。"""
        model = ConsolidationModel()
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            await run(client, "确认 V2 金额约定")
            assert model.dreams == 0
            result = await client.put("/api/projects/one/memory-consolidation-settings", json={"idle_enabled": True})
            assert result.json()["idle_enabled"]
            async with asyncio.timeout(10):
                await model.started.wait()
            job = next(job for job in (await client.get("/api/jobs")).json()["items"] if job.get("kind") == "memory_consolidation")
            assert (await wait_job(client, job["id"]))["status"] == "completed"
            assert model.dreams == 1
        # 固定一个中断前的检查点，模拟进程退出后只有请求启动回执留盘。
        interrupted = app.state.manager.jobs.get(job["id"])
        interrupted.update(status="running", memory_draft={"state": "running", "model_calls": 1, "usage": {}, "changes": []})
        app.state.manager.jobs.save(interrupted)
        resumed_model = ConsolidationModel()
        restarted, _ = application(tmp_path, resumed_model)
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
            restored = (await client.get("/api/jobs/" + job["id"])).json()
            assert restored["status"] == "failed" and restored["memory_state"] == "interrupted"
            assert restored["memory_usage_complete"] is False
            assert resumed_model.dreams == 0
    asyncio.run(scenario())
