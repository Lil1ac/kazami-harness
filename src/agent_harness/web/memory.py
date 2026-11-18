"""项目记忆的同源 HTTP 管理入口；网页明确决定，模型没有人工确认权限。"""

from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from agent_harness.memory.service import MemoryChange, MemoryError, MemorySave, MemoryService, MemoryStore, digest, encode
from agent_harness.memory.topics import project_topics
from agent_harness.web.memory_jobs import ConsolidationRequest, ConsolidationReview, ConsolidationSettings
from agent_harness.work.exports import materialize_text_export
from agent_harness.storage.runs import StorageError


def project_memory_service(manager, identifier: str) -> MemoryService:
    """从 manager 的宿主登记解析 identifier；未知项目拒绝，返回固定作用域服务。"""
    projects, workspaces, _ = manager.catalog.snapshot()
    source = next((item for item in [*projects, *workspaces] if item.id == identifier), None)
    if source is None:
        raise MemoryError("memory_not_found", "项目登记不存在，请重新打开项目。")
    return MemoryService(MemoryStore(manager.runs_root), source.path)


def export_project_memory(manager, identifier: str) -> Path:
    """为 identifier 的有效记忆生成只读 Markdown 派生缓存；返回网页与桌面共用路径。"""
    service = project_memory_service(manager, identifier)
    items = service.list(status="active")["items"]
    paragraphs = ["# 项目记忆", "此文件为阅读快照，编辑文件不会改变宿主记忆。"]
    for topic in project_topics(items):
        paragraphs.extend(["## 主题： " + topic["title"], "\n".join(f"- {item['title']} · {item['id']} · v{item['revision']}" for item in topic["items"])])
    for item in items:
        paragraphs.extend(["## " + item["title"].replace("\n", " "), f"编号：{item['id']} · 版本：{item['revision']} · 类型：{item['type']}", encode(item["details"]), item["content"], "适用范围：" + encode(item["applies_to"]), "证据：" + encode(item["evidence_refs"]), "来源：" + encode(item["source"])])
    service.store.root.mkdir(parents=True, exist_ok=True)
    try:
        return materialize_text_export(service.store.root, ("\n\n".join(paragraphs) + "\n").encode("utf-8"), ".memory-exports", "project-memory.md")
    except StorageError:
        raise MemoryError("memory_storage_error", "记忆导出不可用，请检查派生缓存和数据目录。") from None


