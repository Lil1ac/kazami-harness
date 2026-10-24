"""核对原生选择器的单窗口、取消、超时和输出契约；不打开真实桌面窗口。"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_harness.web.folder_picker import FolderPicker, _native_environment
from agent_harness.web.project_catalog import CatalogError


class FakeDialog:
    """受控选择器子进程，用于核对清理而不干扰用户桌面。"""

    def __init__(self, value: bytes, hold: bool = False):
        """value是模拟标准输出；hold=True时等待调用方取消或超时，返回进程替身。"""
        self.value = value  # 一次JSON输出。
        self.hold = hold  # 是否模拟窗口等待用户。
        self.returncode = None if hold else 0  # 未完成时为空。
        self.killed = False  # 仅在真实清理方法调用后变为True。
        self.waited = False  # 清理必须等待进程退出。

    async def communicate(self):
        """返回模拟字节；等待场景持续挂起直至测试取消协程。"""
        if self.hold:
            await asyncio.Event().wait()
        return self.value, b""

    def kill(self):
        """记录终止GUI进程并设置退出码，返回None，无系统副作用。"""
        self.killed = True
        self.returncode = -9

    async def wait(self):
        """记录等待已终止子进程，返回它的模拟退出码。"""
        self.waited = True
        return self.returncode


def attach(monkeypatch, dialog: FakeDialog) -> list[tuple]:
    """把dialog注入选择器并返回启动参数列表；不改变真实进程或桌面。"""
    launches = []
    monkeypatch.setattr(FolderPicker, "available", property(lambda self: True))
    async def spawn(*args, **kwargs):
        """保存受控启动参数，返回模拟进程；不解释或执行args。"""
        launches.append(args)
        return dialog
    monkeypatch.setattr("agent_harness.web.folder_picker.asyncio.create_subprocess_exec", spawn)
    return launches


@pytest.mark.parametrize("path", [None, str(Path("C:/项目 目录") if __import__("os").name == "nt" else Path("/项目 目录"))])
def test_picker_returns_selection_or_cancel_without_shell(monkeypatch, path):
    """选择与取消分别返回Path/None，启动不经Shell且直接运行标准库GUI脚本。"""
    dialog = FakeDialog(json.dumps({"path":path}).encode("utf-8"))
    launches = attach(monkeypatch,dialog)
    result = asyncio.run(FolderPicker().choose())
    assert result == (Path(path) if path is not None else None)
    assert "-I" in launches[0] and launches[0][-1].endswith("native_dialog.py")


def test_timeout_kills_and_waits_for_gui_process(monkeypatch):
    """超时必须终止并等待GUI进程，释放窗口槽，下次明确选择仍可重试。"""
    dialog = FakeDialog(b"",hold=True)
    attach(monkeypatch,dialog)
    monkeypatch.setattr(FolderPicker,"TIMEOUT_SECONDS",.01)
    picker = FolderPicker()
    with pytest.raises(CatalogError) as captured:
        asyncio.run(picker.choose())
    assert captured.value.code == "folder_picker_timeout"
    assert dialog.killed and dialog.waited and not picker._active


def test_concurrent_picker_rejected_and_cancel_cleans_process(monkeypatch):
    """并发点击拒绝第二个窗口，取消第一次请求后同样等待GUI退出。"""
    dialog = FakeDialog(b"",hold=True)
    attach(monkeypatch,dialog)
    async def scenario():
        """在同一事件循环中协调两次点击与取消，返回None。"""
        picker=FolderPicker()
        task=asyncio.create_task(picker.choose())
        await asyncio.sleep(0)
        with pytest.raises(CatalogError) as captured:
            await picker.choose()
        assert captured.value.code == "folder_picker_busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert dialog.killed and dialog.waited and not picker._active
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [b"[]", b"invalid", b'{"path":"relative"}'])
def test_invalid_output_is_controlled_and_unlocks_picker(monkeypatch,value):
    """不合法的子进程结果转换为稳定错误，不能把未知输出登记成目录。"""
    attach(monkeypatch,FakeDialog(value))
    picker=FolderPicker()
    with pytest.raises(CatalogError) as captured:
        asyncio.run(picker.choose())
    assert captured.value.code == "folder_picker_failed" and not picker._active


def test_native_environment_preserves_shell_paths_without_credentials(monkeypatch):
    """GUI 补齐 Shell 展开变量，不泄露密钥或修改父环境；其他命令环境仍受限。"""
    desktop = {
        "SYSTEMDRIVE": "C:", "PROGRAMDATA": "C:/ProgramData",
        "USERPROFILE": "C:/Users/test", "APPDATA": "C:/Users/test/AppData/Roaming",
        "LOCALAPPDATA": "C:/Users/test/AppData/Local", "HOMEDRIVE": "C:", "HOMEPATH": "/Users/test",
    }
    for key, value in desktop.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv("PYTHONPATH", "unexpected-injection")
    original = dict(os.environ)
    result = _native_environment()
    assert all(result[key] == value for key, value in desktop.items())
    assert "DEEPSEEK_API_KEY" not in result and "PYTHONPATH" not in result
    assert dict(os.environ) == original
    from agent_harness.processes import _child_environment
    assert "PROGRAMDATA" not in _child_environment()


def test_native_picker_uses_temporary_cwd(monkeypatch):
    """GUI 辅助进程在已有临时目录启动，避免 Shell 缓存污染用户项目。"""
    launches = []  # 记录关键字参数，核对启动位置和凭据隔离。
    monkeypatch.setattr(FolderPicker, "available", property(lambda self: True))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")

    async def spawn(*args, **kwargs):
        """接收 GUI 启动参数但不创建窗口，返回已取消的模拟进程。"""
        launches.append(kwargs)
        return FakeDialog(b'{"path":null}')

    monkeypatch.setattr("agent_harness.web.folder_picker.asyncio.create_subprocess_exec", spawn)
    assert asyncio.run(FolderPicker().choose()) is None
    assert launches[0]["cwd"] == tempfile.gettempdir()
    assert "DEEPSEEK_API_KEY" not in launches[0]["env"]
