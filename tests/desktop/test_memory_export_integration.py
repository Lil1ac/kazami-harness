"""项目记忆导出复用桌面认证、原生下载和已配置下载目录。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx

from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import PreferenceStore
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.desktop.test_plan_export_integration import NativeDownload
from tests.web.test_planning_api import ScriptModel


def test_memory_export_native_destination_and_invalid_paths(tmp_path: Path):
    """真实记忆正文由标准桌面入口下载；拒绝跨源、查询篡改及目录穿越，不启动模型。"""
    project = tmp_path / "project"
    project.mkdir()
    web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist", workspaces=[WebWorkspace(id="files", label="记忆项目", path=project)])
    preferences = PreferenceStore(tmp_path / "desktop")
    preferences.update(downloads_directory=tmp_path / "downloads")
    native = NativeDownload()
    host = DesktopHost(preferences, native, "memory-token", "offline-key")
    model = ScriptModel([])
    app = create_app(web, RunConfig(), tmp_path / "records", model_client=model, desktop=host)

    async def scenario():
        """在真实认证应用保存并下载记忆，只模拟 Electron 文件落地，返回 None。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers={"Authorization": "Bearer memory-token"}) as client:
            saved = await client.post("/api/projects/files/memories", json={"request_id": str(uuid4()), "title": "发布约定", "content": "外部客户升级前保留旧字段", "type": "fact", "details": {"subject": "发布约定", "attribute": "说明", "value": "外部客户升级前保留旧字段"}})
            assert saved.status_code == 200
            url = "/api/projects/files/memories/export"
            downloaded = await client.post("/api/desktop/download", json={"url": url})
            assert downloaded.status_code == 200, downloaded.text
            assert native.calls[0][1]["directory"] == str(tmp_path / "downloads")
            source = Path(native.calls[0][1]["source"])
            assert source.is_relative_to(tmp_path / "records/.memory-exports")
            assert "外部客户升级" in source.read_text(encoding="utf-8")
            for invalid in ("https://untrusted.example" + url, url + "?path=../secret", "/api/projects/../memories/export"):
                assert (await client.post("/api/desktop/download", json={"url": invalid})).status_code == 400
            source.write_text("tampered", encoding="utf-8")
            assert (await client.get(url)).status_code == 503
            assert not model.requests
    asyncio.run(scenario())
