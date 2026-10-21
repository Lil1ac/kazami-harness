"""通过真实 Runner 和离线模型验证本地控制台 API 的持久化与生命周期契约。"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from typing import AsyncIterator

import httpx
import pytest

from agent_harness.config import ConfigurationError
from agent_harness.providers.base import ModelResponse
from agent_harness.schemas import RunConfig
from agent_harness.store import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebVerifier, WebWorkspace, load_web_config


# 固定离线凭据只用于启动预检，任何响应与运行记录都不能出现其值。
SECRET = "offline-web-key-never-serialize"
# 仓库中的宿主检查程序由测试直接复用，避免复制一份实现后相互证明。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FinalModel:
    """立即给出最终回答的离线模型，保留调用计数供验收阶段不重入模型的断言。"""

    def __init__(self) -> None:
        """初始化调用次数为零；不读取凭据、不访问网络，返回 None。"""
        self.calls = 0  # 实际进入 complete 的次数，跨同一应用的任务累计。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收 Runner 的历史与工具定义并返回固定最终回答；没有文件或网络副作用。"""
        self.calls += 1
        return ModelResponse(
            message={"role": "assistant", "content": "离线任务已完成。"},
            tool_calls=[], finish_reason="stop", response_model="offline-test",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )


class GatedModel(FinalModel):
    """通过事件明确控制请求开始和完成，供并发、取消、断流与停服测试使用。"""

    def __init__(self) -> None:
        """建立未置位的开始和释放事件；事件只由测试所在线程的异步循环使用。"""
        super().__init__()
        self.entered = asyncio.Event()  # 模型请求已经开始，可安全触发取消或第二次提交。
        self.release = asyncio.Event()  # 测试置位后才返回回答；取消时由 Runner 接收异常。
        self.cancelled = False  # 只有等待确实接收取消时才变为 True。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """通知请求开始并等待 release；取消时保留事实后重抛，正常时返回固定回答。"""
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().complete(messages, tools)


@pytest.fixture
def web_config(tmp_path: Path) -> WebConfig:
    """在 tmp_path 建立工作区和静态页；返回隔离配置，不创建运行记录或请求模型。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frontend = tmp_path / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>test-console</title>", encoding="utf-8")
    (frontend / "assets" / "app.js").write_text("console.log('console')", encoding="utf-8")
    return WebConfig(
        project_dir=tmp_path, runtime_root=tmp_path / "runtime", frontend_dist=frontend,
        workspaces=[WebWorkspace(id="sample", label="隔离工作区", path=workspace)],
        verifiers=[],
    )


@asynccontextmanager
async def api_client(config: WebConfig, model: FinalModel | None = None) -> AsyncIterator[tuple]:
    """以 config 和离线 model 启动真实应用生命周期，产出 (HTTP客户端,应用) 并在退出时收尾。"""
    app = create_app(config, RunConfig(), runs_root=config.project_dir / "records", model_client=model or FinalModel())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            yield client, app


async def submit(client: httpx.AsyncClient, **overrides) -> dict:
    """合并 overrides 到最小合法任务并提交；要求接纳为202，返回 RunView JSON。"""
    response = await client.post("/api/runs", json={"prompt": "执行离线任务", "workspace_id": "sample", **overrides})
    assert response.status_code == 202, response.text
    return response.json()


async def finished(client: httpx.AsyncClient, identifier: str) -> dict:
    """在最多5秒内读取 identifier 直至后台任务退出，返回最终 RunView；超时抛 TimeoutError。"""
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/api/runs/{identifier}")
            assert response.status_code == 200, response.text
            view = response.json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)


def sse_frames(content: str) -> list[dict]:
    """把有限 SSE 文本解码为事件列表，保留 event/id 和 JSON data；忽略心跳注释，返回新列表。"""
    frames = []
    for block in content.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" in line and not line.startswith(":"):
                name, value = line.split(":", 1)
                fields[name] = value.lstrip()
        if "data" in fields:
            fields["data"] = json.loads(fields["data"])
            frames.append(fields)
    return frames


def test_submit_finishes_in_background_and_persists_unverified(web_config: WebConfig, monkeypatch):
    """使用事件模型验证提交后请求可返回、跨HTTP请求继续运行且最终结果与磁盘一致；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """在模型阻塞时检查运行，再释放并比较运行记录；所有等待均有5秒上限。"""
        model = GatedModel()
        async with api_client(web_config, model) as (client, app):
            created = await submit(client)
            identifier = created["run_id"]
            await asyncio.wait_for(model.entered.wait(), 5)
            during = (await client.get(f"/api/runs/{identifier}")).json()
            assert during["active"] is True
            assert during["result"]["execution_status"] == "running"
            assert (await client.get("/api/options")).json()["busy_run_id"] == identifier
            model.release.set()
            view = await finished(client, identifier)
            assert view["result"]["execution_status"] == "completed"
            assert view["result"]["validation_status"] == "unverified"
            assert view["result"]["final_answer"] == "离线任务已完成。"
            history = RunStore.read(web_config.project_dir / "records", identifier)
            assert history.result.model_dump(mode="json") == view["result"]
            assert [event.type for event in history.events].count("run_finished") == 1
            assert model.calls == 1
            assert (await client.get("/api/options")).json()["busy_run_id"] is None

    asyncio.run(scenario())


