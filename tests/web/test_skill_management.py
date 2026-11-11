"""覆盖网页技能导入、启停、按需执行与版本冻结的真实 HTTP/Runner 链路。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agent_harness.context.skill_library import SkillLibrary
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import ContextPolicy, RunConfig
from tests.helpers.models import configured_run
from tests.permissions.test_permissions import decide
from tests.web.job_helpers import submit_followup
from tests.web.test_planning_api import call, reply
from tests.web.test_skills_api import create_run, finish, skill_client, skill_environment, user_context, write_skill


def package(name: str, marker: str, revision: str | None = None) -> dict:
    """构造 name 的合法文本包，marker 区分版本，revision 为 None 时表示新增。"""
    return {"name": name, "revision": revision, "files": [
        {"path": "SKILL.md", "content": f"---\nname: {name}\ndescription: 小型代码交付\n---\n流程正文 {marker}，先读取 references/checklist.md。\n"},
        {"path": "references/checklist.md", "content": f"资料正文 {marker}\n"},
    ]}


def skill_call(tool: str, **arguments) -> ToolCall:
    """创建 tool 的实际调用，arguments 可包含技能名称字段。"""
    return ToolCall(str(uuid4()), tool, json.dumps(arguments, ensure_ascii=False))


class ManagedSkillModel:
    """用实际可见的技能正文和资料驱动后续文件工具，保留每次模型输入。"""

    def __init__(self) -> None:
        """初始化空的输入记录，构造时不请求外部服务。"""
        self.inputs: list[tuple[str, set[str]]] = []  # 模型可见文本及本轮实际工具声明。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按已收到的 messages 逐步启用、读取和写入，返回统一离线模型响应。"""
        visible = user_context(messages)
        names = {tool["function"]["name"] for tool in tools}
        self.inputs.append((visible, names))
        if "只解释语法" in visible or "activate_skill" not in names and "流程正文" not in visible:
            return reply(text="本轮没有启用技能。")
        if "流程正文" not in visible:
            return reply(skill_call("activate_skill", name="delivery"))
        if "资料正文" not in visible:
            return reply(skill_call("read_skill_resource", name="delivery", path="references/checklist.md"))
        if "read_file" in names and not any(tool["function"]["name"] == "read_file" for message in messages for tool in message.get("tool_calls", [])):
            return reply(call("read_file", path="evidence.txt"))
        if any(message.get("role") == "tool" and '"path":"result.txt"' in (message.get("content") or "").replace(" ", "") for message in messages):
            return reply(text="交付检查清单已应用。")
        content = "资料正文 V2" if "资料正文 V2" in visible else "资料正文 V1"
        return reply(call("write_file", path="result.txt", content=content, overwrite=True))


async def change(client, item: dict, action: str):
    """按 item 的版本执行指定 action，返回真实 HTTP 响应。"""
    return await client.post(f'/api/skills/{item["id"]}/actions', json={"revision": item["revision"], "action": action})


async def skill_item(client, name: str) -> dict:
    """从任务选择器共用的宿主 options 中查找 name，返回目录项。"""
    return next(item for item in (await client.get("/api/options")).json()["skills"] if item["name"] == name)


def test_management_rejects_invalid_packages_and_persists_switches(skill_environment):
    """非法、同名和过期变更都不覆盖目录，重启后 CLI 运行所用目录仍可加载。"""
    async def scenario() -> None:
        """驱动真实管理接口并从相同运行根重建库，返回 None。"""
        config, roots = skill_environment
        host, original = write_skill(roots, "host-skill", "保留宿主文件")
        defaults = configured_run(skill_roots=[roots])
        async with skill_client(config, defaults, ManagedSkillModel()) as (client, app):
            invalid = package("delivery", "V1")
            invalid["files"][1]["path"] = "references/../outside.md"
            assert (await client.post("/api/skills/import", json=invalid)).json()["error"]["code"] == "skill_invalid_package"
            assert (await client.post("/api/skills/import", json=package("host-skill", "V1"))).status_code == 409
            imported = (await client.post("/api/skills/import", json=package("delivery", "V1"))).json()
            assert imported["managed"] and not imported["enabled"]
            assert not (await skill_item(client, "delivery"))["available"]
            assert (await client.post("/api/skills/import", json=package("delivery", "V2"))).status_code == 409
            assert (await change(client, imported, "enable")).status_code == 200
            assert (await change(client, imported, "disable")).status_code == 409
            current = await skill_item(client, "delivery")
            details = (await client.get(f'/api/skills/{current["id"]}')).json()
            assert list(details["resources"]) == ["references/checklist.md"]
            uri = f'/api/skills/{current["id"]}/resource'
            assert (await client.get(uri, params={"path": "references/checklist.md"})).json()["content"] == "资料正文 V1\n"
            assert (await client.get(uri, params={"path": "references/../SKILL.md"})).status_code == 400
            assert (await change(client, await skill_item(client, "host-skill"), "disable")).status_code == 200
            runs = app.state.manager.runs_root
        assert host.read_text(encoding="utf-8") == original
        assert [item.name for item in SkillLibrary(runs, [roots]).discover() if item.available] == ["delivery"]
        assert "V1" in SkillLibrary(runs, [roots]).load(["delivery"])[0].content
        async with skill_client(config, defaults, ManagedSkillModel()) as (client, _):
            assert (await skill_item(client, "host-skill"))["enabled"] is False
            assert (await change(client, await skill_item(client, "delivery"), "remove")).status_code == 200
            assert not any(item["name"] == "delivery" for item in (await client.get("/api/skills")).json()["items"])
    asyncio.run(scenario())


