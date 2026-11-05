"""Hooks 管理接口；只解析已登记项目，不接受浏览器提交宿主任意目录或命令。"""

import asyncio

from fastapi import Query

from agent_harness.hooks.catalog import HookStore, discover, review
from agent_harness.hooks.models import HookReview
from agent_harness.work.tasks import TaskError


def install_hook_routes(app, manager) -> None:
    """为 app 注册登记项目的读取、审核及启停；manager 提供权限宿主配置，返回 None。"""
    store = HookStore(manager.runs_root)

    def workspace(project_id: str):
        """从登记快照解析 project_id，返回绝对目录；未知编号抛 TaskError，不创建项目。"""
        projects, workspaces, _ = manager.catalog.snapshot()
        item = next((entry for entry in [*projects, *workspaces] if entry.id == project_id), None)
        if item is None:
            raise TaskError("hook_project_missing", "请选择已登记的项目。")
        return item.path

    @app.get("/api/hooks")
    async def list_hooks(project_id: str = Query(min_length=1, max_length=64)) -> dict:
        """读取登记 project_id 的当前规则与审核状态；返回快照，不运行脚本或更新运行配置。"""
        root = await asyncio.to_thread(workspace, project_id)
        items = await asyncio.to_thread(discover, root, manager.run_config.hooks, store)
        return {"items": items, "config_path": str(root / ".agents" / "hooks.toml")}

    @app.post("/api/hooks/review")
    async def review_hook(body: HookReview, project_id: str = Query(min_length=1, max_length=64)) -> dict:
        """对 project_id 的固定 body 定义审核/启停；返回新快照，变化时拒绝且不执行命令。"""
        root = await asyncio.to_thread(workspace, project_id)
        items = await asyncio.to_thread(review, root, manager.run_config.hooks, store, body)
        return {"items": items, "config_path": str(root / ".agents" / "hooks.toml")}