def test_busy_rejects_second_submission_without_second_record(web_config: WebConfig, monkeypatch):
    """首任务等待时第二次提交返回409且没有额外记录；取消首任务后正常收尾，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """通过开始事件确定并发窗口，读取真实目录确认只创建一次运行记录。"""
        model = GatedModel()
        async with api_client(web_config, model) as (client, app):
            first = await submit(client)
            await asyncio.wait_for(model.entered.wait(), 5)
            response = await client.post("/api/runs", json={"prompt": "第二个任务", "workspace_id": "sample"})
            assert response.status_code == 409
            assert response.json()["error"]["code"]
            assert len([path for path in (web_config.project_dir / "records").iterdir() if path.is_dir()]) == 1
            model.release.set()
            await finished(client, first["run_id"])

    asyncio.run(scenario())


def test_api_cancel_is_idempotent_and_keeps_unique_terminal(web_config: WebConfig, monkeypatch):
    """网页取消必须记录api_user来源；重复取消无重复终态，模型确实收到取消，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """模型开始后请求两次取消，检查落盘事实和取消来源，不依赖固定睡眠时机。"""
        model = GatedModel()
        async with api_client(web_config, model) as (client, app):
            created = await submit(client)
            identifier = created["run_id"]
            await asyncio.wait_for(model.entered.wait(), 5)
            assert (await client.post(f"/api/runs/{identifier}/cancel")).status_code == 200
            assert (await client.post(f"/api/runs/{identifier}/cancel")).status_code == 200
            view = await finished(client, identifier)
            assert view["result"]["execution_status"] == "cancelled"
            assert view["result"]["stop_reason"] == "user_cancelled"
            assert model.cancelled is True
            assert (await client.post(f"/api/runs/{identifier}/cancel")).status_code == 200
            events = RunStore.read(web_config.project_dir / "records", identifier).events
            assert [item.type for item in events].count("run_finished") == 1
            cancellations = [item for item in events if item.type == "cancellation_received"]
            assert len(cancellations) == 1
            assert cancellations[0].payload["source"] == "api_user"

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"workspace_id": "unknown"},
    {"verifier_id": "unknown"},
    {"config": {"limits": {"max_model_turns": 0}}},
    {"config": {"api_key": SECRET}},
    {"verifier": {"argv": [SECRET]}},
    {"workspace": SECRET},
    {"expected_outputs": ["../outside.txt"]},
])
def test_invalid_submission_has_safe_error_and_no_record(web_config: WebConfig, monkeypatch, payload: dict):
    """payload为无效或越界的任务覆盖；要求启动前拒绝且不回显敏感输入、不落盘，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """提交非法字段或预置ID，并检查统一错误包装和记录根目录的零副作用。"""
        async with api_client(web_config) as (client, app):
            response = await client.post("/api/runs", json={"prompt": "离线任务", "workspace_id": "sample", **payload})
            assert response.status_code in {400, 404, 422}
            assert set(response.json()) == {"error"}
            assert set(response.json()["error"]) >= {"code", "message"}
            assert SECRET not in response.text
            root = web_config.project_dir / "records"
            assert not root.exists() or not list(root.iterdir())

    asyncio.run(scenario())


def test_missing_key_is_rejected_before_record_creation(web_config: WebConfig, monkeypatch):
    """未配置模型密钥时网页可读取选项，但提交任务应在创建记录前失败；返回 None。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    async def scenario():
        """读取脱敏选项后尝试提交，再确认运行根目录没有任务文件。"""
        async with api_client(web_config) as (client, app):
            assert (await client.get("/api/options")).json()["key_configured"] is False
            response = await client.post("/api/runs", json={"prompt": "离线任务", "workspace_id": "sample"})
            assert response.status_code in {400, 422, 503}
            assert response.json()["error"]["code"]
            root = web_config.project_dir / "records"
            assert not root.exists() or not list(root.iterdir())

    asyncio.run(scenario())


