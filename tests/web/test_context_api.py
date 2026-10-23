"""通过真实 ASGI、Runner 和 Git 工作树验证上下文预算与指令快照的公开契约。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from typing import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse
from agent_harness.schemas import ContextPolicy, RunConfig, RunManifest, TaskSpec
from agent_harness.store import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


# 固定离线值只满足任务启动预检，错误响应和持久化记录不得回显它。
SECRET = "offline-context-key-never-serialize"


class RecordingModel:
    """保存真实 Runner 发来的消息副本并立即结束；完全不访问供应商或网络。"""

    def __init__(self) -> None:
        """初始化空请求历史；每个元素保存一次 complete 的 messages/tools 副本，返回 None。"""
        self.requests: list[dict] = []  # 成功进入模型接口的请求；预算拒绝不会追加元素。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """复制 messages 与工具定义后返回固定答案；不修改调用方内容，不读写工作区。"""
        self.requests.append(deepcopy({"messages": messages, "tools": tools}))
        return ModelResponse(
            message={"role": "assistant", "content": "已按项目约定完成离线检查。"},
            tool_calls=[], finish_reason="stop", response_model="offline-context",
        )


@pytest.fixture
def context_web_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebConfig:
    """在 tmp_path 创建普通工作区并配置离线凭据，返回不与其他测试共享记录的服务配置。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return WebConfig(
        project_dir=tmp_path, runtime_root=tmp_path / "runtime" / "samples",
        frontend_dist=tmp_path / "dist",
        workspaces=[WebWorkspace(id="sample", label="上下文工作区", path=workspace)],
    )


@asynccontextmanager
async def context_client(
    config: WebConfig, model: RecordingModel, host_config: RunConfig | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """以 config、离线 model 和可选宿主设置启动真实应用；产出 HTTP 客户端，退出时清理后台任务。"""
    app = create_app(
        config, host_config or RunConfig(), runs_root=config.project_dir / "records", model_client=model,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False,
        ) as client:
            yield client


async def completed(client: httpx.AsyncClient, run_id: str) -> dict:
    """轮询 run_id 至执行与 Git 快照均退出，返回最终视图；超过十秒抛 TimeoutError。"""
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            view = response.json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)


async def create_run(client: httpx.AsyncClient, **overrides) -> dict:
    """合并 overrides 到普通工作区任务并提交；要求 202，等待收尾后返回最终视图。"""
    payload = {"prompt": "按项目约定检查任务", "workspace_id": "sample", **overrides}
    response = await client.post("/api/runs", json=payload)
    assert response.status_code == 202, response.text
    return await completed(client, response.json()["run_id"])


def stream_events(content: str) -> list[dict]:
    """从有限 SSE content 中提取 run_event 的 JSON 数据；忽略心跳与 stream_end，返回顺序列表。"""
    result = []
    for block in content.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" in line and not line.startswith(":"):
                name, value = line.split(":", 1)
                fields[name] = value.lstrip()
        if fields.get("event") == "run_event":
            event = json.loads(fields["data"])
            assert int(fields["id"]) == event["seq"]
            result.append(event)
    return result


def git(repository: Path, *arguments: str) -> bytes:
    """在临时 repository 中运行固定 Git arguments 并返回 stdout；无 shell，失败或超时终止测试。"""
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], check=True, capture_output=True, timeout=10,
    ).stdout


