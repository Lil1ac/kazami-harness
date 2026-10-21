"""同源本地 HTTP/SSE 入口：网页复用现有运行核心，只暴露受限任务与产物接口。"""

import asyncio
import codecs
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
import httpx
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..providers.base import ModelClient
from ..schemas import RunConfig
from .config import WebConfig
from .schemas import CreateRunRequest, RunView, ReviewDecision
from .service import WebRunManager, WebServiceError


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
) -> FastAPI:
    """创建本地应用并返回 FastAPI；run_config/runs_root=None 使用安全默认配置与项目 .runs。

    web_config 固定可选工作区和前端目录；client/transport 是离线测试注入点，None 用真实模型。
    lifespan 关闭时会等待活动任务清理；构造不调用模型，不读取密钥值到响应或日志。
    """
    manager = WebRunManager(
        web_config,
        run_config or RunConfig(),
        runs_root or web_config.project_dir / ".runs",
        model_client=model_client,
        transport=transport,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """在 app 生命周期内维护后台任务，退出时等待取消清理；yield None 不产生额外资源。"""
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(title="Agent Harness 本地控制台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager  # 公开服务实例供生命周期与离线测试协调。

    @app.middleware("http")
    async def same_origin(request: Request, call_next) -> Response:
        """拒绝跨源修改请求和不可信 Host；call_next 继续路由，返回添加基础安全头的响应。

        无 Origin 的本地 CLI/测试请求允许；浏览器仅允许同源与固定 Vite 开发源。
        testserver 仅是 ASGI 测试惯例名称，真实服务仍由 CLI 固定绑定 127.0.0.1。
        """
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return _error(400, "invalid_host", "本地服务只接受本机地址。")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed_origins = {
                f"{request.url.scheme}://{request.url.netloc}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            }
            if (
                request.headers.get("sec-fetch-site") == "cross-site"
                or (origin is not None and origin not in allowed_origins)
            ):
                return _error(403, "cross_origin_forbidden", "任务操作必须来自本地控制台同源页面。")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(WebServiceError)
    async def service_error(request: Request, error: WebServiceError) -> JSONResponse:
        """将 error 的受控业务字段返回给 request 调用方；不输出堆栈或原始输入。"""
        return _error(error.status, error.code, error.message)

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
        return await asyncio.to_thread(manager.options)

    @app.post("/api/runs", status_code=202, response_model=RunView)
    async def submit(request: CreateRunRequest) -> RunView:
        """按 request 创建独立后台任务并返回初始视图；不等待模型完成，忙时 409。"""
        return await manager.submit(request)

    @app.get("/api/runs")
    async def runs(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        status: Literal[
            "created", "running", "completed", "failed", "cancelled", "timed_out", "limit_exceeded"
        ] | None = None,
    ) -> dict:
        """按零起始 offset 和 1..100 的 limit 分页；status=None 不按执行状态筛选。"""
        return manager.list_runs(offset, limit, status)

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def run_detail(run_id: UUID) -> RunView:
        """返回 run_id 的实时或历史运行视图；不存在返回 404，不恢复执行动作。"""
        return manager.get_run(run_id)

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
    async def project_patch(run_id: UUID) -> FileResponse:
        """下载 run_id 完整固化补丁；强制附件，不将代码作为浏览器可执行内容。"""
        target = await manager.project_patch(run_id)
        return FileResponse(
            target, media_type="application/octet-stream", filename=f"{run_id}.patch",
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
    ) -> Response:
        """下载 run_id 已登记产物，或 preview=True 返回最多 64KiB UTF-8 纯文本预览。

        location 区分记录与工作区；二进制不可预览但可附件下载，未登记或越界返回 404。
        """
        target = manager.artifact_path(run_id, location, path)
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
    @app.get("/tasks")
    @app.get("/tasks/{run_id}")
    @app.get("/settings")
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

    return app