@pytest.mark.parametrize("valid", [True, False])
def test_host_log_verifier_runs_after_model_and_exposes_real_result(web_config: WebConfig, monkeypatch, valid: bool):
    """valid控制真实报告是否正确，要求宿主检查器得到对应passed/failed并公开双流产物，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    report = {"counts": {"auth": 1, "database": 2, "timeout": 3}, "total_errors": 6}
    if not valid:
        report["total_errors"] = 7
    (web_config.workspaces[0].path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    config_file = web_config.project_dir / "checker.toml"
    argv = [sys.executable, str(PROJECT_ROOT / "examples" / "verifiers" / "verify_logs.py")]
    config_file.write_text("argv = " + json.dumps(argv) + "\ntimeout_seconds = 5.0\n", encoding="utf-8")
    web_config.verifiers = [WebVerifier(id="logs", label="日志检查", description="固定样例检查", path=config_file)]

    async def scenario():
        """运行真实本地验收子进程，查询保存的详情和可读取的运行内stdout产物。"""
        model = FinalModel()
        async with api_client(web_config, model) as (client, app):
            created = await submit(client, verifier_id="logs", expected_outputs=["report.json"])
            view = await finished(client, created["run_id"])
            result = view["result"]
            assert result["execution_status"] == "completed"
            assert result["validation_status"] == ("passed" if valid else "failed")
            assert result["verification"]["exit_code"] == (0 if valid else 1)
            assert model.calls == 1
            streams = result["verification"]["artifacts"]
            assert {item["kind"] for item in streams} == {"stdout", "stderr"}
            stdout = next(item for item in streams if item["kind"] == "stdout")
            preview = await client.get(f"/api/runs/{created['run_id']}/artifacts", params={"location": "run", "path": stdout["path"], "preview": "true"})
            assert preview.status_code == 200
            assert json.loads(preview.json()["content"])["status"] == ("passed" if valid else "failed")
            history = RunStore.read(web_config.project_dir / "records", created["run_id"])
            assert [item.type for item in history.events].count("verification_result") == 1
            assert [item.type for item in history.events].count("run_finished") == 1

    asyncio.run(scenario())


def test_history_pagination_filter_and_options_never_serialize_key(web_config: WebConfig, monkeypatch):
    """顺序创建两次真实运行后验证历史分页和状态过滤，所有API响应均不泄露凭据，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """保存两个完成任务后读取不同页和脱敏配置，确认任务身份未被分页重复。"""
        async with api_client(web_config) as (client, app):
            first = await submit(client, prompt="第一个任务")
            await finished(client, first["run_id"])
            second = await submit(client, prompt="第二个任务", config={"limits": {"max_model_turns": 2}})
            second_view = await finished(client, second["run_id"])
            page1 = await client.get("/api/runs", params={"offset": 0, "limit": 1})
            page2 = await client.get("/api/runs", params={"offset": 1, "limit": 1})
            assert page1.json()["total"] == page2.json()["total"] == 2
            assert {page1.json()["items"][0]["run_id"], page2.json()["items"][0]["run_id"]} == {first["run_id"], second["run_id"]}
            assert page1.json()["items"][0]["run_id"] == second["run_id"]
            assert (await client.get("/api/runs", params={"status": "completed"})).json()["total"] == 2
            assert (await client.get("/api/runs", params={"status": "cancelled"})).json()["items"] == []
            assert second_view["config"]["limits"]["max_model_turns"] == 2
            assert second_view["config"]["limits"]["max_tool_calls"] == RunConfig().limits.max_tool_calls
            options = await client.get("/api/options")
            assert options.json()["key_configured"] is True
            assert SECRET not in "".join([page1.text, page2.text, options.text, json.dumps(second_view)])

    asyncio.run(scenario())