def test_context_patch_keeps_host_defaults_and_snapshot_survives_restart(context_web_config: WebConfig):
    """网页局部打开指令加载须保留宿主字节预算；重启后读取原快照，不重读已变化源文件或重入模型。"""
    instruction = "# 项目约定\n函数需有中文注释，禁止修改 tests 目录。\n"
    path = context_web_config.workspaces[0].path / "AGENTS.md"
    path.write_bytes(instruction.encode("utf-8"))
    host = RunConfig(context=ContextPolicy(
        load_project_instructions=False, max_input_bytes=65536, max_project_instruction_bytes=4096,
    ))

    async def scenario():
        """提交单字段策略覆盖，检查实际请求和落盘快照，再创建新应用确认历史内容稳定。"""
        model = RecordingModel()
        async with context_client(context_web_config, model, host) as client:
            view = await create_run(client, config={"context": {"load_project_instructions": True}})
            assert {key: view["config"]["context"][key] for key in ("load_project_instructions", "max_input_bytes", "max_project_instruction_bytes")} == {
                "load_project_instructions": True, "max_input_bytes": 65536,
                "max_project_instruction_bytes": 4096,
            }
            assert (await client.get("/api/options")).json()["config"]["context"]["load_project_instructions"] is False
            context = view["context"]
            assert context["serialization"] == "json_utf8_v1"
            assert context["instruction"] == {
                "status": "loaded", "path": "AGENTS.md", "content": instruction,
                "sha256": sha256(instruction.encode("utf-8")).hexdigest(),
                "size_bytes": len(instruction.encode("utf-8")), "error_code": None,
            }
            assert len(model.requests) == 1
            messages = model.requests[0]["messages"]
            assert any(instruction in (message.get("content") or "") for message in messages)
            assert context["last_check"]["message_count"] == len(messages)
            assert context["last_check"]["max_input_bytes"] == 65536
            assert context["last_check"]["allowed"] is True
        path.write_text("# 已修改的约定\n这段不得覆盖旧任务快照。\n", encoding="utf-8")
        restored_model = RecordingModel()
        async with context_client(context_web_config, restored_model, host) as client:
            response = await client.get(f"/api/runs/{view['run_id']}")
            assert response.status_code == 200
            assert response.json()["context"] == context
            assert response.json()["result"] == view["result"]
            assert restored_model.requests == []
        history = RunStore.read(context_web_config.project_dir / "records", view["run_id"])
        prepared = [event for event in history.events if event.type == "context_prepared"]
        assert len(prepared) == 1
        assert prepared[0].payload["instruction"]["content"] == instruction
        assert SECRET not in json.dumps([event.model_dump(mode="json") for event in history.events])

    asyncio.run(scenario())


def test_first_input_over_budget_has_zero_model_and_http_attempts(context_web_config: WebConfig):
    """首轮输入超限应保存拒绝检查并退出；拟请求轮次为一，实际模型轮次与 HTTP 尝试仍为零。"""
    async def scenario():
        """从页面式请求设置小预算，确认结束原因、检查数值与实际模型入口次数完全一致。"""
        model = RecordingModel()
        async with context_client(context_web_config, model) as client:
            view = await create_run(
                client, prompt="说明输入预算如何处理。" * 500,
                config={"context": {"max_input_bytes": 1024}},
            )
            assert view["result"]["execution_status"] == "limit_exceeded"
            assert view["result"]["stop_reason"] == "max_model_input_bytes"
            assert view["result"]["model_turns"] == 0
            assert view["result"]["http_attempts"] == 0
            check = view["context"]["last_check"]
            assert check["model_turn"] == 1
            assert check["allowed"] is False
            assert check["max_input_bytes"] == 1024
            assert check["input_bytes"] > check["max_input_bytes"]
            assert model.requests == []
            events = (await client.get(f"/api/runs/{view['run_id']}/events")).json()["items"]
            assert len([item for item in events if item["type"] == "context_checked"]) == 1
            assert not any(item["type"] == "model_request_started" for item in events)

    asyncio.run(scenario())


def test_legacy_history_without_context_events_stays_null(context_web_config: WebConfig):
    """旧清单缺少 context 且没有上下文事件时返回 null；即便工作区现有 AGENTS，也不补造历史或触发模型。"""
    identifier = uuid4()
    root = context_web_config.project_dir / "records"
    store = RunStore.create(root, RunManifest(
        run_id=identifier, task=TaskSpec(prompt="旧版本中断任务", workspace=context_web_config.workspaces[0].path),
        config=RunConfig(),
    ))
    store.append("run_started", {})
    manifest_path = root / str(identifier) / "run.json"
    legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy["config"].pop("context")
    manifest_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    (context_web_config.workspaces[0].path / "AGENTS.md").write_text("不属于旧任务的约定。", encoding="utf-8")
    events_path = root / str(identifier) / "events.jsonl"
    before = events_path.read_bytes()

    async def scenario():
        """新应用只读恢复缺少终态的记录，验证详情和事件不会因新增功能而自动写入内容。"""
        model = RecordingModel()
        async with context_client(context_web_config, model) as client:
            response = await client.get(f"/api/runs/{identifier}")
            assert response.status_code == 200, response.text
            view = response.json()
            assert view["context"] is None
            assert view["interrupted"] is True
            assert view["last_seq"] == 2
            assert model.requests == []
        assert events_path.read_bytes() == before

    asyncio.run(scenario())


