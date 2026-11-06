"""可控 MCP 服务：真实 stdio 协议、资料读取、副作用及取消验收，不访问公网。"""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server import MCPServer


server = MCPServer("Harness 验收服务")  # 测试工具真实交给官方 SDK 注册和校验。


@server.tool()
def lookup(query: str) -> dict:
    """用 query 查询验收资料，返回文本和固定来源 URL；只读，不访问网络。"""
    return {"query": query, "text": Path(os.environ["HARNESS_MCP_FIXTURE"]).read_text(encoding="utf-8"), "url": "https://example.test/docs/reference"}


@server.tool()
def change(value: str) -> dict:
    """将 value 追加到验收计数文件并返回当前内容；用于确认审批前零副作用及不重放。"""
    path = Path(os.environ["HARNESS_MCP_FIXTURE"]).with_suffix(".count")
    path.write_text((path.read_text() if path.exists() else "") + value, encoding="utf-8")
    return {"value": path.read_text()}


@server.tool()
async def slow(value: str) -> dict:
    """先产生 value 副作用再等待60秒；返回结果前被取消，用于核对未知结果不重试。"""
    change(value)
    await asyncio.sleep(60)
    return {"value": value}


@server.tool()
def echo_credential() -> str:
    """返回明确传入的测试环境值；验证客户端不会把秘密存进工具结果或日志。"""
    return os.environ.get("HARNESS_MCP_SECRET", "missing")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--http":
        server.run(transport="streamable-http", port=int(sys.argv[2]))
    else:
        server.run()