def test_sse_replays_exclusive_cursor_and_finite_terminal(web_config: WebConfig, monkeypatch):
    """已完成任务SSE按独占游标补发，事件唯一连续，Last-Event-ID生效且最后关闭；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """以分页事件作为事实源，分别比较query游标和重连header得到的有限SSE。"""
        async with api_client(web_config) as (client, app):
            created = await submit(client)
            identifier = created["run_id"]
            view = await finished(client, identifier)
            first = (await client.get(f"/api/runs/{identifier}/events", params={"after": 0, "limit": 2})).json()
            assert len(first["items"]) == 2
            assert first["has_more"] is True
            cursor = first["items"][-1]["seq"]
            tail = (await client.get(f"/api/runs/{identifier}/events", params={"after": cursor, "limit": 100})).json()
            expected = [item["seq"] for item in tail["items"]]
            assert all(seq > cursor for seq in expected)
            for params, headers in [({"after": cursor}, {}), ({}, {"Last-Event-ID": str(cursor)})]:
                stream = await client.get(f"/api/runs/{identifier}/stream", params=params, headers=headers)
                assert stream.status_code == 200
                assert "text/event-stream" in stream.headers["content-type"]
                frames = sse_frames(stream.text)
                run_events = [item for item in frames if item.get("event") == "run_event"]
                assert [item["data"]["seq"] for item in run_events] == expected
                assert [int(item["id"]) for item in run_events] == expected
                assert len(set(expected)) == len(expected)
                assert frames[-1]["event"] == "stream_end"
                assert frames[-1]["data"]["last_seq"] == view["last_seq"]
                assert frames[-1]["data"]["interrupted"] is False

    asyncio.run(scenario())


def test_sse_disconnect_keeps_background_run_alive(web_config: WebConfig, monkeypatch):
    """用真实ASGI断连事件关闭订阅，确认这不会把模型任务取消，重新查询后仍可完成；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """在首条SSE消息发送后通知http.disconnect，使用ASGI2.3保证真实断流清理路径。"""
        model = GatedModel()
        async with api_client(web_config, model) as (client, app):
            created = await submit(client)
            await asyncio.wait_for(model.entered.wait(), 5)
            disconnect = asyncio.Event()
            request_sent = False
            seen_body = []

            async def receive():
                """首次提供空GET请求，之后等断开信号并返回http.disconnect，不接触运行任务。"""
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict):
                """收到首段SSE正文后标记断开；message是ASGI响应事件，返回 None。"""
                if message["type"] == "http.response.body" and message.get("body"):
                    seen_body.append(message["body"])
                    disconnect.set()

            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": f"/api/runs/{created['run_id']}/stream", "query_string": b"after=0",
                "root_path": "", "headers": [(b"host", b"localhost")],
                "client": ("127.0.0.1", 12345), "server": ("localhost", 80),
            }
            await asyncio.wait_for(app(scope, receive, send), 5)
            assert seen_body
            assert model.cancelled is False
            assert (await client.get(f"/api/runs/{created['run_id']}")).json()["active"] is True
            model.release.set()
            assert (await finished(client, created["run_id"]))["result"]["execution_status"] == "completed"

    asyncio.run(scenario())


