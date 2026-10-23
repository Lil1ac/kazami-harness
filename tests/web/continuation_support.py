"""普通中断接续的共享Git项目和等待辅助；不依赖已移除的恢复协议。"""

import asyncio
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from agent_harness.projects.models import ProjectPreset
from agent_harness.web.config import WebConfig, WebWorkspace


def git(root: Path, *arguments: str) -> bytes:
    """在临时root执行固定Git参数并返回stdout；不用shell，失败终止测试。"""
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=10).stdout


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> WebConfig:
    """创建固定Git基线和真实宿主测试，并保留源仓库dirty文件供隔离断言。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-continuation-key")
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "check.py").write_text('from app import add\nassert add(2, 3) == 5\nprint("actual continuation test passed")\n', encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "continuation baseline")
    (root / "local.txt").write_text("保留源目录未提交内容", encoding="utf-8")
    workspace = tmp_path / "ordinary"
    workspace.mkdir()
    return WebConfig(project_dir=tmp_path, runtime_root=tmp_path / "runtime/copies", frontend_dist=tmp_path / "dist", workspaces=[WebWorkspace(id="ordinary", label="普通", path=workspace)], projects=[ProjectPreset(id="code", label="普通接续测试", path=root, test_argv=[sys.executable, "check.py"], test_timeout_seconds=10)])


async def idle(client: httpx.AsyncClient, run_id: str) -> dict:
    """最多20秒等待run_id结束及代码收尾，返回最终API视图；超时使测试失败。"""
    async with asyncio.timeout(20):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            view = response.json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)