def test_context_events_match_pagination_sse_and_view(context_web_config: WebConfig):
    """分页、SSE 和运行视图应来自同一上下文事实；断点续读不漏掉准备与预算事件，也不重复序号。"""
    (context_web_config.workspaces[0].path / "AGENTS.md").write_text("执行前先核对目录。\n", encoding="utf-8")

    async def scenario():
        """用每页两条事件跨页收集，再比较完整 SSE 和游标 SSE，并核对视图派生字段。"""
        async with context_client(context_web_config, RecordingModel()) as client:
            view = await create_run(client)
            endpoint = f"/api/runs/{view['run_id']}"
            cursor = 0
            events = []
            while True:
                response = await client.get(endpoint + "/events", params={"after": cursor, "limit": 2})
                assert response.status_code == 200
                page = response.json()
                events.extend(page["items"])
                if page["items"]:
                    cursor = page["items"][-1]["seq"]
                if not page["has_more"]:
                    break
            assert [event["seq"] for event in events] == list(range(1, view["last_seq"] + 1))
            response = await client.get(endpoint + "/stream")
            assert response.status_code == 200
            assert stream_events(response.text) == events
            prepared = next(event for event in events if event["type"] == "context_prepared")
            checked = next(event for event in events if event["type"] == "context_checked")
            assert prepared["payload"]["instruction"] == view["context"]["instruction"]
            assert prepared["payload"]["serialization"] == view["context"]["serialization"]
            assert checked["payload"] == view["context"]["last_check"]
            response = await client.get(endpoint + "/stream", headers={"Last-Event-ID": str(prepared["seq"])})
            assert stream_events(response.text) == [event for event in events if event["seq"] > prepared["seq"]]

    asyncio.run(scenario())


def test_invalid_context_configuration_is_safe_and_creates_no_records(context_web_config: WebConfig):
    """未知路径、越界数值与错误类型都须在建记录前拒绝；错误不回显输入，宿主默认值保持不变。"""
    async def scenario():
        """逐一提交恶意或无效策略，检查受控错误包装、零记录和零模型请求。"""
        model = RecordingModel()
        invalid = [
            {"instructions_path": SECRET}, {"max_input_bytes": 0}, {"max_input_bytes": True},
            {"max_input_bytes": "1024"}, {"max_project_instruction_bytes": -1},
            {"load_project_instructions": "false"}, {"max_input_bytes": None},
        ]
        async with context_client(context_web_config, model) as client:
            initial = (await client.get("/api/options")).json()["config"]["context"]
            for policy in invalid:
                response = await client.post("/api/runs", json={
                    "prompt": "应在启动前拒绝", "workspace_id": "sample", "config": {"context": policy},
                })
                assert response.status_code in {400, 422}, response.text
                assert set(response.json()) == {"error"}
                assert SECRET not in response.text
                root = context_web_config.project_dir / "records"
                assert not root.exists() or not list(root.iterdir())
            assert (await client.get("/api/options")).json()["config"]["context"] == initial
            assert model.requests == []

    asyncio.run(scenario())


