"""同源本地 HTTP/SSE 入口：网页复用现有运行核心，只暴露受限任务与产物接口。"""

import asyncio
import codecs
from contextlib import asynccontextmanager
import json
import re
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import UUID
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
import httpx
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_harness.providers.base import ModelClient
from agent_harness.schemas import RunConfig
from agent_harness.storage.sessions import SessionError
from agent_harness.storage.runs import StorageError
from agent_harness.web.config import WebConfig
from agent_harness.web.schemas import ConversationPage, CreateProjectRequest, ProjectNameRequest, ProjectSettingsRequest, ConversationView, CreateRunRequest, FollowupRequest, RunView, ReviewDecision
from agent_harness.web.service import WebRunManager, WebServiceError
from agent_harness.web.project_catalog import CatalogError
from agent_harness.web.jobs import JobRequest
from agent_harness.web.conversation_management import ConversationError, ConversationChange, ConversationBatch, ConversationPurge
from agent_harness.work.tasks import TaskError, TaskService, TaskCreate, TaskUpdate
from agent_harness.work.planning import PlanningError
from agent_harness.memory.service import MemoryError
from agent_harness.web.memory import install_memory_routes
from agent_harness.config import ConfigurationError
from agent_harness.permissions.store import PermissionStore

if TYPE_CHECKING:
    from agent_harness.desktop.host import DesktopHost


