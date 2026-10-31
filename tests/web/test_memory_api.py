"""用真实 HTTP、队列、Runner 和文件工具验证项目长期记忆闭环。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
from uuid import uuid4

import httpx

from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.web.test_planning_api import call, reply, ScriptModel



class MemoryModel:
    """读取实际输入中的知识决定输出，学习请求通过真实候选工具执行。"""

    def __init__(self):
        """初始化空请求记录；无网络及宿主文件访问，返回 None。"""
        self.requests = []  # 实际发送的消息与工具，用于检查预算和权限。

    async def complete(self, messages, tools):
        """根据本轮 messages 执行学习或生成 JSON；tools 只供检查，真实文件由 Runner 写入。"""
        self.requests.append(deepcopy({"messages": messages, "tools": tools}))
        user_index = max(index for index, message in enumerate(messages) if message["role"] == "user")
        prompt = messages[user_index]["content"]
        if any(message.get("role") == "tool" for message in messages[user_index + 1:]):
            return reply(text="本轮已处理，执行与检查分别展示。")
        if prompt.startswith("学习"):
            return reply(call("memory_propose", title="接口兼容决定", content="接口返回保留 legacy_name 字段，客户升级后再删除。", type="decision", details={"topic": "接口兼容", "choice": "保留 legacy_name", "rationale": "客户仍未升级"}))
        initial = "\n".join(message.get("content") or "" for message in messages[:user_index] if message.get("role") == "user")
        match = re.search(r"\n(\[.*\])\n</project_memory>", initial)
        selected = json.loads(match[1]) if match else []
        known = any("legacy_name" in item["usage_text"] for item in selected)
        return reply(call("write_file", path="result.json", content=json.dumps({"field": "legacy_name" if known else "need_user_decision"}), overwrite=True))


def application(tmp_path: Path, model, config: RunConfig | None = None):
    """创建两个互相隔离的普通项目，注入 model，返回应用和目录配置；config=None 使用默认。"""
    for name in ("one", "two"):
        (tmp_path / name).mkdir(exist_ok=True)
    web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist", workspaces=[WebWorkspace(id=name, label=name, path=tmp_path / name) for name in ("one", "two")])
    app = create_app(web, config or RunConfig(tools=["read_file", "write_file"]), tmp_path / "runs", model_client=model)

    def key(config):
        """仅为离线启动提供固定 config 凭据占位，不读用户密钥。"""
        return "offline-memory-key"

    app.state.manager._key_provider = key
    return app, web


async def wait_run(client, run_id: str) -> dict:
    """轮询 client 中 run_id 的真实终态；返回运行视图，超时失败，不创建新任务。"""
    for _ in range(500):
        view = (await client.get("/api/runs/" + run_id)).json()
        if not view["active"]:
            return view
        await asyncio.sleep(0.01)
    raise AssertionError("记忆验收运行未结束")


async def run(client, prompt: str, *, project: str = "one", **options):
    """通过 client 接纳 prompt；project 选择已登记目录，options 覆盖请求，返回真实终态视图。"""
    response = await client.post("/api/runs", json={"workspace_id": project, "prompt": prompt, **options})
    assert response.status_code == 202, response.text
    job = await wait_run(client, response.json()["run_id"])
    assert job["result"]["execution_status"] in {"completed", "failed", "waiting_input"}, job
    return (await client.get("/api/runs/" + job["run_id"])).json()


def test_learning_confirmation_cross_conversation_and_deletion(tmp_path: Path):
    """学习产生候选，确认后新会话按真实记忆生成文件；其他项目和删除后的新轮不带知识。"""
    async def scenario():
        """驱动实际应用生命周期及 HTTP 操作，返回 None；不访问供应商。"""
        model = MemoryModel()
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            learning = await run(client, "学习接口兼容决定")
            assert learning["result"]["execution_status"] == "completed"
            candidates = (await client.get(f"/api/runs/{learning['run_id']}/memories")).json()["items"]
            assert len(candidates) == 1 and candidates[0]["status"] == "proposed"
            assert learning["context"]["memory"]["proposals"][0]["id"] == candidates[0]["id"]
            assert (await client.get("/api/projects/one/memories", params={"status": "active"})).json()["items"] == []
            candidate = candidates[0]
            body = {"request_id": str(uuid4()), "expected_revision": 1, "action": "accept"}
            endpoint = f"/api/runs/{learning['run_id']}/memories/{candidate['id']}"
            accepted = await client.patch(endpoint, json=body)
            assert accepted.status_code == 200, accepted.text
            assert (await client.patch(endpoint, json=body)).json() == accepted.json()
            used = await run(client, "修改接口返回")
            assert json.loads((tmp_path / "one/result.json").read_text())["field"] == "legacy_name"
            assert used["context"]["memory"]["selected"][0]["source"]["run_id"] == learning["run_id"]
            await run(client, "修改接口返回", project="two")
            assert json.loads((tmp_path / "two/result.json").read_text())["field"] == "need_user_decision"
            exported = await client.get("/api/projects/one/memories/export")
            assert exported.status_code == 200 and "legacy_name" in exported.text
            assert "attachment" in exported.headers["content-disposition"]
            deleted = await client.patch(endpoint, json={"request_id": str(uuid4()), "expected_revision": 2, "action": "delete"})
            assert deleted.status_code == 200
            await run(client, "修改接口返回")
            assert json.loads((tmp_path / "one/result.json").read_text())["field"] == "need_user_decision"
            historical = (await client.get("/api/runs/" + used["run_id"])).json()
            assert historical["context"]["memory"] == used["context"]["memory"]
            assert historical["result"]["validation_status"] == "unverified"
            revisions = (await client.get(f"/api/projects/one/memories/{candidate['id']}/revisions")).json()["items"]
            assert [item["revision"] for item in revisions] == [3, 2, 1]
    asyncio.run(scenario())


def test_public_controls_origin_and_version_conflict(tmp_path):
    """公开页面只能选择使用/学习；跨项目来源、版本过期及同源限制均在后端生效。"""
    async def scenario():
        """经真实 API 检查非法和合法操作，返回 None，失败不启动模型。"""
        model = MemoryModel()
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            invalid = await client.post("/api/runs", json={"workspace_id": "one", "prompt": "修改", "config": {"memory": {"max_entries": 999}}})
            assert invalid.status_code == 422 and not model.requests
            body = {"request_id": str(uuid4()), "title": "接口", "content": "保留 legacy_name", "type": "fact", "details": {"subject": "接口", "attribute": "说明", "value": "保留 legacy_name"}}
            saved = await client.post("/api/projects/one/memories", json=body)
            assert saved.status_code == 200
            assert (await client.post("/api/projects/one/memories", json=body)).json() == saved.json()
            assert (await client.post("/api/projects/one/memories", json=body, headers={"Origin": "https://untrusted.example"})).status_code == 403
            item = saved.json()
            edited = await client.patch(f"/api/projects/one/memories/{item['id']}", json={"request_id": str(uuid4()), "expected_revision": 1, "action": "edit", "content": {"title": "接口", "content": "保留 legacy_name_v2", "type": "fact", "details": {"subject": "接口", "attribute": "说明", "value": "保留 legacy_name_v2"}}})
            assert edited.status_code == 200
            stale = await client.patch(f"/api/projects/one/memories/{item['id']}", json={"request_id": str(uuid4()), "expected_revision": 1, "action": "delete"})
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "memory_revision_conflict"
            disabled = await run(client, "修改接口", config={"memory": {"use": False, "capture": False}})
            assert not disabled["context"]["memory"]["selected"]
            names = {tool["function"]["name"] for tool in model.requests[-1]["tools"]}
            assert not names.intersection({"memory_search", "memory_get", "memory_propose"})
            foreign = await client.post("/api/projects/two/memories", json={**body, "request_id": str(uuid4()), "source_run_id": disabled["run_id"]})
            assert foreign.status_code == 409
    asyncio.run(scenario())


def test_planning_read_only_memory_tools(tmp_path):
    """规划期间可搜索记忆，候选写入被宿主拒绝，未生成任何候选或额外权限。"""
    async def scenario():
        """让固定模型请求两个工具，检查真实事件和空记忆库，返回 None。"""
        model = ScriptModel([reply(call("memory_search", query="接口"), call("memory_propose", title="越界", content="规划不应持久学习", type="rule", details={"topic": "规划", "behavior": "不学习"})), reply()])
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            view = await run(client, "研究接口", mode="plan")
            results = [json.loads(message["content"]) for message in model.requests[-1]["messages"] if message.get("role") == "tool"]
            assert results[0]["ok"] and results[1]["error"]["code"] == "planning_read_only"
            assert (await client.get("/api/projects/one/memories")).json()["items"] == []
            assert view["result"]["stop_reason"] == "planning_complete"
    asyncio.run(scenario())




def test_corrupt_memory_stops_before_model_and_surfaces_warning(tmp_path):
    """启用的记忆库损坏时不默默降级，零模型请求，页面可读警告随 Run 保留。"""
    async def scenario():
        """构造损坏数据库并走真实提交，关闭使用和候选后允许明确重新运行。"""
        model = MemoryModel()
        app, _ = application(tmp_path, model)
        memory = tmp_path / "runs/.memory"
        memory.mkdir(parents=True)
        (memory / "memory.sqlite3").write_bytes(b"corrupt-sqlite")
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            view = await run(client, "修改接口")
            assert view["result"]["stop_reason"] == "memory_storage_error" and not model.requests
            assert any("记忆记录" in warning for warning in view["warnings"])
            explicit = await run(client, "修改接口", config={"memory": {"use": False, "capture": False}})
            assert explicit["result"]["execution_status"] == "completed"
    asyncio.run(scenario())


def test_partial_memory_override_preserves_other_choice_on_followup(tmp_path):
    """只覆盖 use 时继承父轮 capture，不用字段默认值重新开启用户已关闭的候选。"""
    async def scenario():
        """真实续轮通过队列继承部分选择，返回 None；不访问供应商。"""
        model = MemoryModel()
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = await run(client, "修改接口", config={"memory": {"capture": False}})
            identifier = str(uuid4())
            response = await client.post("/api/runs/" + first["run_id"] + "/followups", json={"request_id": identifier, "prompt": "继续修改", "config": {"memory": {"use": False}}})
            assert response.status_code == 202, response.text
            job = await wait_run(client, response.json()["run_id"])
            view = (await client.get("/api/runs/" + job["run_id"])).json()
            assert view["config"]["memory"]["use"] is False
            assert view["config"]["memory"]["capture"] is False
    asyncio.run(scenario())


def test_followup_does_not_reinject_deleted_memory_tool_result(tmp_path):
    """删除后续轮排除旧 memory_get 成功正文，保留真实对话及原 Run 的固定回读证据。"""
    async def scenario():
        """先实际回读，再删除并接续；检查供应商收到的新输入不包含旧记忆正文。"""
        model = ScriptModel([])
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            saved = (await client.post("/api/projects/one/memories", json={"request_id": str(uuid4()), "title": "接口字段约定", "content": "OBSOLETE_MEMORY_MARKER：接口保留特定字段", "type": "fact", "details": {"subject": "接口字段约定", "attribute": "说明", "value": "OBSOLETE_MEMORY_MARKER：接口保留特定字段"}})).json()
            model.responses.extend([reply(call("memory_get", memory_id=saved["id"])), reply(), reply()])
            first = await run(client, "读取接口字段约定")
            assert first["context"]["memory"]["reads"]
            await client.patch(f"/api/projects/one/memories/{saved['id']}", json={"request_id": str(uuid4()), "expected_revision": 1, "action": "delete"})
            identifier = str(uuid4())
            response = await client.post("/api/runs/" + first["run_id"] + "/followups", json={"request_id": identifier, "prompt": "继续检查当前约定"})
            assert response.status_code == 202, response.text
            job = await wait_run(client, response.json()["run_id"])
            second = (await client.get("/api/runs/" + job["run_id"])).json()
            sent = json.dumps(model.requests[-1]["messages"], ensure_ascii=False)
            assert "OBSOLETE_MEMORY_MARKER" not in sent
            assert "读取接口字段约定" in sent and "继续检查当前约定" in sent
            assert second["context"]["memory"]["selected"] == []
            old = (await client.get("/api/runs/" + first["run_id"])).json()
            assert old["context"]["memory"]["reads"] == first["context"]["memory"]["reads"]
    asyncio.run(scenario())


def test_path_rule_pauses_pending_batch_then_replans_with_actual_context(tmp_path):
    """路径约定首次出现时整批剩余变更不执行，下一次实际输入带约定后才接受新参数。"""
    async def scenario():
        """通过真实队列、Runner 和文件工具检查副作用、配对结果及逐次采用记录，返回 None。"""
        model = ScriptModel([
            reply(call("write_file", path="src/api/result.json", content='{"amount": 1.2}'), call("write_file", path="must_not_exist.txt", content="old batch")),
            reply(call("write_file", path="src/api/result.json", content='{"amount": "1.20"}')),
            reply(),
        ])
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            saved = await client.post("/api/projects/one/memories", json={"request_id": str(uuid4()), "title": "金额序列化约定", "type": "rule", "origin": "user_correction", "details": {"topic": "金额序列化", "behavior": "金额必须序列化为两位小数字符串"}, "applies_to": {"paths": ["src/api/**"]}})
            assert saved.status_code == 200, saved.text
            view = await run(client, "完成工作")
            assert view["result"]["execution_status"] == "completed"
            assert json.loads((tmp_path / "one/src/api/result.json").read_text())["amount"] == "1.20"
            assert not (tmp_path / "one/must_not_exist.txt").exists()
            paired = [json.loads(message["content"]) for message in model.requests[1]["messages"] if message["role"] == "tool"]
            assert len(paired) == 2 and all(value["error"]["code"] == "memory_context_updated" for value in paired)
            assert "金额必须序列化为两位小数字符串" not in json.dumps(model.requests[0]["messages"], ensure_ascii=False)
            assert "金额必须序列化为两位小数字符串" in json.dumps(model.requests[1]["messages"], ensure_ascii=False)
            uses = view["context"]["memory"]["uses"]
            assert uses[0]["selected"] == [] and uses[1]["selected"][0]["id"] == saved.json()["id"]
            await client.patch(f"/api/projects/one/memories/{saved.json()['id']}", json={"request_id": str(uuid4()), "expected_revision": 1, "action": "delete"})
            old = (await client.get("/api/runs/" + view["run_id"])).json()
            assert old["context"]["memory"]["uses"] == uses
    asyncio.run(scenario())


def test_http_conflict_requires_explicit_versioned_replacement(tmp_path):
    """HTTP 返回同项目旧主张供前端审阅；带明确旧版本的替代才在同事务生效。"""
    async def scenario():
        """保存相反决策并检查409、原子替代和历史修订，返回 None，不调用模型。"""
        app, _ = application(tmp_path, MemoryModel())
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = {"request_id": str(uuid4()), "title": "存储方案", "type": "decision", "details": {"topic": "存储", "choice": "SQLite", "rationale": "单机部署"}}
            old = (await client.post("/api/projects/one/memories", json=first)).json()
            new = {**first, "request_id": str(uuid4()), "details": {"topic": "存储", "choice": "Postgres", "rationale": "新增多机部署要求"}}
            rejected = await client.post("/api/projects/one/memories", json=new)
            assert rejected.status_code == 409
            assert rejected.json()["error"]["details"]["conflicts"][0]["details"]["choice"] == "SQLite"
            replaced = await client.post("/api/projects/one/memories", json={**new, "request_id": str(uuid4()), "replacements": [{"id": old["id"], "revision": old["revision"]}]})
            assert replaced.status_code == 200, replaced.text
            active = (await client.get("/api/projects/one/memories?status=active")).json()["items"]
            assert len(active) == 1 and active[0]["details"]["choice"] == "Postgres"
    asyncio.run(scenario())