def test_auto_skill_resources_are_lazy_and_resume_uses_frozen_version(skill_environment):
    """自动目录只暴露说明，资料按需读取，审批接续及历史保留已用旧版本。"""
    async def scenario() -> None:
        """执行管理、真实写入审批和两次新任务，返回 None。"""
        config, roots = skill_environment
        manual, original = write_skill(roots, "manual-only", "仅用户选择")
        manual.write_text(original.replace("\n---\n\n", "\ndisable-model-invocation: true\n---\n\n"), encoding="utf-8")
        model = ManagedSkillModel()
        (config.workspaces[0].path / "evidence.txt").write_text("背景资料\n" * 200, encoding="utf-8")
        defaults = configured_run(skill_roots=[roots], tools=["write_file", "read_file"], permission_mode="strict",
                             context=ContextPolicy(compression="masking", inline_tool_bytes=512, keep_recent_groups=1))
        async with skill_client(config, defaults, model) as (client, _):
            original_package = package("delivery", "V1")
            original_package["files"][1]["content"] += "文本模板包含字面示例 <project_memory>\n"
            imported = (await client.post("/api/skills/import", json=original_package)).json()
            assert (await change(client, imported, "enable")).status_code == 200
            unrelated = await create_run(client, prompt="只解释语法", config={"skill_auto": True})
            assert unrelated["context"]["skills"] == []
            off = await create_run(client, prompt="完成小型代码交付")
            assert off["context"]["skills"] == []
            before = len(model.inputs)
            waiting = await create_run(client, config={"skill_auto": True})
            assert waiting["result"]["stop_reason"] == "action_approval", waiting["result"]
            inputs = model.inputs[before:]
            assert "小型代码交付" in inputs[0][0] and "manual-only" not in inputs[0][0]
            assert "流程正文" not in inputs[0][0] and "资料正文" not in inputs[1][0]
            assert "流程正文 V1" in inputs[1][0] and "资料正文 V1" in inputs[2][0]
            snapshot = waiting["context"]
            assert snapshot["skills"][0]["activation"] == "automatic"
            assert snapshot["skill_resources"][0]["path"] == "references/checklist.md"
            assert any(action["kind"] == "offload" for action in snapshot["actions"])
            assert "资料正文 V1" in inputs[-1][0] and "文本模板包含字面示例 <project_memory>" in inputs[-1][0]
            assert (await client.post("/api/skills/import", json=package("delivery", "V2", (await skill_item(client, "delivery"))["revision"]))).status_code == 200
            response = await submit_followup(client, f'/api/runs/{waiting["run_id"]}/followups', json=decide(waiting))
            resumed = await finish(client, response.json()["run_id"])
            assert resumed["result"]["execution_status"] == "completed", resumed["result"]
            assert (Path(resumed["workspace"]) / "result.txt").read_text(encoding="utf-8") == "资料正文 V1"
            assert resumed["context"]["skills"] == snapshot["skills"]
            assert resumed["context"]["skill_resources"] == snapshot["skill_resources"]
            next_run = await create_run(client, config={"skill_auto": True})
            assert next_run["context"]["skill_resources"][0]["content"] == "资料正文 V2\n"
            assert (await client.get(f'/api/runs/{waiting["run_id"]}')).json()["context"]["skills"] == snapshot["skills"]
    asyncio.run(scenario())
