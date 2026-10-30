"""Electron 子进程入口；标准输入提供私有启动信息，HTTP 服务与原生通道共用一个事件循环。"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import socket
import sys

from pydantic import Field, SecretStr, ValidationError
import uvicorn

from ..config import ConfigurationError, load_config
from ..providers.base import ModelClient
from ..schemas import DataModel
from ..web.api import create_app
from ..web.config import load_web_config
from .bridge import NativeBridge
from .host import DesktopHost
from .storage import PreferenceStore, resource_root


class Bootstrap(DataModel):
    """仅父子标准流传输的启动凭据；禁止通过 argv、环境或运行配置传递。"""

    token: str = Field(min_length=32, max_length=256, repr=False)  # 当前进程独立的随机会话值。
    api_key: SecretStr = Field(max_length=2560)  # 解密后的模型凭据；空值允许查看历史。


async def serve(data_directory: Path, runs_directory: Path, config_path: Path | None, bootstrap: Bootstrap, bridge: NativeBridge, model_client: ModelClient | None = None) -> None:
    """启动本机后台并等待完整关闭；路径固定数据/记录/配置，bootstrap 仅驻留内存。

    bridge 在父进程关闭管道或发送 shutdown 时请求停止；model_client=None 使用真实模型，
    测试可显式注入离线模型。返回 None，启动失败抛受控配置错误，不自动重放任务。
    """
    root = resource_root()
    store = PreferenceStore(data_directory)
    config = load_config(config_path or root / "harness.example.toml")
    if store.value.config is not None:
        config = store.value.config.apply(config)
    web_config = load_web_config(project_dir=root).model_copy(update={"worktrees_root": store.directory / "worktrees"})
    if not (web_config.frontend_dist / "index.html").is_file():
        raise ConfigurationError("前端尚未构建，请先运行 npm.cmd --prefix frontend run build。")
    host = DesktopHost(store, bridge, bootstrap.token, bootstrap.api_key.get_secret_value())
    app = create_app(web_config, config, runs_directory, desktop=host, model_client=model_client)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", http="h11", ws="none", log_config=None, access_log=False, timeout_graceful_shutdown=2))

        def shutdown() -> None:
            """父进程请求退出或管道断开时停止接纳请求，等待统一 Runner 清理；返回 None。"""
            server.should_exit = True

        bridge.start(asyncio.get_running_loop(), shutdown)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started and not task.done():
            await asyncio.sleep(.01)
        if server.started and not task.done():
            bridge.send({"type": "ready", "port": port})
        await task
    finally:
        listener.close()


def main(argv: list[str] | None = None, *, model_client: ModelClient | None = None) -> int:
    """解析 argv 并从标准输入启动后台；成功清理返回0，配置或协议错误返回2。

    model_client 仅供显式测试入口注入，None 使用真实供应商；没有对应产品命令行开关。
    标准输出只发送受控 JSON，日志写入标准错误，由 Electron 收集到应用数据目录。
    """
    parser = argparse.ArgumentParser(description="Electron 管理的 Agent Harness 后台服务")
    parser.add_argument("--data-dir", type=Path, required=True, help="应用公开设置目录")
    parser.add_argument("--runs-dir", type=Path, required=True, help="固定运行记录目录")
    parser.add_argument("--config", type=Path, help="宿主默认运行配置，省略使用打包配置")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    bridge = NativeBridge(sys.stdin, sys.stdout)
    try:
        line = sys.stdin.readline(65537)
        if len(line) > 65536:
            raise ValueError("启动信息超过限制")
        bootstrap = Bootstrap.model_validate_json(line)
        asyncio.run(serve(args.data_dir.resolve(), args.runs_dir.resolve(), args.config, bootstrap, bridge, model_client))
        return 0
    except (ConfigurationError, ValidationError, ValueError):
        bridge.send({"type": "error", "message": "后台启动配置无效，请检查应用设置和配置文件。"})
        return 2
    except Exception:
        bridge.send({"type": "error", "message": "后台服务异常退出，请检查应用日志与目录权限。"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
