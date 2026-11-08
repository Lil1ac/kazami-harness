"""桌面层的计划导出集成验收；独立文件名避免与网页导出测试发生模块名冲突。"""

from tests.web.job_helpers import submit_run, submit_followup

import asyncio
from pathlib import Path

import httpx

from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import PreferenceStore
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from tests.web.test_planning_api import PLAN, ScriptModel, call, finished, planning_config, reply


class NativeDownload:
    """记录下载请求并返回宿主目标路径，底层文件来自真实导出缓存。"""

    def __init__(self) -> None:
        """初始化空请求记录，不启动窗口或操作系统下载。"""
        self.calls: list[tuple[str, dict]] = []  # 最终交给 Electron 的方法及已核对文件路径。

    async def request(self, method: str, parameters: dict) -> dict:
        """保存 method/parameters；返回目标下载路径，非 file.download 请求使测试失败。"""
        self.calls.append((method, parameters))
        assert method == "file.download"
        return {"path": str(Path(parameters["directory"]) / Path(parameters["source"]).name)}


def test_desktop_download_uses_same_plan_artifact_and_configured_directory(planning_config) -> None:
    """桌面标准附件入口可导出待批准计划，源文件受控且目标使用用户配置，不触发执行。"""
    config = planning_config
    native = NativeDownload()
    preferences = PreferenceStore(config.project_dir / "desktop")
    destination = config.project_dir / "downloads"
    preferences.update(downloads_directory=destination)
    host = DesktopHost(preferences, native, "offline-token", {"deepseek": "offline-key"})
    model = ScriptModel([reply(call("submit_plan", **PLAN))])
    app = create_app(config, RunConfig(tools=["read_file"]), config.project_dir / "records", model_client=model, desktop=host)

    async def scenario() -> None:
        """在真实认证 HTTP 服务中提交和下载；校验原生参数与状态，不模拟计划或附件服务。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers={"Authorization": "Bearer offline-token"}) as client:
                view = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "先规划"}))
                url = f"/api/runs/{view['run_id']}/artifacts?location=run&path=plans%2Fv1.md"
                downloaded = await client.post("/api/desktop/download", json={"url": url})
                assert downloaded.status_code == 200, downloaded.text
                assert len(native.calls) == 1 and native.calls[0][1]["directory"] == str(destination)
                source = Path(native.calls[0][1]["source"])
                assert source.is_relative_to(config.project_dir / "records" / view["run_id"])
                assert source.read_text(encoding="utf-8").startswith("# 交付结果文件")
                assert len(model.requests) == 1
    asyncio.run(scenario())
