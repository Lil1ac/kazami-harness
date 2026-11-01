"""通过受控标准流请求 Electron 原生能力；父进程消失时触发后台任务完整收尾。"""

import asyncio
from collections.abc import Callable
import json
import threading
from typing import Protocol, TextIO
from uuid import uuid4

from agent_harness.config import ConfigurationError


class NativeClient(Protocol):
    """后台依赖的原生请求接口；只连接当前 Electron 父进程，测试可注入离线实现。"""

    async def request(self, method: str, parameters: dict) -> object:
        """请求原生 method 并返回 JSON 结果；parameters 由固定宿主代码构造，失败抛受控错误。"""
        ...


class NativeBridge:
    """异步请求与专用输入线程配对，HTTP 事件循环不会被原生对话框阻塞。"""

    def __init__(self, reader: TextIO, writer: TextIO) -> None:
        """绑定父进程的 reader/writer；构造不启动线程，不记录传输中的密钥。"""
        self.reader = reader  # Electron 协议输入，EOF 表示父进程已经退出。
        self.writer = writer  # 只输出 JSON 协议，普通日志写入标准错误。
        self._write_lock = threading.Lock()  # ready 和原生请求共用的输出锁。
        self._pending: dict[str, asyncio.Future] = {}  # 当前事件循环内等待的原生结果。
        self._loop: asyncio.AbstractEventLoop | None = None  # start 时绑定服务事件循环。
        self._shutdown: Callable[[], None] | None = None  # 宿主退出或输入断开时停止 Uvicorn。
        self._closed = False  # 通道丢失后拒绝新原生请求。

    def start(self, loop: asyncio.AbstractEventLoop, shutdown: Callable[[], None]) -> None:
        """绑定 loop 与 shutdown 并启动守护输入线程；线程不能阻止后台退出，返回 None。"""
        self._loop, self._shutdown = loop, shutdown
        threading.Thread(target=self._read, name="electron-parent-input", daemon=True).start()

    def send(self, value: dict) -> None:
        """向父进程发送一条 value JSON；不写日志，管道断开由上层转为受控通道错误。"""
        with self._write_lock:
            self.writer.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.writer.flush()

    async def request(self, method: str, parameters: dict) -> object:
        """请求 method(parameters) 并最多等待180秒；超时或通道丢失抛受控配置错误。"""
        if self._closed or self._loop is None:
            raise ConfigurationError("桌面宿主连接已断开。")
        identifier = uuid4().hex
        future = self._loop.create_future()
        self._pending[identifier] = future
        try:
            self.send({"type": "native", "id": identifier, "method": method, "parameters": parameters})
            return await asyncio.wait_for(future, 180)
        except (OSError, asyncio.TimeoutError):
            raise ConfigurationError("桌面原生操作未完成，请重试。") from None
        finally:
            self._pending.pop(identifier, None)

    def _read(self) -> None:
        """有界读取父进程 JSON 并投递到事件循环；EOF、协议错误或 shutdown 都发起清理。"""
        try:
            while line := self.reader.readline(65537):
                if len(line) > 65536:
                    break
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("type") == "shutdown":
                    break
                if message.get("type") != "result":
                    break
                self._loop.call_soon_threadsafe(self._deliver, message)
        except (OSError, ValueError, RuntimeError):
            pass  # 原始错误可能包含原生参数，不输出到日志。
        finally:
            try:
                self._loop.call_soon_threadsafe(self._lost)
            except RuntimeError:
                pass  # 后台事件循环已退出，无需再次请求清理。

    def _deliver(self, message: dict) -> None:
        """在服务事件循环内配对 message；过期、取消或未知编号的结果直接忽略。"""
        future = self._pending.get(message.get("id"))
        if future is not None and not future.done():
            if message.get("error"):
                future.set_exception(ConfigurationError("桌面原生操作失败，请检查本机权限并重试。"))
            else:
                future.set_result(message.get("result"))

    def _lost(self) -> None:
        """关闭通道并结束所有等待，然后请求服务按 Runner 生命周期清理；返回 None。"""
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConfigurationError("桌面宿主连接已断开。"))
        if self._shutdown is not None:
            self._shutdown()
