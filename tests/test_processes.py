"""使用短生命周期真实子进程验证双流预算、环境隔离和取消回收契约。"""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from agent_harness.processes import ProcessRunner
from agent_harness.schemas import RunLimits
from agent_harness.store import StorageError


def runner(tmp_path: Path, **limits) -> ProcessRunner:
    """为每个测试创建独立产物目录，允许缩小命令时间和输出预算。"""
    return ProcessRunner(RunLimits(**limits), tmp_path / "artifacts")


def execute(tool: ProcessRunner, tmp_path: Path, source: str, **kwargs) -> dict:
    """在当前 Python 解释器中运行一段不依赖 shell 的短脚本。"""
    return asyncio.run(tool.run([sys.executable, "-c", source], tmp_path, **kwargs))


def raw_path(tmp_path: Path, result: dict, stream: str) -> Path:
    """把运行内相对输出引用映射为测试目录中的实际原始文件。"""
    return tmp_path / result[f"{stream}_ref"]


def test_unicode_space_path_and_independent_raw_outputs(tmp_path: Path) -> None:
    """验证工作目录和参数中的中文、空格无需 shell 转义，双流原始文件完整保存。"""
    workspace = tmp_path / "中文 空格"
    workspace.mkdir()
    tool = runner(tmp_path)
    result = asyncio.run(tool.run([
        sys.executable, "-c", "import os,sys;print(os.path.basename(os.getcwd()));print(sys.argv[1]);print('错误🙂',file=sys.stderr)",
        "文字 & echo injected",
    ], workspace))
    assert result["exit_code"] == 0
    assert result["error_code"] is None
    assert result["cleanup_status"] == "not_needed"
    assert "中文 空格" in result["stdout"]
    assert "文字 & echo injected" in result["stdout"]
    assert "错误🙂" in result["stderr"]
    assert raw_path(tmp_path, result, "stdout").read_bytes().decode("utf-8").replace("\r\n", "\n") == result["stdout"].replace("\r\n", "\n")
    assert result["captured_bytes"] == sum(raw_path(tmp_path, result, name).stat().st_size for name in ("stdout", "stderr"))
    second = execute(tool, tmp_path, "print('second')")
    assert second["stdout_ref"] != result["stdout_ref"]


def test_nonzero_exit_is_returned_as_business_result(tmp_path: Path) -> None:
    """验证程序非零退出保留输出和退出码，不作为运行时异常抛出。"""
    result = execute(runner(tmp_path), tmp_path, "import sys;print('problem',file=sys.stderr);sys.exit(7)")
    assert result["exit_code"] == 7
    assert result["error_code"] == "command_nonzero_exit"
    assert "problem" in result["stderr"]


def test_env_whitelist_excludes_keys_proxies_and_python_injection(tmp_path: Path, monkeypatch) -> None:
    """验证子进程不能继承模型密钥、代理凭据或 PYTHONPATH 等启动注入项。"""
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CUSTOM_SECRET", "HTTPS_PROXY", "PYTHONPATH"):
        monkeypatch.setenv(name, "sensitive-sentinel")
    result = execute(runner(tmp_path), tmp_path, "import os,json;print(json.dumps(dict(os.environ)))")
    environment = json.loads(result["stdout"])
    assert "sensitive-sentinel" not in result["stdout"]
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert "PATH" in {key.upper() for key in environment}


def test_parallel_large_stdout_stderr_do_not_deadlock_and_previews_are_bounded(tmp_path: Path) -> None:
    """验证超过管道容量的双流输出被并发排空，原始输出完整而预览有界。"""
    tool = runner(tmp_path, tool_output_preview_chars=37)
    source = "import os,threading; t=threading.Thread(target=lambda:os.write(2,b'e'*300000));t.start();os.write(1,b'o'*300000);t.join()"
    result = execute(tool, tmp_path, source)
    assert result["exit_code"] == 0
    assert result["stdout"] == "o" * 37
    assert result["stderr"] == "e" * 37
    assert result["truncated"] is True
    assert raw_path(tmp_path, result, "stdout").stat().st_size == 300000
    assert raw_path(tmp_path, result, "stderr").stat().st_size == 300000
    assert result["captured_bytes"] == 600000


def test_combined_output_cap_stops_live_process_and_never_overwrites_cap(tmp_path: Path) -> None:
    """验证 stdout 与 stderr 共用硬字节上限，超限会停止仍运行的命令。"""
    result = execute(runner(tmp_path, max_process_capture_bytes=1003), tmp_path,
                     "import os,time;os.write(1,b'a'*600);os.write(2,b'b'*800);time.sleep(20)")
    assert result["output_limit_exceeded"] is True
    assert result["error_code"] == "command_output_limit"
    assert result["captured_bytes"] == 1003
    assert sum(raw_path(tmp_path, result, name).stat().st_size for name in ("stdout", "stderr")) == 1003
    assert result["truncated"] is True
    assert result["cleanup_status"] == "complete"