def install_memory_routes(app: FastAPI, manager) -> None:
    """把记忆路由安装到 app；manager 提供宿主项目归属和历史 Run，返回 None。"""
    store = MemoryStore(manager.runs_root)  # 仅宿主访问的记忆库，页面不传磁盘路径。

    @app.get("/api/projects/{project_id}/memory-library")
    async def memory_library(project_id: str) -> dict:
        """返回 project_id 的主题、历史来源、整理列表和空闲设置。"""
        return manager.memory_jobs.library(project_id)

    @app.get("/api/projects/{project_id}/memory-evidence/{reference}")
    async def memory_evidence(project_id: str, reference: str) -> dict:
        """回读 project_id 的证据 reference；不可见或其他项目返回受控错误。"""
        return manager.memory_jobs.evidence(project_id, reference)

    @app.post("/api/projects/{project_id}/memory-consolidations", status_code=202)
    async def start_consolidation(project_id: str, body: ConsolidationRequest) -> dict:
        """按 body 持久接纳 project_id 的整理作业；返回队列编号和初始状态。"""
        return await manager.memory_jobs.enqueue(project_id, body)

    @app.get("/api/projects/{project_id}/memory-consolidations/{identifier}")
    async def get_consolidation(project_id: str, identifier: UUID) -> dict:
        """读取 project_id 的 identifier 草案和用量；无写入或模型调用。"""
        return manager.memory_jobs.detail(project_id, str(identifier))

    @app.post("/api/projects/{project_id}/memory-consolidations/{identifier}/review")
    async def review_consolidation(project_id: str, identifier: UUID, body: ConsolidationReview) -> dict:
        """按 body 明确选择发布 project_id 的草案项，返回新状态或版本冲突。"""
        return await manager.memory_jobs.review(project_id, str(identifier), body)

    @app.put("/api/projects/{project_id}/memory-consolidation-settings")
    async def configure_consolidation(project_id: str, body: ConsolidationSettings) -> dict:
        """保存 project_id 的空闲整理偏好，返回已保存的开关值。"""
        return manager.memory_jobs.configure(project_id, body.idle_enabled)

    def project_service(identifier: str) -> MemoryService:
        """从宿主目录登记解析 identifier；未知项目拒绝，返回绑定来源的领域服务。"""
        return project_memory_service(manager, identifier)

    def run_service(identifier: UUID) -> MemoryService:
        """从可信 identifier 解析来源项目与实际工作区；读取历史不启动模型。"""
        history = manager._read_history(identifier)
        origin = history.manifest.origin
        return MemoryService(store, origin.registered_path if origin else history.manifest.task.workspace, history.manifest.task.workspace)

    def save(service: MemoryService, body: MemorySave) -> dict:
        """将用户 body 保存到 service；可选来源 Run 须同一项目，返回幂等回执。"""
        provenance = {"kind": "manual_confirmation"}
        if body.source_run_id:
            manager.management.check_run(str(body.source_run_id), writing=True)
            source = run_service(body.source_run_id)
            if store.read(source.source)["scope_id"] != store.read(service.source)["scope_id"]:
                raise MemoryError("memory_source_invalid", "不能关联其他项目的运行。")
            provenance.update(run_id=str(body.source_run_id), sha256=digest(manager._read_history(body.source_run_id).manifest.task.prompt))
        from agent_harness.memory.service import MemoryContent
        content = MemoryContent(**body.model_dump(exclude={"request_id", "source_run_id", "replacements"}))
        return service.create(content, str(body.request_id), provenance=provenance, replacements=[value.model_dump() for value in body.replacements])

    @app.get("/api/projects/{project_id}/memories")
    async def project_memories(project_id: str, query: str = Query(default="", max_length=1000), status: str | None = None, type: str | None = None, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=20)) -> dict:
        """分页读取 project_id 当前记忆，query/status/type 仅作筛选；返回总数与游标。"""
        state = project_service(project_id).list(query, status, type)
        items = state["items"]
        return {**state, "items": items[offset:offset + limit], "total": len(items), "next_offset": offset + limit if offset + limit < len(items) else None}

    @app.post("/api/projects/{project_id}/memories")
    async def save_project_memory(project_id: str, body: MemorySave) -> dict:
        """明确保存 project_id 的 body；无模型调用，不改变任何 Run 终态。"""
        return save(project_service(project_id), body)

    @app.get("/api/projects/{project_id}/memories/{memory_id}/revisions")
    async def memory_revisions(project_id: str, memory_id: UUID) -> dict:
        """返回 project_id 的 memory_id 全部修订，人工可追溯纠错；不改变当前生效版本。"""
        service = project_service(project_id)
        return {"items": store.revisions(service.source, str(memory_id))}

    @app.patch("/api/projects/{project_id}/memories/{memory_id}")
    async def change_project_memory(project_id: str, memory_id: UUID, body: MemoryChange) -> dict:
        """按 body 编辑或决定 project_id 的 memory_id；预期版本过期时返回冲突。"""
        return project_service(project_id).update(str(memory_id), body)

    @app.get("/api/runs/{run_id}/memories")
    async def run_memories(run_id: UUID) -> dict:
        """返回 run_id 当前项目的候选，供会话内审阅；历史使用依据通过 ContextView 读取。"""
        state = run_service(run_id).list(status="proposed")
        view = manager.get_run(run_id)
        referenced = {item["id"] for item in (view.context.memory or {}).get("proposals", [])} if view.context else set()
        state["items"] = [item for item in state["items"] if item["source"].get("run_id") == str(run_id) or item["id"] in referenced]
        return state

    @app.post("/api/runs/{run_id}/memories")
    async def save_run_memory(run_id: UUID, body: MemorySave) -> dict:
        """将会话中明确选取的 body 保存为记忆，来源固定为 run_id；返回持久条目。"""
        if body.source_run_id is not None and body.source_run_id != run_id:
            raise MemoryError("memory_source_invalid", "会话保存只能关联当前轮。")
        return save(run_service(run_id), body.model_copy(update={"source_run_id": run_id}))

    @app.patch("/api/runs/{run_id}/memories/{memory_id}")
    async def change_run_memory(run_id: UUID, memory_id: UUID, body: MemoryChange) -> dict:
        """在 run_id 的项目中按 body 处理候选；人工决定与模型执行记录保持分离。"""
        return run_service(run_id).update(str(memory_id), body)

    @app.get("/api/projects/{project_id}/memories/export")
    async def export_project_memories(project_id: str) -> FileResponse:
        """导出 project_id 当前有效记忆为 Markdown；只读派生内容，不改变版本或批准。"""
        return FileResponse(export_project_memory(manager, project_id), media_type="text/markdown", filename="project-memory.md")
