"""在可回收的宿主进程中运行文件检索，保留相同的工作区路径核验。"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from agent_harness.execution.processes import ProcessRunner, native_io_path
from agent_harness.execution.workspace import WorkspaceError
from agent_harness.permissions.policy import FILE_SCOPE

if TYPE_CHECKING:
    from agent_harness.tools.files import SearchFilesArguments


WORKER_FLAG = "--internal-search-worker"  # 仅用于宿主启动，模型工具参数不能指定命令行。


def worker_argv() -> list[str]:
    """返回当前宿主解释器或打包后台的固定 worker 参数；不使用工作区的解释器或环境。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, "-I", "-B", "-m", "agent_harness", WORKER_FLAG]


def worker_main() -> int:
    """从独立 stdin 读取宿主提交的搜索范围，向 stdout 写唯一 JSON 回执，返回进程退出码。

    输入包含已校验的工具参数、绝对目录和本轮只读根；read_roots 为 None 表示独立调用
    仅受工作区边界约束。输入超限时以非零退出；路径错误编码为既有工具错误。
    """
    from agent_harness.tools.files import SearchFilesArguments, search_files

    raw = sys.stdin.buffer.read(262145)
    if len(raw) > 262144:
        return 2
    request = json.loads(raw)
    workspace, runs_root = Path(request["workspace"]), Path(request["runs_root"])
    read_roots = request["read_roots"]
    token = FILE_SCOPE.set(None if read_roots is None else (workspace, tuple(Path(root) for root in read_roots)))
    try:
        args = SearchFilesArguments.model_validate(request["arguments"])
        data = search_files(workspace, runs_root, args, request["preview_chars"])
        receipt = {"ok": True, "data": data, "error": None, "truncated": data["truncated"]}
    except WorkspaceError as error:
        receipt = {"ok": False, "data": None, "error": {"code": error.code, "message": error.message}, "truncated": False}
    finally:
        FILE_SCOPE.reset(token)
    sys.stdout.buffer.write(json.dumps(receipt, allow_nan=False).encode("ascii"))
    return 0


def search_failure(process: dict, *, local_timeout: bool = False) -> dict:
    """把 process 转为搜索失败回执；local_timeout 区分单次时限与所属 Run 的截止时间。"""
    code = "search_worker_failed"
    if process.get("timed_out"):
        code = "search_timeout" if local_timeout else "tool_timed_out"
    elif process.get("cancelled"):
        code = "tool_cancelled"
    elif process.get("output_limit_exceeded"):
        code = "search_output_limit"
    message = {
        "search_timeout": "目录搜索达到单次时限，请缩小 directory 或给出精确的 glob 文件名。",
        "tool_timed_out": "目录搜索超过运行截止时间。",
        "tool_cancelled": "目录搜索已取消。",
        "search_output_limit": "目录搜索的输出超过保留上限。",
    }.get(code, "目录搜索进程未能返回有效结果。")
    return {
        "ok": False, "data": {key: process.get(key) for key in ("cleanup_status", "timed_out", "cancelled", "exit_code")},
        "error": {"code": code, "message": message},
        "truncated": False,
    }


async def supervise_search(
    runner: ProcessRunner, workspace: Path, runs_root: Path, arguments: "SearchFilesArguments",
    deadline: float | None, before_spawn: Callable[[], None],
) -> dict:
    """用 runner 启动并回收搜索进程，返回与原工具一致的回执。

    workspace 与 runs_root 为宿主固定的绝对目录；arguments 为已校验的模型参数。
    deadline 为 Run 的单调截止时间，None 时仅使用单次搜索时限；before_spawn 在
    实际接纳进程前提交开始事件。取消由 runner 回收进程后携带状态原样抛出。
    """
    scope = FILE_SCOPE.get()
    request = json.dumps({
        "workspace": str(workspace), "runs_root": str(runs_root),
        "arguments": arguments.model_dump(mode="json"), "preview_chars": runner.limits.tool_output_preview_chars,
        "read_roots": None if scope is None else [str(root) for root in scope[1]],
    }).encode("ascii")
    argv = worker_argv()
    search_deadline = asyncio.get_running_loop().time() + runner.limits.search_timeout_seconds
    local_timeout = deadline is None or search_deadline < deadline
    try:
        process = await runner.run(
            argv, Path(argv[0]).parent, timeout_seconds=runner.limits.search_timeout_seconds,
            stdin_bytes=request, deadline=search_deadline if local_timeout else deadline, before_spawn=before_spawn,
        )
    except TimeoutError:
        if not local_timeout or (deadline is not None and asyncio.get_running_loop().time() >= deadline):
            raise
        return search_failure({"timed_out": True, "cleanup_status": "not_needed"}, local_timeout=True)
    if process["error_code"] is not None or process["cleanup_status"] in {"unknown", "incomplete"}:
        return search_failure(process, local_timeout=local_timeout)
    output = runner.artifacts_dir.parent / process["stdout_ref"]
    try:
        # 原始输出已由 ProcessRunner 限长；预览可能截断合法 JSON。
        receipt = json.loads(native_io_path(output).read_bytes())
        if isinstance(receipt, dict) and type(receipt.get("ok")) is bool and type(receipt.get("truncated")) is bool:
            if receipt["ok"] and isinstance(receipt.get("data"), dict) and receipt.get("error") is None:
                return receipt
            if not receipt["ok"] and isinstance(receipt.get("error"), dict):
                return receipt
    except (OSError, ValueError):
        pass
    return search_failure(process)