def test_artifacts_require_registered_relative_reference(web_config: WebConfig, monkeypatch):
    """只允许访问已登记产物，拒绝穿越、绝对路径、未登记文件及内部manifest；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    workspace = web_config.workspaces[0].path
    (workspace / "answer.txt").write_text("中文产物\n", encoding="utf-8", newline="")
    (workspace / "private.txt").write_text(SECRET, encoding="utf-8")

    async def scenario():
        """生成有登记产物的真实结果，分别检查文本预览、下载与不安全请求。"""
        async with api_client(web_config) as (client, app):
            created = await submit(client, expected_outputs=["answer.txt"])
            identifier = created["run_id"]
            await finished(client, identifier)
            endpoint = f"/api/runs/{identifier}/artifacts"
            preview = await client.get(endpoint, params={"location": "workspace", "path": "answer.txt", "preview": "true"})
            assert preview.status_code == 200
            assert preview.json() == {"content": "中文产物\n", "truncated": False, "size_bytes": len("中文产物\n".encode()), "encoding": "utf-8"}
            download = await client.get(endpoint, params={"location": "workspace", "path": "answer.txt"})
            assert download.status_code == 200
            assert download.content.decode() == "中文产物\n"
            assert "attachment" in download.headers["content-disposition"]
            for location, path in [("workspace", "private.txt"), ("workspace", "../private.txt"), ("workspace", str(workspace / "private.txt")), ("run", "manifest.json")]:
                denied = await client.get(endpoint, params={"location": location, "path": path, "preview": "true"})
                assert denied.status_code in {400, 403, 404, 422}
                assert SECRET not in denied.text

    asyncio.run(scenario())


def test_registered_artifact_retargeted_to_outside_symlink_is_denied(web_config: WebConfig, monkeypatch):
    """登记后将文件替换为指向工作区外的链接，下载必须再次核查边界；平台无权限时跳过。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    artifact = web_config.workspaces[0].path / "answer.txt"
    artifact.write_text("原始产物", encoding="utf-8")
    outside = web_config.project_dir / "outside.txt"
    outside.write_text(SECRET, encoding="utf-8")

    async def scenario():
        """先结束真实运行并登记文件，再改变链接目标，检查后续访问不会跟随到外部。"""
        async with api_client(web_config) as (client, app):
            created = await submit(client, expected_outputs=["answer.txt"])
            await finished(client, created["run_id"])
            artifact.unlink()
            try:
                artifact.symlink_to(outside)
            except OSError:
                pytest.skip("当前Windows账户没有创建符号链接的权限")
            response = await client.get(f"/api/runs/{created['run_id']}/artifacts", params={"location": "workspace", "path": "answer.txt", "preview": "true"})
            assert response.status_code in {400, 403, 404}
            assert SECRET not in response.text

    asyncio.run(scenario())


def test_static_spa_fallback_never_masks_api_or_asset_errors(web_config: WebConfig):
    """客户端路由回退到index，但未知API和缺失静态资源仍是404，防止错误被HTML掩盖；返回 None。"""

    async def scenario():
        """读取真实静态文件和深层页面，再检查两个不应触发SPA回退的路径。"""
        async with api_client(web_config) as (client, app):
            for path in ["/", "/runs/example", "/settings"]:
                response = await client.get(path)
                assert response.status_code == 200
                assert "test-console" in response.text
            assert (await client.get("/assets/app.js")).status_code == 200
            for path in ["/api/missing", "/assets/missing.js"]:
                response = await client.get(path)
                assert response.status_code == 404
                assert "test-console" not in response.text
            unknown_api = await client.get("/api/missing")
            assert set(unknown_api.json()) == {"error"}

    asyncio.run(scenario())