def test_git_project_loads_instruction_from_fixed_worktree_not_dirty_source(context_web_config: WebConfig):
    """Git 项目应读取固定提交工作树中的 AGENTS；源目录未提交规则保持原样，模型和快照不混入它。"""
    source = context_web_config.project_dir / "source"
    source.mkdir()
    baseline = "# 提交中的约定\n只读检查，禁止修改现有测试。\n"
    dirty = "# 源目录未提交规则\n此版本不应进入任务。\n"
    instruction_path = source / "AGENTS.md"
    instruction_path.write_bytes(baseline.encode("utf-8"))
    git(source, "init", "-q")
    git(source, "config", "core.autocrlf", "false")
    git(source, "add", ".")
    git(source, "-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "context baseline")
    instruction_path.write_text(dirty, encoding="utf-8")
    before_status = git(source, "status", "--porcelain=v1")
    context_web_config.projects = [ProjectPreset(
        id="project", label="指令基线项目", path=source,
        test_argv=[sys.executable, "-c", "print('host check passed')"], test_timeout_seconds=10.0,
        test_python_paths=["."],
    )]

    async def scenario():
        """提交真实项目任务，核对基线规则与模型输入，再通过既有审阅接口丢弃测试工作树。"""
        model = RecordingModel()
        async with context_client(context_web_config, model) as client:
            response = await client.post("/api/runs", json={"prompt": "只读核对项目规则", "project_id": "project"})
            assert response.status_code == 202, response.text
            view = await completed(client, response.json()["run_id"])
            assert view["result"]["execution_status"] == "completed"
            assert view["result"]["validation_status"] == "passed"
            assert view["context"]["instruction"]["content"] == baseline
            assert Path(view["workspace"]) != source
            assert (Path(view["workspace"]) / "AGENTS.md").read_text(encoding="utf-8") == baseline
            assert len(model.requests) == 1
            messages = model.requests[0]["messages"]
            assert any(baseline in (message.get("content") or "") for message in messages)
            assert not any(dirty in (message.get("content") or "") for message in messages)
            discarded = await client.post(f"/api/runs/{view['run_id']}/review", json={"decision": "discard"})
            assert discarded.status_code == 200, discarded.text
        assert instruction_path.read_text(encoding="utf-8") == dirty
        assert git(source, "status", "--porcelain=v1") == before_status

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing_content", "wrong_hash", "wrong_size", "contradicting_check"])
def test_semantically_damaged_context_is_warned_without_hiding_raw_events(
    context_web_config: WebConfig, damage: str,
):
    """damage 指定快照或预算的语义矛盾；详情返回警告与 null，原始事件保留且不触发模型。"""
    identifier = uuid4()
    root = context_web_config.project_dir / "records"
    store = RunStore.create(root, RunManifest(
        run_id=identifier,
        task=TaskSpec(prompt="包含受损上下文的历史任务", workspace=context_web_config.workspaces[0].path),
        config=RunConfig(),
    ))
    content = "完整项目约定。"
    snapshot = {
        "status": "loaded", "path": "AGENTS.md", "content": content,
        "sha256": sha256(content.encode("utf-8")).hexdigest(),
        "size_bytes": len(content.encode("utf-8")), "error_code": None,
    }
    if damage == "missing_content":
        snapshot.pop("content")
    elif damage == "wrong_hash":
        snapshot["sha256"] = "0" * 64
    elif damage == "wrong_size":
        snapshot["size_bytes"] = 999
    store.append("context_prepared", {"instruction": snapshot, "serialization": "json_utf8_v1"})
    store.append("run_started", {})
    if damage == "contradicting_check":
        store.append("context_checked", {
            "model_turn": 1, "input_bytes": 2000, "max_input_bytes": 1000,
            "message_count": 3, "allowed": True,
        })
    events_path = root / str(identifier) / "events.jsonl"
    before = events_path.read_bytes()
    expected_events = [event.model_dump(mode="json") for event in RunStore.read(root, str(identifier)).events]

    async def scenario():
        """创建只读应用核对详情降级和事件可见性；生命周期退出后记录字节必须保持原样。"""
        model = RecordingModel()
        async with context_client(context_web_config, model) as client:
            response = await client.get(f"/api/runs/{identifier}")
            assert response.status_code == 200, response.text
            view = response.json()
            assert view["context"] is None
            assert any("上下文记录格式无效" in warning for warning in view["warnings"])
            response = await client.get(f"/api/runs/{identifier}/events")
            assert response.status_code == 200, response.text
            assert response.json()["items"] == expected_events
            assert model.requests == []
        assert events_path.read_bytes() == before

    asyncio.run(scenario())
