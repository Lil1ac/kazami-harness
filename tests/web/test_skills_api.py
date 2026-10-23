"""通过真实网页请求、Runner 与文件工具验证技能选择及逐轮冻结。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import ContextPolicy, RunConfig
from agent_harness.store import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class SkillWritingModel:
    """记录真正发给供应商的消息，并依据当前技能约定调用真实写文件工具。"""

    def __init__(self) -> None:
        """初始化空请求列表；调用 complete 才会记录输入，不访问网络或文件。"""
        self.requests: list[list[dict]] = []  # 每次请求的深拷贝，用于核对各轮当前上下文。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收完整消息与工具定义；新任务先写文件，工具返回后给出最终回答。"""
        self.requests.append(deepcopy(messages))
        user_index = max(index for index, message in enumerate(messages) if message["role"] == "user")
        prompt = messages[user_index]["content"]
        observed = any(message["role"] == "tool" for message in messages[user_index + 1:])
        calls = []
        if not observed:
            instructions = "\n".join(message.get("content") or "" for message in messages if message["role"] == "user")
            marker = next((item for item in ("STYLE_ALPHA_V2", "STYLE_ALPHA_V1", "STYLE_BETA") if item in instructions), "NO_SKILL")
            calls = [ToolCall(str(uuid4()), "write_file", json.dumps({
                "path": "result.txt", "content": f"{marker}: {prompt}\n", "overwrite": True,
            }, ensure_ascii=False))]
        message = {"role": "assistant", "content": None if calls else "本轮文件已更新。"}
        if calls:
            message["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}
                for call in calls
            ]
        return ModelResponse(
            message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
            response_model="offline-skills-api",
        )