def test_shutdown_cancels_and_persists_active_run(web_config: WebConfig, monkeypatch):
    """应用停服主动收尾后台任务，记录service_shutdown且唯一终态完整落盘；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """退出生命周期触发停服取消，不手动释放模型，再读取持久化终态检查未遗留运行。"""
        model = GatedModel()
        async with asyncio.timeout(5):
            async with api_client(web_config, model) as (client, app):
                created = await submit(client)
                await asyncio.wait_for(model.entered.wait(), 5)
        assert model.cancelled is True
        history = RunStore.read(web_config.project_dir / "records", created["run_id"])
        assert history.interrupted is False
        assert history.result.execution_status == "cancelled"
        assert history.result.stop_reason == "service_shutdown"
        assert [item.type for item in history.events].count("run_finished") == 1
        event = next(item for item in history.events if item.type == "cancellation_received")
        assert event.payload["source"] == "service_shutdown"

    asyncio.run(scenario())


def test_copied_workspace_isolated_per_submission(web_config: WebConfig, monkeypatch):
    """复制型工作区每次得到独立目录，API返回实际副本且修改副本不污染源样例；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    web_config.workspaces[0].copy_on_run = True
    source = web_config.workspaces[0].path
    (source / "input.txt").write_text("原始输入", encoding="utf-8")

    async def scenario():
        """连续提交并修改首个副本，第二次仍从源目录复制原始输入。"""
        async with api_client(web_config) as (client, app):
            first = await submit(client)
            first_view = await finished(client, first["run_id"])
            first_workspace = Path(first_view["workspace"])
            assert first_workspace != source
            assert first_workspace.is_relative_to(web_config.runtime_root)
            assert (first_workspace / "input.txt").read_text(encoding="utf-8") == "原始输入"
            (first_workspace / "input.txt").write_text("已修改", encoding="utf-8")
            second = await submit(client)
            second_view = await finished(client, second["run_id"])
            second_workspace = Path(second_view["workspace"])
            assert second_workspace != first_workspace
            assert (second_workspace / "input.txt").read_text(encoding="utf-8") == "原始输入"
            assert (source / "input.txt").read_text(encoding="utf-8") == "原始输入"

    asyncio.run(scenario())


def test_load_web_config_resolves_profile_relative_paths(tmp_path: Path):
    """显式TOML的相对目录以配置文件父目录解释，返回绝对预设，不创建运行记录。"""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "input").mkdir()
    (profile / "checker.toml").write_text('argv = ["python"]\n', encoding="utf-8")
    path = profile / "web.toml"
    path.write_text(
        'runtime_root = "runtime"\nfrontend_dist = "dist"\n'
        '[[workspaces]]\nid = "data"\nlabel = "输入"\npath = "input"\ncopy_on_run = true\n'
        '[[verifiers]]\nid = "check"\nlabel = "检查"\npath = "checker.toml"\n',
        encoding="utf-8",
    )
    config = load_web_config(path, project_dir=tmp_path)
    assert config.runtime_root == profile / "runtime"
    assert config.frontend_dist == profile / "dist"
    assert config.workspaces[0].path == profile / "input"
    assert config.workspaces[0].copy_on_run is True
    assert config.verifiers[0].path == profile / "checker.toml"
    assert not config.runtime_root.exists()


