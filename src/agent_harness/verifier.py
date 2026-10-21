"""宿主独立验收：复用进程执行器，以退出状态和运行异常判定产物是否通过。"""

import asyncio
from pathlib import Path
from uuid import UUID

from .processes import ProcessRunner
from .schemas import ArtifactRef, RunLimits, VerificationResult, VerifierSpec


def _map_result(data: dict, elapsed_ms: int) -> VerificationResult:
    """把进程事实映射为验收结果，返回可持久化详情，不接受模型的通过声明。

    data 是 ProcessRunner 返回的字典；elapsed_ms 是从开始到收尾的耗时（毫秒）。
    取消、超时、输出超限、清理异常优先于退出码；只有正常退出 0/1 分别映射
    passed/failed。引用必须是运行内相对路径，不合法引用会触发数据校验异常。
    """
    code = data.get("exit_code")
    cleanup = data.get("cleanup_status", "unknown")
    error_code = data.get("error_code")
    if data.get("cancelled"):
        status, reason = "error", "verifier_cancelled"
    elif data.get("timed_out"):
        status, reason = "error", "verifier_timeout"
    elif data.get("output_limit_exceeded"):
        status, reason = "error", "verifier_output_limit"
    elif cleanup not in {"not_needed", "complete"}:
        status, reason = "error", "verifier_cleanup_failed"
    elif error_code == "command_spawn_failed" or code is None:
        status, reason = "error", "verifier_start_failed"
    elif type(code) is int and code == 0 and error_code is None:
        status, reason = "passed", "checks_passed"
    elif type(code) is int and code == 1 and error_code in {None, "command_nonzero_exit"}:
        status, reason = "failed", "assertion_failed"
    else:
        status, reason = "error", "verifier_exit_error"
    references = [
        ArtifactRef(location="run", path=data[key], kind=kind)
        for key, kind in (("stdout_ref", "stdout"), ("stderr_ref", "stderr")) if data.get(key)
    ]
    return VerificationResult(
        status=status, reason=reason, exit_code=code,
        stdout=data.get("stdout", ""), stderr=data.get("stderr", ""),
        timed_out=bool(data.get("timed_out")), cancelled=bool(data.get("cancelled")),
        output_limit_exceeded=bool(data.get("output_limit_exceeded")), cleanup_status=cleanup,
        elapsed_ms=elapsed_ms, truncated=bool(data.get("truncated")), artifacts=references,
    )


class Verifier:
    """执行一次宿主验收，保存取消后的真实进程结果供 Runner 统一收尾。"""

    def __init__(self, limits: RunLimits, run_dir: Path) -> None:
        """绑定预算和本次运行目录；返回 None，构造时不启动验收或创建输出。

        limits 提供输出上限与清理宽限；run_dir 必须是 Runner 已创建的运行记录目录。
        """
        self._process = ProcessRunner(limits, run_dir / "artifacts")  # 与模型命令同契约，但使用独立进程实例。
        self.last_result: VerificationResult | None = None  # 初始 None；一次正常/取消执行收尾后保存真实详情。

    async def run(self, spec: VerifierSpec, workspace: Path, run_id: UUID) -> VerificationResult:
        """运行宿主固定的验收入口，返回验收状态和输出；取消仍原样向外传播。

        spec 包含预检后的 argv/cwd 与验收自身秒级时限；cwd=None 时取当前宿主目录。
        workspace 是任务绝对目录，run_id 是 UUID，分别通过固定 HARNESS 环境变量传入。
        spec.python_paths 只显式加入当前 cwd 内的源码目录；空列表不注入 Python 搜索路径。
        会生成本次验收输出文件，不继承模型密钥；StorageError 向外传播以停止调度。
        """
        self.last_result = None
        started = asyncio.get_running_loop().time()
        environment = {"HARNESS_WORKSPACE": str(workspace), "HARNESS_RUN_ID": str(run_id)}
        try:
            data = await self._process.run(
                spec.argv, spec.cwd if spec.cwd is not None else Path.cwd(),
                timeout_seconds=spec.timeout_seconds, extra_env=environment,
                python_paths=spec.python_paths,
            )
        except asyncio.CancelledError as error:
            data = getattr(error, "harness_process_result", None)
            elapsed = max(0, int((asyncio.get_running_loop().time() - started) * 1000))
            self.last_result = _map_result(data or {"cancelled": True, "cleanup_status": "unknown"}, elapsed)
            raise
        elapsed = max(0, int((asyncio.get_running_loop().time() - started) * 1000))
        self.last_result = _map_result(data, elapsed)
        return self.last_result
