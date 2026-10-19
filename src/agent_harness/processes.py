"""受预算约束的命令执行：并发采集双流，并在中止时回收当前命令的进程树。"""

import asyncio
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import signal
from typing import BinaryIO
from uuid import uuid4

from .schemas import RunLimits
from .store import StorageError


@dataclass
class _Capture:
    """保存一个命令的两份原始文件及有界预览，两个读取任务共享总字节预算。"""

    handles: dict[str, BinaryIO]
    refs: dict[str, str]
    previews: dict[str, bytearray] = field(default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()})
    stream_bytes: dict[str, int] = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    captured_bytes: int = 0
    exceeded: asyncio.Event = field(default_factory=asyncio.Event)


def _child_environment() -> dict[str, str]:
    """仅传递启动程序所需的路径、临时目录和区域变量，排除凭据及注入变量。"""
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return result


def _windows_taskkill() -> str:
    """从 Windows 系统 API 获取固定系统程序，避免按 PATH 查找可被替换的 taskkill。"""
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("system_directory_unavailable")
    return str(Path(buffer.value) / "taskkill.exe")


class ProcessRunner:
    """一次只执行一个命令；原始输出留在运行产物目录，模型只收到有界预览。"""

    def __init__(self, limits: RunLimits, artifacts_dir: Path) -> None:
        """绑定本次运行的预算与 artifacts 目录，构造阶段不产生目录或进程。"""
        self.limits = limits
        self.artifacts_dir = artifacts_dir.absolute()
        self.last_result: dict | None = None

    def _open_capture(self) -> _Capture:
        """排他创建独立命令目录与双流文件，拒绝已有符号链接重定向输出位置。"""
        handles: dict[str, BinaryIO] = {}
        try:
            if self.artifacts_dir.is_symlink():
                raise StorageError("命令产物目录不能是符号链接")
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            root = self.artifacts_dir.resolve(strict=True)
            if not root.is_relative_to(self.artifacts_dir.parent.resolve(strict=True)):
                raise StorageError("命令产物目录越出运行范围")
            commands = self.artifacts_dir / "commands"
            if commands.is_symlink():
                raise StorageError("命令输出目录不能是符号链接")
            commands.mkdir(exist_ok=True)
            if not commands.resolve(strict=True).is_relative_to(root):
                raise StorageError("命令输出目录越出产物范围")
            command_id = str(uuid4())
            directory = commands / command_id
            directory.mkdir()
            if directory.is_symlink() or not directory.resolve(strict=True).is_relative_to(root):
                raise StorageError("命令输出目录越出产物范围")
            refs = {}
            for name in ("stdout", "stderr"):
                handles[name] = (directory / f"{name}.bin").open("xb")
                refs[name] = f"artifacts/commands/{command_id}/{name}.bin"
            return _Capture(handles=handles, refs=refs)
        except (OSError, RuntimeError) as exc:
            for handle in handles.values():
                handle.close()
            raise StorageError("创建命令原始输出记录失败") from exc
        except StorageError:
            for handle in handles.values():
                handle.close()
            raise

    async def _capture_stream(self, name: str, stream: asyncio.StreamReader, capture: _Capture) -> None:
        """持续排空一个管道；每次写入前扣除共享字节预算，不累计完整输出到内存。"""
        preview_bytes = self.limits.tool_output_preview_chars * 4 + 4
        try:
            while chunk := await stream.read(65536):
                if capture.exceeded.is_set():
                    # 超限后继续丢弃排空，避免管道背压阻塞 kill 后的进程回收。
                    continue
                remaining = self.limits.max_process_capture_bytes - capture.captured_bytes
                saved = chunk[:remaining]
                # 计数与写入之间没有 await，两个流不会竞争后共同突破上限。
                if saved:
                    capture.handles[name].write(saved)
                    capture.captured_bytes += len(saved)
                    capture.stream_bytes[name] += len(saved)
                    preview_remaining = preview_bytes - len(capture.previews[name])
                    capture.previews[name].extend(saved[:preview_remaining])
                if len(saved) < len(chunk):
                    capture.exceeded.set()
        except OSError as exc:
            raise StorageError("保存命令原始输出失败") from exc

    async def _watch(self, process: asyncio.subprocess.Process, readers: list[asyncio.Task], waiter: asyncio.Task, capture: _Capture, timeout: float) -> str:
        """等待进程及双流结束，同时监控超时、超限和父进程退出后的遗留管道。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        parent_exited_at: float | None = None
        while True:
            for reader in readers:
                if reader.done() and not reader.cancelled() and reader.exception() is not None:
                    raise reader.exception()
            if capture.exceeded.is_set():
                return "output_limit"
            if process.returncode is not None and all(reader.done() for reader in readers):
                await waiter
                return "finished"
            now = loop.time()
            if process.returncode is not None:
                parent_exited_at = now if parent_exited_at is None else parent_exited_at
                if now - parent_exited_at >= 0.15:
                    # Windows 的旧 PID 可能已复用，不能再对已退出父进程执行 taskkill。
                    return "descendants_unconfirmed"
            if now >= deadline:
                return "timeout"
            pending = [task for task in [*readers, waiter] if not task.done()]
            await asyncio.wait(pending, timeout=min(0.05, deadline - now), return_when=asyncio.FIRST_COMPLETED)

    @staticmethod
    def _discard_late_spawn(task: asyncio.Task) -> None:
        """消费超出清理宽限的创建结果，并尽力终止稍后才交付的直属进程。"""
        if task.cancelled():
            return
        try:
            process = task.result()
            if process.returncode is None:
                process.kill()
        except (OSError, ValueError):
            pass

    async def _recover_spawn(self, task: asyncio.Task, deadline: float) -> tuple[asyncio.subprocess.Process | None, bool]:
        """在清理剩余预算内获取创建结果；拿不到句柄时标记无法确认，绝不无限等待。"""
        await asyncio.wait([task], timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        if not task.done():
            task.add_done_callback(self._discard_late_spawn)
            task.cancel()
            return None, False
        try:
            return task.result(), True
        except (asyncio.CancelledError, OSError, ValueError):
            return None, True

    async def _cleanup(self, process: asyncio.subprocess.Process, deadline: float | None = None) -> str:
        """在独立宽限内终止当前命令树并回收；确认不足时保留不完整状态。"""
        if os.name == "nt" and process.returncode is not None:
            return "unknown"
        helper: asyncio.subprocess.Process | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.cleanup_grace_seconds if deadline is None else deadline
        try:
            async with asyncio.timeout_at(deadline):
                if os.name == "nt":
                    helper = await asyncio.create_subprocess_exec(
                        _windows_taskkill(), "/PID", str(process.pid), "/T", "/F",
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL, env=_child_environment(),
                    )
                    helper_code = await helper.wait()
                    if helper_code != 0:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                        return "incomplete"
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.wait()
                return "complete"
        except (TimeoutError, OSError, ProcessLookupError):
            # 树回收失败时仍尽力结束我们直接创建的父进程，但不谎报树已清理完成。
            if process.returncode is None:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
            if loop.time() < deadline:
                try:
                    async with asyncio.timeout_at(deadline):
                        await process.wait()
                except (TimeoutError, OSError):
                    pass
            return "incomplete"
        finally:
            if helper is not None and helper.returncode is None:
                try:
                    helper.kill()
                    if loop.time() < deadline:
                        async with asyncio.timeout_at(deadline):
                            await helper.wait()
                except (TimeoutError, OSError, ProcessLookupError):
                    pass

    async def _settle(self, process: asyncio.subprocess.Process | None, tasks: list[asyncio.Task], capture: _Capture, stop: bool, deadline: float | None = None) -> str:
        """完成必要清理、停止采集任务并刷新双流文件，确保返回前输出不会继续变化。"""
        status = "not_needed"
        if process is not None and stop:
            status = await self._cleanup(process, deadline)
        # 不完整清理或已退出父进程的遗留管道必须主动关闭本地读端，避免事件循环挂住。
        if process is not None and (status in {"unknown", "incomplete"}):
            for stream in (process.stdout, process.stderr):
                transport = getattr(stream, "_transport", None)
                if transport is not None:
                    transport.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        failure: OSError | None = None
        for handle in capture.handles.values():
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as exc:
                failure = exc
            finally:
                handle.close()
        if failure is not None:
            raise StorageError("刷新命令原始输出失败") from failure
        return status

    async def run(self, argv: list[str], cwd: Path, *, timeout_seconds: float | None = None) -> dict:
        """执行 argv 并返回有界输出和清理状态；取消时附带结果后原样重新抛出取消异常。"""
        timeout = self.limits.command_timeout_seconds if timeout_seconds is None else timeout_seconds
        if not argv or any(not isinstance(value, str) or "\x00" in value for value in argv) or not argv[0].strip():
            raise ValueError("命令参数必须是非空字符串数组")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("命令超时必须为有限正数")
        if os.name == "nt" and Path(argv[0]).suffix.lower() in {".bat", ".cmd", ".ps1"}:
            raise ValueError("脚本必须通过显式解释器执行")
        capture = self._open_capture()
        process: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task] = []
        reason = "spawn_failed"
        cancel_error: asyncio.CancelledError | None = None
        storage_error: StorageError | None = None
        loop = asyncio.get_running_loop()
        command_deadline = loop.time() + timeout
        cleanup_deadline: float | None = None
        spawn_unconfirmed = False
        try:
            options = {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=_child_environment(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536, **options,
            ))
            try:
                async with asyncio.timeout_at(command_deadline):
                    process = await asyncio.shield(spawn)
            except (asyncio.CancelledError, TimeoutError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    cancel_error = exc
                    reason = "cancelled"
                else:
                    reason = "timeout"
                # 创建耗时也计入命令预算；回收创建竞态与之后的树清理共享同一宽限。
                cleanup_deadline = loop.time() + self.limits.cleanup_grace_seconds
                recovery = asyncio.create_task(self._recover_spawn(spawn, cleanup_deadline))
                while not recovery.done():
                    try:
                        await asyncio.shield(recovery)
                    except asyncio.CancelledError as repeated:
                        cancel_error = repeated if cancel_error is None else cancel_error
                        reason = "cancelled"
                process, confirmed = recovery.result()
                spawn_unconfirmed = not confirmed
            if process is not None:
                readers = [
                    asyncio.create_task(self._capture_stream("stdout", process.stdout, capture)),
                    asyncio.create_task(self._capture_stream("stderr", process.stderr, capture)),
                ]
                waiter = asyncio.create_task(process.wait())
                tasks = [*readers, waiter]
                if reason not in {"cancelled", "timeout"}:
                    reason = await self._watch(process, readers, waiter, capture, max(0.0, command_deadline - loop.time()))
        except asyncio.CancelledError as exc:
            cancel_error = exc if cancel_error is None else cancel_error
            reason = "cancelled"
        except StorageError as exc:
            storage_error = exc
            reason = "storage_error"
        except (OSError, ValueError):
            reason = "cancelled" if cancel_error is not None else "spawn_failed"
        # 清理任务与调用方取消隔离；重复取消也只等待同一个有预算的清理操作。
        settling = asyncio.create_task(self._settle(process, tasks, capture, reason not in {"finished", "spawn_failed"}, cleanup_deadline))
        while not settling.done():
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError as exc:
                cancel_error = exc if cancel_error is None else cancel_error
                reason = "cancelled"
        cleanup = settling.result()
        if spawn_unconfirmed:
            cleanup = "unknown"
        stdout = capture.previews["stdout"].decode("utf-8", errors="replace")
        stderr = capture.previews["stderr"].decode("utf-8", errors="replace")
        preview_chars = self.limits.tool_output_preview_chars
        truncated = capture.exceeded.is_set() or any(
            capture.stream_bytes[name] > len(capture.previews[name]) or len(text) > preview_chars
            for name, text in (("stdout", stdout), ("stderr", stderr))
        )
        errors = {"spawn_failed": "command_spawn_failed", "timeout": "command_timeout", "output_limit": "command_output_limit", "cancelled": "command_cancelled", "descendants_unconfirmed": "command_descendants_unconfirmed"}
        error_code = errors.get(reason)
        if reason == "finished" and process.returncode != 0:
            error_code = "command_nonzero_exit"
        result = {
            "exit_code": None if process is None else process.returncode,
            "stdout": stdout[:preview_chars], "stderr": stderr[:preview_chars],
            "stdout_ref": capture.refs["stdout"], "stderr_ref": capture.refs["stderr"],
            "timed_out": reason == "timeout", "output_limit_exceeded": capture.exceeded.is_set(),
            "cancelled": cancel_error is not None, "cleanup_status": cleanup,
            "captured_bytes": capture.captured_bytes, "truncated": truncated, "error_code": error_code,
        }
        self.last_result = result
        if cancel_error is not None:
            cancel_error.harness_process_result = result
            raise cancel_error
        if storage_error is not None:
            raise storage_error
        return result