@pytest.mark.parametrize("content", [
    'secret = "offline-web-key-never-serialize"\n',
    '[[workspaces]]\nid="data"\nlabel="输入"\npath="missing"\n',
    '[[workspaces]]\nid="data"\nlabel="输入"\npath="."\n[[workspaces]]\nid="data"\nlabel="重复"\npath="."\n',
    '[[workspaces]]\nid="data"\nlabel="输入"\npath="."\n[[verifiers]]\nid="check"\nlabel="检查"\npath="missing.toml"\n',
    '[[workspaces]]\nid="data"\nlabel="输入"\npath="."\nargv=["offline-web-key-never-serialize"]\n',
])
def test_invalid_host_profiles_fail_without_echoing_values(tmp_path: Path, content: str):
    """content覆盖重复ID、缺失目录和越权字段；加载只抛脱敏配置错误，不创建运行目录。"""
    path = tmp_path / "web.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError) as failure:
        load_web_config(path, project_dir=tmp_path)
    assert SECRET not in str(failure.value)
    assert not (tmp_path / "runtime").exists()


def test_historical_unfinished_run_is_interrupted_without_fabricated_terminal(web_config: WebConfig):
    """服务未持有的已开始历史标为中断，保留真实事件且SSE明确结束，不伪造取消或失败。"""
    from uuid import uuid4
    from agent_harness.schemas import RunManifest, TaskSpec

    identifier = uuid4()
    store = RunStore.create(
        web_config.project_dir / "records",
        RunManifest(run_id=identifier, task=TaskSpec(prompt="旧进程未完成任务", workspace=web_config.workspaces[0].path), config=RunConfig()),
    )
    store.append("run_started", {})

    async def scenario():
        """新建应用只读取旧进程事实，查询详情与SSE后再次确认磁盘未被补写终态。"""
        async with api_client(web_config) as (client, app):
            view = (await client.get(f"/api/runs/{identifier}")).json()
            assert view["active"] is False
            assert view["interrupted"] is True
            assert view["result"]["execution_status"] == "running"
            assert view["result"]["stop_reason"] is None
            assert view["last_seq"] == 2
            stream = await client.get(f"/api/runs/{identifier}/stream")
            frames = sse_frames(stream.text)
            assert frames[-1]["event"] == "stream_end"
            assert frames[-1]["data"] == {"last_seq": 2, "interrupted": True}
            assert (await client.post(f"/api/runs/{identifier}/cancel")).status_code == 200
            assert (await client.get("/api/options")).json()["busy_run_id"] is None
        history = RunStore.read(web_config.project_dir / "records", str(identifier))
        assert len(history.events) == 2
        assert history.result is None

    asyncio.run(scenario())


