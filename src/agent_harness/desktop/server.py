"""Electron 子进程入口；标准输入提供私有启动信息，HTTP 服务与原生通道共用一个事件循环。"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import socket
import sys
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError
import uvicorn

from agent_harness.config import ConfigurationError, load_config
from agent_harness.providers.base import ModelClient
from agent_harness.schemas import DataModel, RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import load_web_config
from agent_harness.desktop.bridge import NativeBridge
from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import PreferenceStore, resource_root


class Bootstrap(DataModel):
    """仅父子标准流传输的启动凭据；禁止通过 argv、环境或运行配置传递。"""

    token: str = Field(min_length=32, max_length=256, repr=False)  # 当前进程独立的随机会话值。
    model_keys: dict[Literal["deepseek", "qwen"], Annotated[SecretStr, Field(max_length=2560)]]  # 启动携带内置供应商密钥，自定义密钥按登记版本读取。


async def serve(data_directory: Path, runs_directory: Path, config_path: Path | None, bootstrap: Bootstrap, bridge: NativeBridge, model_client: ModelClient | None = None, *, web_config_path: Path | None = None) -> None:
    """启动本机后台并等待完整关闭；路径固定数据/记录/配置，bootstrap 仅驻留内存。

    bridge 在父进程关闭管道或发送 shutdown 时请求停止；model_client=None 使用真实模型，
    web_config_path=None 使用内置宿主容量；非空读取调度容量、共享资源及受控项目配置。
    测试可显式注入离线模型。返回 None，启动失败抛受控配置错误，不自动重放任务。
    """
    root = resource_root()
    store = PreferenceStore(data_directory)
    config = load_config(config_path or root / "harness.example.toml")
    config = RunConfig.model_validate({**config.model_dump(), "custom_providers": {**config.custom_providers, **store.value.custom_providers}})
    if store.value.config is not None:
        config = store.value.config.apply(config)
    # 桌面复用宿主预设和调度规则；界面始终使用随应用分发的构建，工作树仍由应用数据目录管理。
    web_config = load_web_config(web_config_path, project_dir=root).model_copy(update={"worktrees_root": store.directory / "worktrees", "frontend_dist": root / "frontend" / "dist"})
    if not (web_config.frontend_dist / "index.html").is_file():
        raise ConfigurationError("前端尚未构建，请先运行 npm.cmd --prefix frontend run build。")
    host = DesktopHost(store, bridge, bootstrap.token, {provider: key.get_secret_value() for provider, key in bootstrap.model_keys.items()})
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
        await host.load_custom_keys(config)
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
    parser.add_argument("--web-config", type=Path, help="宿主目录、调度容量及串行资源组配置，省略使用内置默认值")
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
        asyncio.run(serve(args.data_dir.resolve(), args.runs_dir.resolve(), args.config, bootstrap, bridge, model_client, web_config_path=args.web_config))
        return 0
    except (ConfigurationError, ValidationError, ValueError):
        bridge.send({"type": "error", "message": "后台启动配置无效，请检查应用设置和配置文件。"})
        return 2
    except Exception:
        bridge.send({"type": "error", "message": "后台服务异常退出，请检查应用日志与目录权限。"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