def test_exact_output_cap_does_not_report_limit_exceeded(tmp_path: Path) -> None:
    """验证恰好等于字节预算且随后 EOF 的输出可以正常完成。"""
    result = execute(runner(tmp_path, max_process_capture_bytes=1003), tmp_path, "import os;os.write(1,b'a'*1003)")
    assert result["exit_code"] == 0
    assert result["output_limit_exceeded"] is False
    assert result["captured_bytes"] == 1003


def test_timeout_cleans_process_and_keeps_partial_output(tmp_path: Path) -> None:
    """验证单次命令超时会回收进程并保留中止前已刷出的输出。"""
    result = execute(runner(tmp_path), tmp_path, "import time;print('started',flush=True);time.sleep(20)", timeout_seconds=0.5)
    assert result["timed_out"] is True
    assert result["cancelled"] is False
    assert result["error_code"] == "command_timeout"
    assert result["cleanup_status"] == "complete"
    assert "started" in result["stdout"]
    assert result["exit_code"] is not None


def _process_alive(pid: int) -> bool:
    """使用平台原生查询判断测试子进程是否仍存活，不向任何无关进程发送终止信号。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        status = Path(f"/proc/{pid}/stat")
        if status.exists() and status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_clean_blocking_child_tree(tmp_path: Path, cancel: bool) -> None:
    """验证父子进程都阻塞时，超时或取消会回收树并保留原始取消对象类型。"""
    pid_file = tmp_path / "child.pid"
    source = (
        "import subprocess,sys,time,pathlib;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
        "print('parent-ready',flush=True);time.sleep(20)"
    )
    tool = runner(tmp_path)

    async def scenario() -> dict:
        """等待子进程登记后触发取消，确保测试真实覆盖已创建的进程树。"""
        task = asyncio.create_task(tool.run([sys.executable, "-c", source, str(pid_file)], tmp_path, timeout_seconds=1.0))
        if not cancel:
            return await task
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        task.cancel("test-stop")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert type(caught.value) is asyncio.CancelledError
        assert caught.value.args == ("test-stop",)
        return caught.value.harness_process_result

    result = asyncio.run(scenario())
    assert result["cleanup_status"] == "complete"
    assert result["cancelled"] is cancel
    assert result["timed_out"] is (not cancel)
    assert not _process_alive(int(pid_file.read_text()))
    assert tool.last_result == result


def test_spawn_failure_is_safe_data_and_keeps_empty_raw_files(tmp_path: Path) -> None:
    """验证不存在的程序返回固定错误码，异常文本不回显程序名或其他输入。"""
    tool = runner(tmp_path)
    result = asyncio.run(tool.run([str(tmp_path / "private-secret-no-such.exe")], tmp_path))
    assert result["error_code"] == "command_spawn_failed"
    assert result["exit_code"] is None
    assert result["cleanup_status"] == "not_needed"
    assert "private-secret" not in json.dumps(result)
    assert raw_path(tmp_path, result, "stdout").read_bytes() == b""


def test_stdin_is_closed_and_shell_metacharacters_are_literal(tmp_path: Path) -> None:
    """验证命令没有交互输入，argv 中的 shell 元字符仅作为普通字符串传递。"""
    marker = tmp_path / "injected.txt"
    result = asyncio.run(runner(tmp_path).run([
        sys.executable, "-c", "import sys;print(repr(sys.stdin.read()));print(sys.argv[1])", f"& echo hacked > {marker}",
    ], tmp_path))
    assert "''" in result["stdout"]
    assert "& echo hacked" in result["stdout"]
    assert not marker.exists()


def test_output_directory_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """验证既有产物目录符号链接不能把命令原始输出导向运行目录外。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "artifacts").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(StorageError, match="符号链接"):
        execute(runner(tmp_path), tmp_path, "print('must-not-run')")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="只在 Windows 上验证隐式 shell 脚本入口")
@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1"])
def test_windows_script_requires_explicit_interpreter(tmp_path: Path, suffix: str) -> None:
    """验证 Windows 脚本不得绕过 shell=false 的承诺而隐式解释执行。"""
    with pytest.raises(ValueError, match="显式解释器"):
        asyncio.run(runner(tmp_path).run([str(tmp_path / f"script{suffix}")], tmp_path))


def test_continuous_dual_stream_overflow_still_cleans_without_pipe_deadlock(tmp_path: Path) -> None:
    """验证持续大量双流输出在超限后仍排空，进程树回收不受管道背压阻塞。"""
    source = (
        "import os,threading\n"
        "def flood(fd):\n"
        " while True: os.write(fd,b'x'*65536)\n"
        "threading.Thread(target=flood,args=(2,),daemon=True).start()\n"
        "flood(1)\n"
    )
    result = execute(runner(tmp_path, max_process_capture_bytes=17), tmp_path, source)
    assert result["output_limit_exceeded"] is True
    assert result["captured_bytes"] == 17
    assert set(result["stdout"] + result["stderr"]) <= {"x"}
    assert result["cleanup_status"] == "complete"