def test_completed_history_survives_new_app_instance(web_config: WebConfig, monkeypatch):
    """完成任务可以由新应用实例恢复详情和事件，无需再次调用模型；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """关闭第一实例后创建第二实例，比较磁盘恢复的终态与事件序号。"""
        async with api_client(web_config) as (client, app):
            created = await submit(client)
            original = await finished(client, created["run_id"])
        restored_model = FinalModel()
        async with api_client(web_config, restored_model) as (client, app):
            response = await client.get(f"/api/runs/{created['run_id']}")
            restored = response.json()
            assert restored["result"] == original["result"]
            assert restored["last_seq"] == original["last_seq"]
            assert restored["active"] is False
            assert restored["interrupted"] is False
            assert restored_model.calls == 0
            assert (await client.get("/api/runs")).json()["total"] == 1

    asyncio.run(scenario())


def test_cross_origin_or_foreign_host_cannot_create_task(web_config: WebConfig, monkeypatch):
    """跨站页面和外来Host请求在任务创建前拒绝；正常同源仍可提交，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """逐一请求跨Origin、跨站标记和伪造Host，再用同源请求证明服务仍可用。"""
        async with api_client(web_config) as (client, app):
            for headers, status in [
                ({"Origin": "https://untrusted.example"}, 403),
                ({"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}, 403),
                ({"Host": "untrusted.example"}, 400),
            ]:
                response = await client.post("/api/runs", headers=headers, json={"prompt": "不应启动", "workspace_id": "sample"})
                assert response.status_code == status
                assert set(response.json()) == {"error"}
                root = web_config.project_dir / "records"
                assert not root.exists() or not list(root.iterdir())
            response = await client.post("/api/runs", headers={"Origin": "http://localhost"}, json={"prompt": "同源请求", "workspace_id": "sample"})
            assert response.status_code == 202
            await finished(client, response.json()["run_id"])

    asyncio.run(scenario())


def test_binary_download_and_bounded_unicode_preview(web_config: WebConfig, monkeypatch):
    """二进制拒绝文本预览但可完整下载，大文本按字节截断仍保持有效UTF8，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    workspace = web_config.workspaces[0].path
    binary = b"\x89PNG\x00\xff\x01"
    text = "a" * 65535 + "中" + "文" * 20
    (workspace / "image.bin").write_bytes(binary)
    (workspace / "large.txt").write_bytes(text.encode("utf-8"))

    async def scenario():
        """用真实登记产物请求有界预览和附件，检查类型、截断边界与原始字节。"""
        async with api_client(web_config) as (client, app):
            created = await submit(client, expected_outputs=["image.bin", "large.txt"])
            await finished(client, created["run_id"])
            endpoint = f"/api/runs/{created['run_id']}/artifacts"
            preview = await client.get(endpoint, params={"location": "workspace", "path": "image.bin", "preview": "true"})
            assert preview.status_code == 415
            assert preview.json()["error"]["code"] == "binary_preview_unavailable"
            downloaded = await client.get(endpoint, params={"location": "workspace", "path": "image.bin"})
            assert downloaded.status_code == 200
            assert downloaded.content == binary
            large = await client.get(endpoint, params={"location": "workspace", "path": "large.txt", "preview": "true"})
            assert large.status_code == 200
            detail = large.json()
            assert detail["truncated"] is True
            assert detail["size_bytes"] == len(text.encode("utf-8"))
            assert detail["content"] == "a" * 65535
            assert len(detail["content"].encode("utf-8")) <= 65536

    asyncio.run(scenario())


def test_sse_flushes_terminal_added_while_old_page_is_sending(web_config: WebConfig, monkeypatch):
    """旧事件页发送期间任务完成时，SSE仍必须补齐新增事件，再发送stream_end，返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)

    async def scenario():
        """发送首条旧事件时释放模型并等后台结束，确定制造分页快照与终态之间的竞争窗口。"""
        model = GatedModel()
        async with api_client(web_config, model) as (client, app):
            created = await submit(client)
            identifier = created["run_id"]
            await asyncio.wait_for(model.entered.wait(), 5)
            request_sent = False
            released = False
            chunks = []
            never_disconnect = asyncio.Event()

            async def receive():
                """提供空GET请求，之后保持订阅连接，直到有限终态响应自行结束。"""
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await never_disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict):
                """收集真实ASGI正文；发送首条run_event后等待模型落盘终态，再允许旧页继续发送。"""
                nonlocal released
                if message["type"] == "http.response.start":
                    assert message["status"] == 200
                if message["type"] == "http.response.body" and message.get("body"):
                    body = message["body"]
                    chunks.append(body)
                    if b"event: run_event" in body and not released:
                        released = True
                        model.release.set()
                        await app.state.manager.wait_idle()

            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": f"/api/runs/{identifier}/stream", "query_string": b"after=0",
                "root_path": "", "headers": [(b"host", b"localhost")],
                "client": ("127.0.0.1", 12345), "server": ("localhost", 80),
            }
            await asyncio.wait_for(app(scope, receive, send), 5)
            assert released is True
            history = RunStore.read(web_config.project_dir / "records", identifier)
            frames = sse_frames(b"".join(chunks).decode("utf-8"))
            streamed = [item["data"] for item in frames if item.get("event") == "run_event"]
            assert [item["seq"] for item in streamed] == [item.seq for item in history.events]
            assert streamed[-1]["type"] == "run_finished"
            assert frames[-1]["event"] == "stream_end"
            assert frames[-1]["data"]["last_seq"] == streamed[-1]["seq"]

    asyncio.run(scenario())