def _error(status: int, code: str, message: str) -> JSONResponse:
    """把受控 status/code/message 转成统一 JSON 错误响应；不序列化异常正文或输入值。"""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(
    web_config: WebConfig,
    run_config: RunConfig | None = None,
    runs_root: Path | None = None,
    *,
    model_client: ModelClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    desktop: "DesktopHost | None" = None,
) -> FastAPI:
    """创建本地应用并返回 FastAPI；run_config/runs_root=None 使用安全默认配置与项目 .runs。

    web_config 固定可选工作区和前端目录；client/transport 是离线测试注入点，None 用真实模型。
    desktop 为原生宿主，None 表示独立 Web 服务；桌面模式启用会话认证、原生选择器和凭据读取。
    lifespan 关闭时会等待活动任务清理；构造不调用模型，不读取密钥值到响应或日志。
    """
    manager = WebRunManager(
        web_config,
        run_config or RunConfig(),
        runs_root or web_config.project_dir / ".runs",
        model_client=model_client,
        transport=transport,
        key_provider=desktop.api_key if desktop is not None else None,
    )
    if desktop is not None:
        manager.folder_picker = desktop
        desktop.manager = manager

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """在 app 生命周期内维护后台任务，退出时等待取消清理；yield None 不产生额外资源。"""
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(title="Agent Harness 本地控制台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager  # 公开服务实例供生命周期与离线测试协调。
    install_memory_routes(app, manager)

    @app.exception_handler(ConversationError)
    async def conversation_error(request: Request, error: ConversationError) -> JSONResponse:
        """将request会话管理error返回为受控状态；只附恢复所需会话编号，不泄露消息。"""
        # 410可被浏览器启发式缓存；生命周期可恢复，前置拦截也必须禁止缓存。
        return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {"code": error.code, "message": error.message, "details": {"session_id": error.session_id} if error.session_id else None}})

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, error: StorageError) -> JSONResponse:
        """将request持久存储故障转为503；保留原文件，不把系统异常正文传给页面。"""
        return _error(503, "storage_unavailable", "记录暂时无法读取或保存，请检查本地数据目录。")

    @app.exception_handler(MemoryError)
    async def memory_error(request: Request, error: MemoryError) -> JSONResponse:
        """将 request 的记忆 error 映射为安全响应；存储503，不存在404，版本与归属冲突409。"""
        status = 503 if error.code == "memory_storage_error" else 404 if error.code == "memory_not_found" else 409
        return JSONResponse(status_code=status, content={"error": {"code": error.code, "message": error.message, "details": error.details}})

    @app.exception_handler(TaskError)
    @app.exception_handler(PlanningError)
    async def task_error(request: Request, error: TaskError) -> JSONResponse:
        """将 request 对应任务业务 error 转为安全响应；不存在返回404，其余冲突409。"""
        status = 404 if error.code in {"task_not_found", "job_not_found", "integration_not_found"} else 400 if error.code in {"unknown_workspace", "unknown_project", "unknown_verifier", "workspace_copy_not_supported", "job_invalid"} else 409
        return _error(status, error.code, error.message)

    @app.exception_handler(ConfigurationError)
    async def job_configuration_error(request: Request, error: ConfigurationError) -> JSONResponse:
        """将 request 接纳前的配置 error 映射为400；配置文案不携带密钥。"""
        return _error(400, "invalid_run", str(error))

    @app.post("/api/jobs", status_code=202)
    async def enqueue(body: JobRequest) -> dict:
        """持久接纳 body 运行请求并返回 JobView；页面断开不取消后台准备。"""
        pending = asyncio.create_task(manager.jobs.enqueue(body))
        return await asyncio.shield(pending)

    @app.get("/api/jobs")
    async def jobs() -> dict:
        """返回后台作业及队列暂停状态；只读，不恢复执行。"""
        return {"items": [manager.jobs.public(job) for job in manager.jobs.list()], "paused": manager.jobs.paused}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: UUID) -> dict:
        """按 job_id 返回作业归属和真实状态；未建立 Run 时仍可展示。"""
        return manager.jobs.public(manager.jobs.get(str(job_id)))

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: UUID) -> dict:
        """取消 job_id 并等待真实清理；排队项不生成 Run 或文件副作用。"""
        return await manager.jobs.cancel(str(job_id))

    @app.post("/api/jobs/{job_id}/resolve")
    async def resolve_job(job_id: UUID) -> dict:
        """记录用户确认 job_id 进程已停止且工作区已核对；不重放原请求。"""
        return manager.jobs.resolve(str(job_id))

    @app.post("/api/queue/resume")
    async def resume_queue() -> dict:
        """明确恢复尚未执行队列；中断作业未核对时拒绝，返回暂停状态。"""
        manager.jobs.resume()
        return {"paused": manager.jobs.paused}

    def task_service(run_id: UUID) -> TaskService:
        """核对 run_id 并绑定其可信会话；返回人工管理入口，不启动执行。"""
        view = manager.get_run(run_id)
        return TaskService(manager.jobs.store, str(view.session_id or run_id))

    @app.get("/api/runs/{run_id}/permissions")
    async def permissions(run_id: UUID, historical: bool = False, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)) -> dict:
        """返回 run_id 当轮权限与历史；historical固定当轮快照，其余分页会话动作，不启动或批准。"""
        view = manager.get_run(run_id)
        state = view.result.permissions.model_dump(mode="json")
        history = {"items": [state["pending"]] if state["pending"] else [], "total": int(state["pending"] is not None)} if historical else PermissionStore(manager.runs_root).list(str(view.session_id or run_id), limit=limit, offset=offset)
        return {"state": state, "policy": view.config.permission_policy.model_dump(), **history}

    @app.get("/api/runs/{run_id}/tasks")
    async def tasks(run_id: UUID, historical: bool = False) -> dict:
        """返回 run_id 会话最新清单；historical=True 只返回当轮固定快照。"""
        view = manager.get_run(run_id)
        return (view.result.tasks or {"revision": 0, "items": []}) if historical else task_service(run_id).store.snapshot(str(view.session_id or run_id))

    @app.post("/api/runs/{run_id}/tasks")
    async def create_task(run_id: UUID, body: TaskCreate) -> dict:
        """人工创建 body 工作项；当前会话执行中时拒绝结构性变更，返回创建回执。"""
        service = task_service(run_id)
        active = manager._active
        if active and str(active.manifest.session_id or active.manifest.run_id) == service.session:
            raise TaskError("session_busy", "请先停止当前运行，再修改任务范围。")
        return service.create(body)

    @app.patch("/api/runs/{run_id}/tasks")
    async def update_task(run_id: UUID, body: TaskUpdate) -> dict:
        """人工更新 body 工作项；取消进行中项先停止其真实 Run，其他变更要求会话空闲。"""
        service = task_service(run_id)
        active = manager._active
        if active and str(active.manifest.session_id or active.manifest.run_id) == service.session:
            if body.status != "cancelled":
                raise TaskError("session_busy", "请先停止当前运行，再修改任务范围。")
            item = service.get(body.task_id)
            if item["revision"] != body.expected_revision:
                raise TaskError("task_revision_conflict", "任务已更新，请重新读取后操作。")
            owned = item["active_run_id"]
            items = {value["id"]: value for value in service.store.snapshot(service.session)["items"]}
            for child in items.values():
                parent = child["parent_task_id"]
                while parent:
                    if parent == item["id"] and child["active_run_id"]:
                        owned = child["active_run_id"]
                    parent = items[parent]["parent_task_id"]
            if owned:
                await manager.cancel(UUID(owned))
                await manager.wait_idle()
                body = body.model_copy(update={"expected_revision": service.get(body.task_id)["revision"]})
        return service.update(body)

    @app.get("/api/work-events")
    async def work_events(after: int = Query(default=0, ge=0), scope: str | None = None) -> dict:
        """从 after 游标补读任务和作业事件；scope=None 读取全部，最多一百项。"""
        return {"items": manager.jobs.store.events(after, scope)}

    @app.get("/api/work-stream")
    async def work_stream(request: Request, after: int | None = Query(default=None, ge=0), scope: str | None = None):
        """从 after 订阅 scope 的事务事件；断线游标可补读，只观察、不创建或恢复作业。"""
        async def stream():
            """逐批返回已提交事件；断开或关停结束，空闲发送心跳，返回 SSE 字节流。"""
            previous = request.headers.get("last-event-id", "")
            cursor = after if after is not None else int(previous) if previous.isdigit() and len(previous) < 20 else manager.jobs.store.latest_seq()
            while not manager._closing and not await request.is_disconnected():
                events = manager.jobs.store.events(cursor, scope)
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.middleware("http")
    async def same_origin(request: Request, call_next) -> Response:
        """拒绝跨源修改请求和不可信 Host；call_next 继续路由，返回添加基础安全头的响应。

        无 Origin 的本地 CLI/测试请求允许；浏览器仅允许同源与固定 Vite 开发源。
        testserver 仅是 ASGI 测试惯例名称，真实服务仍由 CLI 固定绑定 127.0.0.1。
        """
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return _error(400, "invalid_host", "本地服务只接受本机地址。")
        if desktop is not None and not desktop.authorized(request):
            return _error(403, "desktop_session_required", "请从 Agent Harness 桌面窗口操作。")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed_origins = {
                f"{request.url.scheme}://{request.url.netloc}",
            }
            if desktop is None:
                allowed_origins.update({"http://127.0.0.1:5173", "http://localhost:5173"})
            if (
                request.headers.get("sec-fetch-site") == "cross-site"
                or (origin is not None and origin not in allowed_origins)
            ):
                return _error(403, "cross_origin_forbidden", "任务操作必须来自本地控制台同源页面。")
        run_match = re.fullmatch(r"/api/runs/([0-9a-fA-F-]{36})(?:/.*)?", request.url.path)
        if run_match:
            try:
                identifier = str(UUID(run_match[1]))
                manager.management.check_run(identifier, writing=request.method not in {"GET", "HEAD", "OPTIONS"})
            except ConversationError as error:
                return await conversation_error(request, error)
            except ValueError:
                return _error(422, "invalid_request", "运行编号必须是有效UUID。")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if desktop is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.exception_handler(WebServiceError)
    async def service_error(request: Request, error: WebServiceError) -> JSONResponse:
        """将 error 的受控业务字段返回给 request 调用方；不输出堆栈或原始输入。"""
        return _error(error.status, error.code, error.message)


    @app.exception_handler(CatalogError)
    async def catalog_error(request: Request, error: CatalogError) -> JSONResponse:
        """将 request 的目录登记失败映射为受控响应；不回显文件或异常正文。"""
        return _error(error.status, error.code, error.message)

    @app.exception_handler(SessionError)
    async def session_error(request: Request, error: SessionError) -> JSONResponse:
        """将会话 error 映射为受控响应；读取/写入故障503，其余继续条件冲突409，不回显输入。"""
        status = 503 if error.code in {"session_unreadable", "session_write_failed"} else 409
        return _error(status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        """将 request 参数校验失败统一转成固定 422；error 中的输入内容不回显。"""
        return _error(422, "invalid_request", "请求参数无效，请检查必填字段、类型与取值范围。")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        """统一框架 HTTP 错误并返回 JSON；只映射状态，不回显 error.detail。"""
        if error.status_code == 404:
            return _error(404, "not_found", "请求的接口或文件不存在。")
        return _error(error.status_code, "http_error", "请求无法处理，请检查访问方式。")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        """返回 request 未处理异常的固定 500；不将 error 内容、输入或凭据写入响应。"""
        return _error(500, "internal_error", "本地服务发生异常，请检查运行记录与目录权限。")

    @app.get("/api/options")
    async def options() -> dict:
        """返回宿主登记选项、默认配置、密钥存在标记和当前槽位；不接收参数。"""
        result = await asyncio.to_thread(manager.options)
        if desktop is not None:
            result["desktop"] = desktop.public_settings()
        return result

    @app.post("/api/projects/pick")
    async def pick_project() -> dict:
        """打开原生选择框，返回取消状态或待确认的目录草稿；不登记、不创建任务。"""
        selected = await manager.folder_picker.choose()
        if selected is None:
            return {"cancelled": True, "selection": None}
        selection = await asyncio.to_thread(manager.catalog.select_directory, selected)
        return {"cancelled": False, "selection": selection}

    @app.post("/api/projects", status_code=201)
    async def create_project(body: CreateProjectRequest) -> dict:
        """按 body 名称与原生选择凭据保存项目绑定，返回登记；重复目录选用已有项目。"""
        identifier = await asyncio.to_thread(manager.catalog.create, body.selection_id, body.label)
        options = await asyncio.to_thread(manager.options)
        return next(item for item in options["recent_projects"] if item["id"] == identifier)

    @app.patch("/api/projects/{project_id}")
    async def rename_project(project_id: str, body: ProjectNameRequest) -> dict:
        """保存 body 项目名称并返回项目；不改绑定路径、执行设置或历史运行。"""
        await asyncio.to_thread(manager.catalog.rename, project_id, body.label)
        options = await asyncio.to_thread(manager.options)
        return next(item for item in options["recent_projects"] if item["id"] == project_id)

    @app.post("/api/projects/{project_id}/open")
    async def open_project(project_id: str) -> dict:
        """重新打开已登记 project_id 并更新最近记录；不接受宿主新路径，不创建任务。"""
        identifier = await asyncio.to_thread(manager.catalog.reopen, project_id)
        options = await asyncio.to_thread(manager.options)
        return next(item for item in options["recent_projects"] if item["id"] == identifier)

    @app.put("/api/projects/{project_id}/settings")
    async def project_settings(project_id: str, body: ProjectSettingsRequest) -> dict:
        """保存用户明确填写的项目测试设置；只检查程序入口，执行留到新任务验收阶段。"""
        await asyncio.to_thread(manager.catalog.configure_execution, project_id, body.test_command, body.test_timeout_seconds, body.python_executable)
        options = await asyncio.to_thread(manager.options)
        return next(item for item in options["recent_projects"] if item["id"] == project_id)

    @app.get("/api/conversations", response_model=ConversationPage)
    async def conversations(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
        project_id: str | None = Query(default=None, min_length=1, max_length=64),
        status: Literal["created", "running", "completed", "failed", "cancelled", "timed_out", "limit_exceeded", "waiting_input"] | None = None,
        state: Literal["active", "archived", "trashed"] = "active",
        q: str = Query(default="", max_length=200),
        ungrouped: bool = False,
    ) -> ConversationPage:
        """按最新状态和当前项目身份聚合分页；只读磁盘事实，不打开目录或启动模型。"""
        # 与现有运行列表一样串行读取，共享的历史LRU不跨线程修改。
        return manager.conversation_index.list(offset, limit, project_id, status, state=state, query=q, ungrouped=ungrouped)

    @app.post("/api/conversations/batch")
    async def manage_conversations(body: ConversationBatch) -> dict:
        """逐项管理body明确选择的会话；失败项保留原因，读取或整理不发起运行。"""
        return await manager.management.batch(body)

    @app.get("/api/conversations/{session_id}")
    async def conversation_metadata(session_id: UUID) -> dict:
        """返回session_id管理视图；回收站和清理墓碑仍可读取恢复状态，不返回消息。"""
        return manager.management.view(str(session_id)).model_dump(mode="json")

    @app.patch("/api/conversations/{session_id}")
    async def manage_conversation(session_id: UUID, body: ConversationChange) -> dict:
        """按body幂等更新session_id标题、归属或生命周期；执行来源固定不变。"""
        return (await manager.management.change(str(session_id), body)).model_dump(mode="json")

    @app.delete("/api/conversations/{session_id}")
    async def purge_conversation(session_id: UUID, body: ConversationPurge) -> dict:
        """明确确认后彻底清理session_id专属记录；项目源码及另存记忆保持，失败允许重试。"""
        pending = asyncio.create_task(manager.management.purge(str(session_id), body))
        return (await asyncio.shield(pending)).model_dump(mode="json")

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def run_detail(run_id: UUID) -> RunView:
        """返回 run_id 的实时或历史运行视图；不存在返回 404，不恢复执行动作。"""
        return manager.get_run(run_id)

    @app.get("/api/runs/{run_id}/conversation", response_model=ConversationView)
    async def conversation(run_id: UUID) -> ConversationView:
        """读取 run_id 所属会话和可继续原因；不启动模型或重新拍摄快照。"""
        return manager.conversations.view(run_id)

    from agent_harness.web.teams import install_team_routes
    install_team_routes(app, manager)

    @app.get("/api/runs/{run_id}/subagents")
    async def subagents(run_id: UUID) -> dict:
        """只读run_id本轮委派列表；返回持久进度，不重投作业或调用模型。"""
        return manager.list_subagents(run_id)

    @app.get("/api/runs/{run_id}/subagents/{child_id}")
    async def subagent(run_id: UUID, child_id: UUID, after: int = Query(default=0, ge=0),
                       limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict:
        """返回直属child_id有界事件和报告页；after/offset分别为事件序号及UTF-8字节偏移。"""
        return manager.get_subagent(run_id, child_id, after, limit, offset)


    @app.get("/api/runs/{run_id}/subagents/{child_id}/diff")
    async def subagent_diff(run_id: UUID, child_id: UUID) -> dict:
        """返回run_id登记child_id的固定补丁预览，最多256KiB；读取不捕获现场或执行模型。"""
        detail = manager.get_subagent(run_id, child_id, 0, 1, 0)
        if detail["subagent"]["role"] != "develop" or detail["subagent"]["snapshot_status"] != "ready":
            raise HTTPException(409, detail="开发子成果尚无完整代码快照。")
        path = await asyncio.to_thread(manager.projects.patch_path, child_id)
        raw = await asyncio.to_thread(path.read_bytes)
        return {"content": raw[:256 * 1024].decode("utf-8", errors="replace"), "truncated": len(raw) > 256 * 1024}

    @app.post("/api/runs/{run_id}/subagents/{child_id}/review")
    async def subagent_review(run_id: UUID, child_id: UUID, request: ReviewDecision) -> dict:
        """明确保留或丢弃直属child_id工作树；run_id必须停止且无审批/集成待办，历史证据保持。"""
        detail = manager.get_subagent(run_id, child_id, 0, 1, 0)
        view = manager.get_run(run_id)
        manager.management.require_active(str(view.session_id))
        if detail["subagent"]["role"] != "develop" or view.active or manager.management.busy_reason(str(view.session_id)):
            raise HTTPException(409, detail="请先完成父运行及相关后台作业。")
        from agent_harness.permissions.store import PermissionStore
        if PermissionStore(manager.runs_root).waiting(manager._read_history(run_id)) is not None:
            raise HTTPException(409, detail="请先处理父会话中的子任务审批。")
        return (await asyncio.to_thread(manager.projects.decide, child_id, request.decision)).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/subagents/{child_id}/cancel")
    async def cancel_subagent(run_id: UUID, child_id: UUID) -> dict:
        """停止run_id直属child_id，返回封存快照；同源与会话生命周期校验沿用公共中间件。"""
        return await manager.cancel_subagent(run_id, child_id)

    @app.post("/api/runs/{run_id}/cancel", response_model=RunView)
    async def cancel(run_id: UUID) -> RunView:
        """幂等取消 run_id 并返回当前视图；收尾期间 cancelling=True，不提前声称已取消。"""
        return await manager.cancel(run_id)

    @app.get("/api/runs/{run_id}/review")
    async def project_review(run_id: UUID) -> dict:
        """返回已停止 run_id 的固化代码审阅；重启遗留快照可补捕获一次，活动时返回409。"""
        return (await manager.get_review(run_id)).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/review")
    async def project_decision(run_id: UUID, request: ReviewDecision) -> dict:
        """按 request 保留或丢弃 run_id 工作树；只处理本工具归属目录，保留审阅与测试证据。"""
        return (await manager.decide_review(run_id, request.decision)).model_dump(mode="json")

    from agent_harness.web.integrations import IntegrationCreate, IntegrationAction, IntegrationResolutions

    @app.get("/api/runs/{run_id}/integration-targets")
    async def integration_targets(run_id: UUID, parent_run_id: UUID | None = None) -> dict:
        """返回run_id同仓库内的已登记目标；只读目录与分支，不隐式切换或创建工作树。"""
        return {"items": await asyncio.to_thread(manager.integrations.targets, str(run_id), str(parent_run_id) if parent_run_id else None)}

    @app.get("/api/runs/{run_id}/integrations")
    async def integrations(run_id: UUID, parent_run_id: UUID | None = None) -> dict:
        """列出run_id所属会话的固定采纳历史；历史查询不执行原动作。"""
        view = manager.get_run(parent_run_id or run_id)
        if parent_run_id is not None and not any(item.run_id == run_id for item in view.result.subagents):
            manager.integrations.team_review(str(run_id), view)
        service = manager.integrations
        values = service.store.list(str(view.session_id or run_id))
        if parent_run_id is not None:
            values = [value for value in values if value["run_id"] == str(run_id) or str(run_id) in value.get("source_run_ids", [])]
        return {"items": [service.store.public(value) for value in values]}

    @app.post("/api/runs/{run_id}/integrations", status_code=202)
    async def create_integration(run_id: UUID, body: IntegrationCreate) -> dict:
        """按body明确选择的目标持久接纳集成准备；网页断开不取消后台作业。"""
        return await asyncio.shield(asyncio.create_task(manager.integrations.create(str(run_id), body)))

    @app.get("/api/integrations/{integration_id}")
    async def integration(integration_id: UUID) -> dict:
        """返回integration_id的固定事实；不从当前目标推测成功，也不自动重放。"""
        value = manager.integrations.store.get(str(integration_id))
        manager.get_run(value.get("parent_run_id") or value["run_id"])
        return manager.integrations.store.public(value)

    @app.get("/api/integrations/{integration_id}/file")
    async def integration_file(integration_id: UUID, path: str) -> dict:
        """按相对path读取固定三方内容和最终差异；仅返回有界文本，不读取现场同名文件。"""
        return await asyncio.to_thread(manager.integrations.store.preview, str(integration_id), path)

    @app.get("/api/integrations/{integration_id}/checks/{check_id}/artifacts")
    async def integration_check_artifact(integration_id: UUID, check_id: UUID, path: str) -> FileResponse:
        """下载check_id登记的固定path原始输出；遵守来源会话生命周期，不开放任意文件路径。"""
        value = manager.integrations.store.get(str(integration_id))
        manager.get_run(value.get("parent_run_id") or value["run_id"])
        target = manager.integrations.store.check_artifact(str(integration_id), str(check_id), path)
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.post("/api/integrations/{integration_id}/resolutions")
    async def resolve_integration(integration_id: UUID, body: IntegrationResolutions) -> dict:
        """按body版本解决候选冲突；真实目标保持不变，旧检查和旧确认不适用于新版本。"""
        return await manager.integrations.resolve(str(integration_id), body)

    @app.post("/api/integrations/{integration_id}/actions", status_code=202)
    async def integration_action(integration_id: UUID, body: IntegrationAction) -> dict:
        """接纳body绑定的人工检查、采纳、核对或撤销预览；不会调用模型。"""
        return await asyncio.shield(asyncio.create_task(manager.integrations.action(str(integration_id), body)))

    @app.get("/api/runs/{run_id}/diff")
    async def project_diff(run_id: UUID) -> dict:
        """返回 run_id 固化补丁的最多256KiB UTF-8预览；完整内容通过 patch 附件下载。"""
        target = await manager.project_patch(run_id)
        try:
            size = target.stat().st_size
            with target.open("rb") as source:
                raw = source.read(262145)
            truncated = len(raw) > 262144
            content = codecs.getincrementaldecoder("utf-8")("replace").decode(raw[:262144], final=not truncated)
            content = content.encode("utf-8")[:262144].decode("utf-8", errors="ignore")
            return {"content": content, "truncated": truncated, "size_bytes": size}
        except OSError:
            raise WebServiceError(503, "project_patch_unreadable", "代码补丁暂时无法读取。") from None

    @app.get("/api/runs/{run_id}/patch")
    async def project_patch(run_id: UUID, parent_run_id: UUID | None = None) -> FileResponse:
        """下载 run_id 完整固化补丁；强制附件，不将代码作为浏览器可执行内容。"""
        target = await manager.project_patch(run_id, parent_run_id)
        return FileResponse(
            target, media_type="application/octet-stream", filename=f"{run_id}.diff" if parent_run_id is None and manager.get_run(run_id).execution_mode == "local" else f"{run_id}.patch",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: UUID, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)) -> dict:
        """返回 run_id 中序号大于 after 的事件页；limit 范围 1..500，不消费或删除事件。"""
        return manager.list_events(run_id, after, limit).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: UUID, request: Request, after: int = Query(default=0, ge=0)) -> StreamingResponse:
        """补发 run_id 在 after/Last-Event-ID 之后的事件并持续 SSE；断开不取消任务。

        两个游标同时提供时取较大值，必须是非负整数；终态或中断历史发 stream_end。
        """
        manager.get_run(run_id)
        cursor = after
        header = request.headers.get("last-event-id")
        if header is not None:
            try:
                previous = int(header)
                if previous < 0:
                    raise ValueError
                cursor = max(cursor, previous)
            except ValueError:
                raise WebServiceError(422, "invalid_cursor", "事件游标必须是非负整数。") from None

        async def generate() -> AsyncIterator[str]:
            """从已验证 cursor 持续发送落盘事件；每十秒心跳，断开只结束当前订阅。"""
            current = cursor
            heartbeat = asyncio.get_running_loop().time()
            while True:
                if await request.is_disconnected():
                    return
                try:
                    manager.management.check_run(str(run_id))
                except ConversationError:
                    return
                page = manager.list_events(run_id, current, 100)
                for event in page.items:
                    current = event.seq
                    yield f"id: {event.seq}\nevent: run_event\ndata: {event.model_dump_json()}\n\n"
                if page.has_more:
                    continue
                view = manager.get_run(run_id)
                # yield 发送期间 Runner 仍可追加事件；先补齐新的终态前缀再结束订阅。
                if view.last_seq > current:
                    continue
                if not view.active:
                    ending = {"last_seq": view.last_seq, "interrupted": view.interrupted}
                    yield "event: stream_end\ndata: " + json.dumps(ending, separators=(",", ":")) + "\n\n"
                    return
                now = asyncio.get_running_loop().time()
                if now - heartbeat >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat = now
                await asyncio.sleep(0.2)
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs/{run_id}/artifacts")
    async def artifact(
        run_id: UUID,
        location: Literal["run", "workspace"],
        path: str = Query(min_length=1, max_length=4096),
        preview: bool = False,
        parent_run_id: UUID | None = None,
    ) -> Response:
        """下载 run_id 已登记产物，或 preview=True 返回最多 64KiB UTF-8 纯文本预览。

        location 区分记录与工作区；二进制不可预览但可附件下载，未登记或越界返回 404。
        """
        target = manager.artifact_path(run_id, location, path, parent_run_id)
        if not preview:
            return FileResponse(
                target,
                media_type="application/octet-stream",
                filename=target.name,
                headers={"X-Content-Type-Options": "nosniff"},
            )
        try:
            size = target.stat().st_size
            with target.open("rb") as source:
                raw = source.read(65537)
            truncated = len(raw) > 65536
            content = codecs.getincrementaldecoder("utf-8")("strict").decode(raw[:65536], final=not truncated)
            if "\x00" in content:
                raise UnicodeError
            return JSONResponse({"content": content, "truncated": truncated, "size_bytes": size, "encoding": "utf-8"})
        except UnicodeError:
            return _error(415, "binary_preview_unavailable", "此产物不是 UTF-8 文本，请下载查看。")
        except OSError:
            return _error(404, "artifact_not_found", "产物未登记或不可访问。")

    def frontend_file(relative: str) -> Path:
        """解析构建目录内 relative 文件并返回绝对路径；不存在或越界抛固定 404。"""
        try:
            base = web_config.frontend_dist.resolve()
            target = (base / relative).resolve(strict=True)
            if not target.is_relative_to(base) or not target.is_file():
                raise ValueError
            return target
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404) from None

    @app.get("/assets/{asset_path:path}")
    async def asset_file(asset_path: str) -> FileResponse:
        """仅返回 assets 下的静态 asset_path；缺失脚本直接 404，不返回 SPA HTML。"""
        return FileResponse(frontend_file("assets/" + asset_path))

    @app.get("/")
    @app.get("/runs")
    @app.get("/runs/{run_id}")
    @app.get("/jobs")
    @app.get("/jobs/{job_id}")
    @app.get("/settings")
    @app.get("/projects/{project_id}")
    async def frontend_page() -> Response:
        """返回前端路由统一 index；尚未构建时给出本地构建步骤，不影响 API 使用。"""
        try:
            index = frontend_file("index.html")
        except HTTPException:
            return HTMLResponse(
                "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                "<title>Agent Harness</title><body><h1>前端尚未构建</h1>"
                "<p>请在项目 frontend 目录运行 npm install 和 npm run build，然后刷新此页。</p>"
                "<p>后端 API 已就绪；开发模式也可以启动 npm run dev。</p></body></html>",
                status_code=503,
            )
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    if desktop is not None:
        from agent_harness.desktop.api import install_routes
        install_routes(app, desktop, manager)
    return app
