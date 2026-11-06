"""用官方 SDK 执行一次有界发现或调用；未知结果不会自动重放。"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from jsonschema import validators
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from agent_harness.mcp.models import McpServerConfig
from agent_harness.work.tasks import TaskError
from agent_harness.execution.scheduling import capacity

# SDK 解析异常可能带原始服务输出或 URL；诊断统一经下方受控错误和持久事件提供，
# 不让第三方协议日志绕过本项目的凭据边界。此模块只用于 Harness 的 MCP 客户端。
for _namespace in ("mcp", "httpx2", "httpcore2"):
    _logger = logging.getLogger(_namespace)
    _logger.addHandler(logging.NullHandler())
    _logger.propagate = False


class McpError(TaskError):
    """受控外部错误；unknown 表示工具请求已发出，不能由模型盲目重试。"""

    def __init__(self, code: str, message: str, *, unknown: bool = False) -> None:
        """保存固定 code/message 和 unknown 副作用标记；不保留原始异常正文，返回 None。"""
        super().__init__(code, message)
        self.unknown = unknown  # 仅用于宿主禁用连接及固定历史证据。


def validate_schema(schema: dict) -> None:
    """验证工具 schema；只允许文档内引用，拒绝验证器通过网络加载外部 Schema。"""
    def inspect(value):
        """递归检查 value 内引用；无返回值，外部引用或结构过深由调用者拒绝。"""
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                    raise ValueError("不支持外部 Schema 引用")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    inspect(schema)
    validators.validator_for(schema).check_schema(schema)


def catalog_value(tools) -> list[dict]:
    """将 SDK tools 转为有界、排序且已校验的工具目录；非法定义整次拒绝，不静默丢弃。"""
    result = []
    for tool in tools:
        value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        entry = {key: value[key] for key in ("name", "description", "inputSchema", "outputSchema", "annotations") if key in value}
        if not entry.get("name") or len(entry["name"]) > 128 or len(entry.get("description", "")) > 16000:
            raise ValueError("工具描述超限")
        validate_schema(entry["inputSchema"])
        if "outputSchema" in entry:
            validate_schema(entry["outputSchema"])
        result.append(entry)
    if len(result) > 128 or len({item["name"] for item in result}) != len(result) or len(json.dumps(result).encode()) > 512 * 1024:
        raise ValueError("目录重复或超限")
    return sorted(result, key=lambda item: item["name"])


def local_identity(config: McpServerConfig) -> dict:
    """返回 stdio 实际入口、目录及显式脚本身份；HTTP 返回空值，不推断全部传递依赖。"""
    if config.transport != "stdio":
        return {}
    executable, cwd = Path(config.command), Path(config.cwd)
    if not executable.is_file() or not cwd.is_dir() or executable.is_symlink() or cwd.is_symlink() or cwd.is_junction():
        raise McpError("mcp_target_changed", "本机服务程序或目录不可核验，请重新配置审核。")
    paths = [executable, *(cwd / arg for arg in config.args if Path(arg).suffix.lower() in {".py", ".js", ".ps1", ".sh"})]
    result = {"cwd": str(cwd.resolve()), "files": {}}
    for path in paths:
        if not path.is_file() or path.is_symlink() or path.is_junction():
            raise McpError("mcp_target_changed", "服务入口脚本已变化或不可访问。")
        stat = path.stat()
        result["files"][str(path.resolve())] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    stat = cwd.stat()
    result["directory_identity"] = [stat.st_dev, stat.st_ino]
    return result


@asynccontextmanager
async def connect(config: McpServerConfig, secret: str, statuses: list[int] | None = None):
    """连接 config 并 yield SDK Client；secret 仅认证，statuses 可接收认证状态码，None 不观察。退出清理资源。"""
    if config.transport == "stdio":
        # SDK 只继承系统必需变量，服务 stderr 不流入桌面私有协议或未经脱敏的日志。
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            transport = stdio_client(StdioServerParameters(command=config.command, args=config.args, cwd=config.cwd,
                env={name: os.environ[name] for name in config.env_vars if name in os.environ}), errlog=errlog)
            async with capacity("processes"):
                async with Client(transport, read_timeout_seconds=config.startup_timeout_seconds, cache=None) as client:
                    yield client
    else:
        headers = {config.credential_header: ("Bearer " + secret if config.credential_header == "Authorization" else secret)} if secret else {}
        loopback = urlsplit(config.url).hostname in {"localhost", "127.0.0.1", "::1"}
        async def observe(response):
            """只记录 response 的401/403状态，不读取响应正文；用于识别 SDK 包装后的认证失败。"""
            if statuses is not None and response.status_code in {401, 403}:
                statuses.append(response.status_code)
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(config.tool_timeout_seconds, connect=config.startup_timeout_seconds), follow_redirects=False, trust_env=not loopback, event_hooks={"response": [observe]}) as http:
            async with Client(streamable_http_client(config.url, http_client=http), read_timeout_seconds=config.startup_timeout_seconds, cache=None) as client:
                yield client


async def perform(config: McpServerConfig, *, secret: str = "", tool: dict | None = None, arguments: dict | None = None) -> dict:
    """发现 config 或调用 tool；None 仅发现。校验当前定义，返回目录/协议及有界结果，异常不暴露凭据。"""
    called = False
    statuses: list[int] = []  # SDK 可能把 HTTP 异常转为协议错误，认证状态独立保留且不含正文。
    try:
        async with asyncio.timeout(config.startup_timeout_seconds) as timeout:
            async with connect(config, secret, statuses) as client:
                tools, cursor = [], None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    if len(tools) > 128:
                        raise ValueError("工具目录超限")
                    cursor = page.next_cursor
                    if not cursor:
                        break
                catalog = catalog_value(tools)
                private_values = [value for value in [secret, *(os.environ.get(name, "") for name in config.env_vars)] if value]
                directory_json = json.dumps(catalog, ensure_ascii=False)
                if any(json.dumps(value, ensure_ascii=False)[1:-1] in directory_json for value in private_values):
                    raise McpError("mcp_definition_unsafe", "服务工具定义包含私密环境值，未保存目录，请检查服务。")
                output = {"catalog": catalog, "protocol_version": client.protocol_version}
                if tool is not None:
                    current = next((value for value in catalog if value["name"] == tool["name"]), None)
                    if current != tool:
                        raise McpError("mcp_definition_changed", "远端工具定义已变化，原参数尚未调用，请重新发现并审核。")
                    validators.validator_for(tool["inputSchema"])(tool["inputSchema"]).validate(arguments)
                    timeout.reschedule(asyncio.get_running_loop().time() + config.tool_timeout_seconds)
                    called = True
                    reply = await client.call_tool(tool["name"], arguments, read_timeout_seconds=config.tool_timeout_seconds)
                    value = reply.model_dump(mode="json", by_alias=True, exclude_none=True)
                    raw = json.dumps(value, ensure_ascii=False)
                    if secret:
                        raw = raw.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[凭据已隐藏]")
                    for name in config.env_vars:
                        if os.environ.get(name):
                            raw = raw.replace(json.dumps(os.environ[name], ensure_ascii=False)[1:-1], "[环境值已隐藏]")
                    if len(raw.encode()) > 256 * 1024:
                        # 返回超限本身已是完整回执；保留预览，不允许把缺失正文说成完整结果。
                        value = {"content": [{"type": "text", "text": raw[:16000]}], "isError": bool(value.get("isError")), "truncated": True}
                    else:
                        value = json.loads(raw)
                    if tool.get("outputSchema") and not value.get("isError") and not value.get("truncated"):
                        validators.validator_for(tool["outputSchema"])(tool["outputSchema"]).validate(value.get("structuredContent"))
                    output["result"] = value
                return output
    except asyncio.CancelledError:
        raise
    except McpError:
        raise
    except Exception as error:
        # SDK ExceptionGroup 的正文可能包含 URL、响应或环境值，只映射受控类别。
        def status(value):
            """从 value 及异常组提取 HTTP 状态码；返回整数或 None，不读取异常字符串。"""
            response = getattr(value, "response", None)
            if response is not None:
                return getattr(response, "status_code", None)
            return next((code for child in getattr(value, "exceptions", []) if (code := status(child)) is not None), None)
        code = status(error) or (statuses[-1] if statuses else None)
        if code in {401, 403} and not called:
            raise McpError("mcp_auth_required", "服务认证失败，请检查凭据；OAuth 登录尚未支持。") from None
        if called:
            raise McpError("mcp_result_unknown", "请求可能已经执行，结果未能完整核验；连接已停用，请核对远端后再启用。", unknown=True) from None
        raise McpError("mcp_connection_failed", "服务连接、发现或定义校验失败，请检查端点、程序、网络与超时。") from None