@pytest.fixture
def skill_environment(tmp_path: Path, monkeypatch) -> tuple[WebConfig, Path]:
    """在临时目录登记普通工作区与技能根，返回配置和根路径；凭据只用于离线预检。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-skills-api-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    roots = tmp_path / "skills"
    roots.mkdir()
    config = WebConfig(
        project_dir=tmp_path, runtime_root=tmp_path / "runtime", frontend_dist=tmp_path / "dist",
        workspaces=[WebWorkspace(id="files", label="技能验收工作区", path=workspace)],
    )
    return config, roots


def write_skill(root: Path, name: str, marker: str) -> tuple[Path, str]:
    """在 root 下写入 name 对应的规范技能；返回文件路径及完整原文，marker 为模型可见约定。"""
    directory = root / name
    directory.mkdir(exist_ok=True)
    content = f"---\nname: {name}\ndescription: 输出约定 {name}\n---\n\n请在结果中采用 {marker}。\n"
    target = directory / "SKILL.md"
    target.write_text(content, encoding="utf-8", newline="")
    return target, content


@asynccontextmanager
async def skill_client(config: WebConfig, run_config: RunConfig, model: SkillWritingModel) -> AsyncIterator[tuple]:
    """用宿主 run_config 与离线 model 启动真实应用，产出客户端和应用；退出等待后台清理。"""
    app = create_app(config, run_config, config.project_dir / "records", model_client=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False,
        ) as client:
            yield client, app


async def finish(client: httpx.AsyncClient, identifier: str) -> dict:
    """等待 identifier 完成执行与工作区收尾，返回最终网页视图；最长等待十五秒。"""
    async with asyncio.timeout(15):
        while True:
            response = await client.get(f"/api/runs/{identifier}")
            assert response.status_code == 200, response.text
            view = response.json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)


async def create_run(client: httpx.AsyncClient, **extra) -> dict:
    """提交普通工作区任务并等待收尾；extra 可覆盖网页请求字段，返回真实最终视图。"""
    response = await client.post("/api/runs", json={"prompt": "first", "workspace_id": "files", **extra})
    assert response.status_code == 202, response.text
    return await finish(client, response.json()["run_id"])


async def continue_run(client: httpx.AsyncClient, parent: dict, prompt: str, **extra) -> dict:
    """以 parent 为最新轮提交 prompt 和可选配置覆盖，返回后台执行完毕后的网页视图。"""
    response = await client.post(
        f'/api/runs/{parent["run_id"]}/followups',
        json={"request_id": str(uuid4()), "prompt": prompt, **extra},
    )
    assert response.status_code == 202, response.text
    return await finish(client, response.json()["run_id"])


def evidence(records: Path, identifier: str) -> dict[str, str]:
    """读取 identifier 三份可信运行文件并返回 SHA256，用于验证续轮不会改写历史。"""
    return {
        name: hashlib.sha256((records / identifier / name).read_bytes()).hexdigest()
        for name in ("run.json", "events.jsonl", "summary.json")
    }


def user_context(messages: list[dict]) -> str:
    """拼接 messages 中用户角色的正文，供区分当前技能指令与历史工具调用参数。"""
    return "\n".join(message.get("content") or "" for message in messages if message["role"] == "user")


def test_skill_options_and_selected_body_drive_real_tool_output(skill_environment):
    """目录展示可用及损坏技能，网页只传名称；所选原文进入模型并驱动真实产物。"""
    async def scenario():
        """提交所选技能后核对输入、文件、快照及持久化事件；返回 None。"""
        config, roots = skill_environment
        _, original = write_skill(roots, "alpha", "STYLE_ALPHA_V1")
        broken = roots / "broken"
        broken.mkdir()
        (broken / "SKILL.md").write_text("---\nname: broken\n---\n没有描述。", encoding="utf-8")
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[roots], tools=["write_file"]), model) as (client, app):
            options_response = await client.get("/api/options")
            assert options_response.status_code == 200
            options = {item["name"]: item for item in options_response.json()["skills"]}
            assert options["alpha"]["available"] is True
            assert options["alpha"]["description"] == "输出约定 alpha"
            assert options["broken"]["available"] is False
            assert options["broken"]["error_code"].startswith("skill_")
            run = await create_run(client, config={"skills": ["alpha"]})
            assert run["result"]["execution_status"] == "completed"
            assert run["result"]["tool_calls"] == 1
            assert run["config"]["skills"] == ["alpha"]
            assert (Path(run["workspace"]) / "result.txt").read_text(encoding="utf-8") == "STYLE_ALPHA_V1: first\n"
            assert original in user_context(model.requests[0])
            loaded = run["context"]["skills"]
            assert len(loaded) == 1 and loaded[0]["content"] == original
            assert loaded[0]["sha256"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
            assert loaded[0]["size_bytes"] == len(original.encode("utf-8"))
            assert run["context"]["skill_error"] is None
            persisted = RunStore.read(app.state.manager.runs_root, run["run_id"])
            prepared = next(event for event in persisted.events if event.type == "context_prepared")
            assert prepared.payload["skills"] == loaded
    asyncio.run(scenario())


def test_followup_inherits_selection_reloads_body_and_can_clear_or_replace(skill_environment):
    """四轮真实文件修改覆盖默认继承、同名文件更新、明确清空和切换，并冻结旧证据。"""
    async def scenario():
        """顺序提交各轮，比较模型当前指令与历史 SHA256；返回 None。"""
        config, roots = skill_environment
        _, first_body = write_skill(roots, "alpha", "STYLE_ALPHA_V1")
        _, beta_body = write_skill(roots, "beta", "STYLE_BETA")
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[roots], tools=["write_file"]), model) as (client, app):
            first = await create_run(client, config={"skills": ["alpha"]})
            original_evidence = evidence(app.state.manager.runs_root, first["run_id"])
            _, second_body = write_skill(roots, "alpha", "STYLE_ALPHA_V2")
            second_call = len(model.requests)
            second = await continue_run(client, first, "second")
            assert second["config"]["skills"] == ["alpha"]
            assert second["context"]["skills"][0]["content"] == second_body
            second_input = user_context(model.requests[second_call])
            assert second_body in second_input and first_body not in second_input
            assert second_input.count(second_body) == 1
            third_call = len(model.requests)
            third = await continue_run(client, second, "third", config={"skills": []})
            assert third["config"]["skills"] == [] and third["context"]["skills"] == []
            assert "STYLE_ALPHA" not in user_context(model.requests[third_call])
            assert (Path(third["workspace"]) / "result.txt").read_text(encoding="utf-8") == "NO_SKILL: third\n"
            fourth_call = len(model.requests)
            fourth = await continue_run(client, third, "fourth", config={"skills": ["beta"]})
            assert fourth["config"]["skills"] == ["beta"]
            assert beta_body in user_context(model.requests[fourth_call])
            assert "STYLE_ALPHA" not in user_context(model.requests[fourth_call])
            assert (Path(fourth["workspace"]) / "result.txt").read_text(encoding="utf-8") == "STYLE_BETA: fourth\n"
            assert evidence(app.state.manager.runs_root, first["run_id"]) == original_evidence
            old = (await client.get(f'/api/runs/{first["run_id"]}')).json()
            assert old["context"]["skills"][0]["content"] == first_body
            assert old["context"]["skills"][0]["sha256"] != second["context"]["skills"][0]["sha256"]
    asyncio.run(scenario())


def test_followup_uses_current_host_roots_after_restart(skill_environment):
    """宿主重启调整技能根后，续轮沿用选名却从当前宿主目录加载，原始快照保持不变。"""
    async def scenario():
        """两次应用生命周期使用同一记录目录及不同技能根，核对新旧运行输入；返回 None。"""
        config, old_roots = skill_environment
        _, original = write_skill(old_roots, "alpha", "STYLE_ALPHA_V1")
        new_roots = config.project_dir / "new-skills"
        new_roots.mkdir()
        _, replacement = write_skill(new_roots, "alpha", "STYLE_ALPHA_V2")
        async with skill_client(config, RunConfig(skill_roots=[old_roots], tools=["write_file"]), SkillWritingModel()) as (client, app):
            first = await create_run(client, config={"skills": ["alpha"]})
            before = evidence(app.state.manager.runs_root, first["run_id"])
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[new_roots], tools=["write_file"]), model) as (client, app):
            second = await continue_run(client, first, "after-restart")
            assert second["config"]["skill_roots"] == [str(new_roots)]
            assert replacement in user_context(model.requests[0])
            assert original not in user_context(model.requests[0])
            assert evidence(app.state.manager.runs_root, first["run_id"]) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing", "invalid", "oversized"])
def test_skill_failure_is_persisted_before_any_model_or_tool(skill_environment, failure: str):
    """选中缺失、损坏或过大的技能时保留 failed 记录与受控错误，模型和工具调用为零。"""
    async def scenario():
        """提交失败技能并读取真实事件及工作区，验证无静默降级或原始异常泄漏；返回 None。"""
        config, roots = skill_environment
        if failure == "invalid":
            broken = roots / "unusable"
            broken.mkdir()
            (broken / "SKILL.md").write_text("---\nname: [unterminated\n---\n", encoding="utf-8")
        elif failure == "oversized":
            write_skill(roots, "unusable", "超限正文" * 20000)
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[roots], tools=["write_file"]), model) as (client, app):
            run = await create_run(client, config={"skills": ["unusable"]})
            result = run["result"]
            assert result["execution_status"] == "failed"
            assert result["stop_reason"].startswith("skill_")
            assert result["model_turns"] == result["http_attempts"] == result["tool_calls"] == 0
            assert model.requests == []
            assert run["context"]["skills"] == []
            assert run["context"]["skill_error"]["code"] == result["stop_reason"]
            assert run["context"]["skill_error"]["message"]
            assert not (Path(run["workspace"]) / "result.txt").exists()
            history = RunStore.read(app.state.manager.runs_root, run["run_id"])
            assert any(event.type == "context_prepared" for event in history.events)
            assert [event.type for event in history.events].count("run_finished") == 1
    asyncio.run(scenario())


def test_unselected_skill_is_not_loaded_and_host_paths_cannot_be_overridden(skill_environment):
    """未选技能不注入；网页提交自定义宿主根或技能正文均被422拒绝且不调用模型。"""
    async def scenario():
        """先校验边界再创建普通任务，确认没有误加载或拒绝后遗留运行；返回 None。"""
        config, roots = skill_environment
        write_skill(roots, "alpha", "STYLE_ALPHA_V1")
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[roots], tools=["write_file"]), model) as (client, app):
            for patch in ({"skill_roots": [str(roots)]}, {"skills": ["alpha"], "skill_content": "任意正文"}):
                response = await client.post("/api/runs", json={"prompt": "first", "workspace_id": "files", "config": patch})
                assert response.status_code == 422, response.text
            assert model.requests == []
            assert not app.state.manager.runs_root.exists() or not list(app.state.manager.runs_root.glob("*/run.json"))
            run = await create_run(client)
            assert run["context"]["skills"] == [] and run["context"]["skill_error"] is None
            assert "STYLE_ALPHA" not in user_context(model.requests[0])
            assert (Path(run["workspace"]) / "result.txt").read_text(encoding="utf-8") == "NO_SKILL: first\n"
    asyncio.run(scenario())


def test_selected_skill_counts_towards_hard_input_budget(skill_environment):
    """技能完整正文参与宿主输入预算；超限保留快照但不得发出模型请求或执行工具。"""
    async def scenario():
        """使用同一正文和足够容纳宿主提示的低字节预算，检查技能导致的真实超限；返回 None。"""
        config, roots = skill_environment
        _, original = write_skill(roots, "alpha", "STYLE_ALPHA_V1" + "额外说明" * 2000)
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[roots], tools=["write_file"]), model) as (client, app):
            baseline = await create_run(client, config={"context": {"max_input_bytes": 8000}})
            assert baseline["result"]["execution_status"] == "completed"
            before = len(model.requests)
            selected = await create_run(client, config={"skills": ["alpha"], "context": {"max_input_bytes": 8000}})
            assert selected["result"]["execution_status"] == "limit_exceeded"
            assert selected["result"]["stop_reason"] == "max_model_input_bytes"
            assert selected["result"]["model_turns"] == selected["result"]["tool_calls"] == selected["result"]["http_attempts"] == 0
            assert len(model.requests) == before
            assert selected["context"]["skills"][0]["content"] == original
    asyncio.run(scenario())


class SkillCompressionModel:
    """制造真实长工具历史并返回受控摘要，使技能保护在实际 hybrid 流程中接受检验。"""

    def __init__(self) -> None:
        """初始化按任务计步及执行请求证据；不读写工作区，不访问模型网络。"""
        self.steps: dict[str, int] = {}  # 每条用户任务已返回的执行响应次数，摘要不占此步数。
        self.execution: list[tuple[str, list[dict]]] = []  # 按任务保存真实执行输入，摘要输入单独计数。
        self.summary_calls = 0  # 实际进入无工具摘要请求的次数。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """执行请求连续读取小文件以积累历史；无工具请求返回合法JSON摘要，保持真实预算计数。"""
        calls = []
        if not tools:
            self.summary_calls += 1
            content = json.dumps({
                "goal": "核对当前文件", "decisions": [], "changes": [],
                "evidence": ["读过 small.txt，文件内容以最新工具观察为准。"],
                "open_issues": [], "next_steps": ["继续本轮核对。"],
            }, ensure_ascii=False)
        else:
            prompts = {"compress-first", "compress-cleared", "compress-restored"}
            prompt = next(message["content"] for message in reversed(messages)
                          if message["role"] == "user" and message.get("content") in prompts)
            self.execution.append((prompt, deepcopy(messages)))
            step = self.steps.get(prompt, 0) + 1
            self.steps[prompt] = step
            if step <= 5:
                calls = [ToolCall(f"{prompt}-{step}", "read_file", '{"path":"small.txt"}')]
            content = "继续核对文件及当前约定。" + "历史分析细节abcd；" * 95 if calls else "本轮核对完成。"
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}
                for call in calls
            ]
        return ModelResponse(
            message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
            response_model="offline-skills-compression",
        )


def test_hybrid_preserves_current_skill_and_inherits_summaries_across_prefix_changes(skill_environment):
    """真实摘要后技能全文仍在每次执行输入；清空及恢复技能改变前缀长度时，祖先摘要仍可核验继承。"""
    async def scenario():
        """连续运行选中、清空、恢复三轮，检查真实摘要事件、消息与首轮证据；返回 None。"""
        config, roots = skill_environment
        _, original = write_skill(roots, "alpha", "STYLE_ALPHA_V1")
        (config.workspaces[0].path / "small.txt").write_text("source evidence\n", encoding="utf-8")
        model = SkillCompressionModel()
        host = RunConfig(
            skill_roots=[roots], tools=["read_file"],
            context=ContextPolicy(
                compression="hybrid", max_input_bytes=15000, keep_recent_groups=1,
                inline_tool_bytes=1024, max_summary_calls=4,
            ),
        )
        async with skill_client(config, host, model) as (client, app):
            first = await create_run(client, prompt="compress-first", config={"skills": ["alpha"]})
            assert first["result"]["execution_status"] == "completed", first["result"]
            assert first["context"]["summary_calls"] >= 1
            assert any(action["kind"] == "summary" for action in first["context"]["actions"])
            before = evidence(app.state.manager.runs_root, first["run_id"])
            cleared = await continue_run(client, first, "compress-cleared", config={"skills": []})
            assert cleared["result"]["execution_status"] == "completed", cleared["result"]
            assert any(action["reason"] == "inherited" for action in cleared["context"]["actions"])
            restored = await continue_run(client, cleared, "compress-restored", config={"skills": ["alpha"]})
            assert restored["result"]["execution_status"] == "completed", restored["result"]
            assert any(action["reason"] == "inherited" for action in restored["context"]["actions"])
            assert model.summary_calls >= 1
            for prompt, messages in model.execution:
                current = user_context(messages)
                if prompt == "compress-cleared":
                    assert original not in current
                else:
                    assert current.count(original) == 1
            assert evidence(app.state.manager.runs_root, first["run_id"]) == before
            assert restored["context"]["skills"][0]["content"] == original
            assert not RunStore.read(app.state.manager.runs_root, restored["run_id"]).warnings
    asyncio.run(scenario())


def test_missing_skill_followup_can_be_cleared_without_losing_conversation(skill_environment):
    """技能文件在轮间删除导致失败后，用户可清空选择继续；原有会话与两轮证据均保持完整。"""
    async def scenario():
        """完成成功、加载失败、清空再执行三轮，并核对历史任务与真实产物；返回 None。"""
        config, roots = skill_environment
        source, _ = write_skill(roots, "alpha", "STYLE_ALPHA_V1")
        model = SkillWritingModel()
        async with skill_client(config, RunConfig(skill_roots=[roots], tools=["write_file"]), model) as (client, app):
            first = await create_run(client, config={"skills": ["alpha"]})
            assert first["result"]["execution_status"] == "completed"
            first_evidence = evidence(app.state.manager.runs_root, first["run_id"])
            source.unlink()
            calls = len(model.requests)
            failed = await continue_run(client, first, "failed-selected")
            assert failed["result"]["execution_status"] == "failed"
            assert failed["result"]["stop_reason"].startswith("skill_")
            assert failed["result"]["model_turns"] == failed["result"]["tool_calls"] == 0
            assert len(model.requests) == calls
            failed_evidence = evidence(app.state.manager.runs_root, failed["run_id"])
            conversation = (await client.get(f'/api/runs/{failed["run_id"]}/conversation')).json()
            assert conversation["can_continue"] is True, conversation
            cleared = await continue_run(client, failed, "after-clear", config={"skills": []})
            assert cleared["result"]["execution_status"] == "completed", cleared["result"]
            assert cleared["turn_index"] == 3 and cleared["parent_run_id"] == failed["run_id"]
            assert cleared["context"]["skills"] == [] and cleared["context"]["skill_error"] is None
            users = [message["content"] for message in model.requests[calls] if message["role"] == "user"]
            assert users == ["first", "failed-selected", "after-clear"]
            assert (Path(cleared["workspace"]) / "result.txt").read_text(encoding="utf-8") == "NO_SKILL: after-clear\n"
            assert (Path(failed["workspace"]) / "result.txt").read_text(encoding="utf-8") == "STYLE_ALPHA_V1: first\n"
            assert evidence(app.state.manager.runs_root, first["run_id"]) == first_evidence
            assert evidence(app.state.manager.runs_root, failed["run_id"]) == failed_evidence
    asyncio.run(scenario())