def test_process_creation_counts_towards_command_timeout(tmp_path: Path, monkeypatch) -> None:
    """验证启动延迟占用单命令时限，启动超时后在清理宽限内回收刚创建的进程。"""
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        """仅延迟被测试命令创建，不延迟用于回收的系统 taskkill。"""
        if args[0] == sys.executable:
            await asyncio.sleep(0.2)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    result = execute(runner(tmp_path), tmp_path, "import time;time.sleep(20)", timeout_seconds=0.05)
    assert result["timed_out"] is True
    assert result["cleanup_status"] == "complete"


def test_unconfirmed_slow_spawn_does_not_wait_forever(tmp_path: Path, monkeypatch) -> None:
    """验证创建迟迟未交付句柄时按清理宽限返回 unknown，并取消仍在等待的创建任务。"""
    cancelled = []

    async def stalled_spawn(*args, **kwargs):
        """模拟尚未真正创建任何操作系统进程的挂起启动。"""
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled_spawn)
    tool = runner(tmp_path, cleanup_grace_seconds=0.1)
    result = execute(tool, tmp_path, "print('never-started')", timeout_seconds=0.05)
    assert result["timed_out"] is True
    assert result["cleanup_status"] == "unknown"
    assert cancelled == [True]
    assert result["captured_bytes"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows 父进程退出后不能安全复用 PID 做 taskkill")
def test_exited_parent_with_inherited_child_pipe_is_unknown(tmp_path: Path) -> None:
    """验证父进程先退出而后代仍持有管道时报告 unknown，绝不对旧 PID 执行树终止。"""
    source = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(0.7)'],stdout=sys.stdout,stderr=sys.stderr)"
    )
    result = execute(runner(tmp_path), tmp_path, source)
    assert result["cleanup_status"] == "unknown"
    assert result["error_code"] == "command_descendants_unconfirmed"


@pytest.mark.skipif(os.name != "nt", reason="Windows 固定系统 taskkill 故障回退")
def test_failed_tree_helper_is_incomplete_and_direct_parent_is_reaped(tmp_path: Path, monkeypatch) -> None:
    """验证系统树清理程序失败时仍回收直属进程，并明确报告树清理不完整。"""
    import agent_harness.processes as processes

    def unavailable_taskkill() -> str:
        """模拟系统程序路径获取失败，不启动任何额外进程。"""
        raise OSError("sensitive-value-that-must-not-appear")

    monkeypatch.setattr(processes, "_windows_taskkill", unavailable_taskkill)
    result = execute(runner(tmp_path), tmp_path, "import time;time.sleep(20)", timeout_seconds=0.2)
    assert result["cleanup_status"] == "incomplete"
    assert result["exit_code"] is not None
    assert "sensitive-value" not in json.dumps(result)


def test_raw_output_failure_raises_safe_storage_error(tmp_path: Path, monkeypatch) -> None:
    """验证原始输出写入失败会清理命令并抛固定存储错误，不能作为正常工具结果继续。"""
    tool = runner(tmp_path)
    original = tool._capture_stream

    async def broken_reader(name, stream, capture):
        """在 stdout 真实读取后模拟存储故障，stderr 仍使用真实有界采集。"""
        if name == "stdout":
            await stream.read(1)
            raise StorageError("保存命令原始输出失败")
        await original(name, stream, capture)

    monkeypatch.setattr(tool, "_capture_stream", broken_reader)
    with pytest.raises(StorageError, match="原始输出"):
        execute(tool, tmp_path, "import time;print('partial',flush=True);time.sleep(20)")
    assert tool.last_result["exit_code"] is not None
    assert tool.last_result["cleanup_status"] == "complete"


def test_cancel_during_spawn_keeps_original_cancellation_and_cleans(tmp_path: Path, monkeypatch) -> None:
    """验证启动竞态中的取消也保留原对象参数，等待句柄后回收真实进程。"""
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        """给测试命令增加启动前延迟，固定覆盖取消时尚未获得句柄的时序。"""
        if args[0] == sys.executable:
            await asyncio.sleep(0.15)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    tool = runner(tmp_path)

    async def scenario() -> dict:
        """在启动等待阶段取消命令，取得异常附带的真实清理结果。"""
        task = asyncio.create_task(tool.run([sys.executable, "-c", "import time;time.sleep(20)"], tmp_path))
        await asyncio.sleep(0.02)
        task.cancel("cancel-during-spawn")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("cancel-during-spawn",)
        return caught.value.harness_process_result

    result = asyncio.run(scenario())
    assert result["cancelled"] is True
    assert result["error_code"] == "command_cancelled"
    assert result["cleanup_status"] == "complete"
    assert result["exit_code"] is not None
