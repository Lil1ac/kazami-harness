"""CLI 启动前校验、真实执行与只读历史查看。"""

import argparse
import asyncio
import json
import os
import signal
import threading
import sys
from pathlib import Path
from uuid import uuid4

from agent_harness import __version__
from agent_harness.config import ConfigurationError, load_config, prepare_task
from agent_harness.schemas import RunManifest, RunResult
from agent_harness.runner import CancellationState, RunOutcome, exit_code, run_task, validate_output_paths
from agent_harness.store import RunStore, StorageError


def _parser() -> argparse.ArgumentParser:
    """定义 run/show 子命令和参数约束，交给 argparse 处理帮助信息与格式错误。"""
    parser = argparse.ArgumentParser(prog="harness", description="Agent Harness 任务运行入口")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="执行 DeepSeek 文件与命令任务，或仅准备记录")
    task = run.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="任务描述，建议写明工作区内文件名")
    task.add_argument("--task-file", type=Path, help="UTF-8 任务描述文件")
    run.add_argument("--workspace", type=Path, required=True, help="已存在的任务工作目录")
    run.add_argument("--config", type=Path, help="运行配置 TOML")
    run.add_argument("--verifier", type=Path, help="宿主侧验收清单（当前仅支持 prepare-only 预检）")
    run.add_argument("--runs-dir", type=Path, default=Path(".runs"), help="运行记录目录")
    run.add_argument("--expected-output", action="append", default=[], help="登记工作区内预期产物的相对路径，可多次指定")
    run.add_argument("--model", help="覆盖 requested_model")
    run.add_argument("--max-model-turns", type=int, help="覆盖模型轮次上限")
    run.add_argument("--prepare-only", action="store_true", help="只校验并创建记录，不调用模型或工具")
    run.add_argument("--json", action="store_true", help="输出 JSON，便于脚本读取")

    show = commands.add_parser("show", help="只读查看历史运行")
    show.add_argument("run_id", help="UUID 格式的运行标识")
    show.add_argument("--runs-dir", type=Path, default=Path(".runs"))
    show.add_argument("--json", action="store_true")
    return parser


def _prompt(args: argparse.Namespace) -> str:
    """取得直接输入或 UTF-8 文件中的任务文本；读取失败时返回受控配置错误。"""
    if args.task is not None:
        return args.task
    try:
        return args.task_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("任务文件必须是可读取的 UTF-8 文件") from exc


def _print_result(data: dict, *, as_json: bool, prepared: bool = False) -> None:
    """按 JSON 或中文摘要展示结果，并说明未验收、未完整结束和记录损坏等状态。"""
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if prepared:
        print("准备完成：尚未调用模型、执行工具或验收任务。")
    print(f"运行 ID：{data['run_id']}")
    print(f"执行状态：{data['execution_status']}")
    print(f"验收状态：{data['validation_status']}")
    print(f"清理状态：{data['cleanup_status']}")
    print(f"模型轮次：{data['model_turns']}；HTTP 尝试：{data['http_attempts']}；工具调用：{data['tool_calls']}")
    print(f"事件数量：{data['event_count']}")
    print(f"记录目录：{data['run_dir']}")
    if data.get("stop_reason"):
        print(f"结束原因：{data['stop_reason']}")
    if data.get("stop_reason") == "unexpected_cancellation":
        print("提示：执行收到内部取消异常，来源位置已写入 cancellation_received 事件。")
    elif data.get("stop_reason") == "task_cancelled":
        print("提示：任务收到了取消请求，未检测到用户 Ctrl+C；来源位置已记录。")
    for artifact in data.get("artifacts", []):
        print(f"产物：{artifact['location']} / {artifact['path']}")
    for missing in data.get("missing_artifacts", []):
        print(f"产物提示：预期文件尚未生成：{missing}")
    if data.get("final_answer"):
        print("\n模型回答：")
        print(data["final_answer"])
    if data["execution_status"] == "completed" and data["validation_status"] == "unverified":
        print("提示：执行已结束，尚未通过独立验收；本次执行命令返回码为 3。")
    if data.get("interrupted"):
        print("提示：记录包含开始事件但缺少结束事件，可能仍在运行或发生了中断。")
    for warning in data.get("warnings", []):
        print(f"记录警告：{warning}")


async def _execute_with_signals(manifest: RunManifest, runs_dir: Path, api_key: str) -> RunOutcome:
    """标记真实 SIGINT 并保留 asyncio.run 的标准取消行为，结束后恢复原处理器。"""
    state = CancellationState()
    previous = signal.getsignal(signal.SIGINT)

    def on_interrupt(signum, frame):
        """记下用户中断证据，再由 asyncio 原处理器取消主任务或处理第二次中断。"""
        state.user_requested = True
        previous(signum, frame)

    # asyncio.run 在主线程已安装自己的 SIGINT 处理器；工作线程和忽略信号时不接管。
    installed = threading.current_thread() is threading.main_thread() and callable(previous)
    if installed:
        signal.signal(signal.SIGINT, on_interrupt)
    try:
        return await run_task(manifest, runs_dir, api_key, cancellation_state=state)
    finally:
        if installed and signal.getsignal(signal.SIGINT) is on_interrupt:
            signal.signal(signal.SIGINT, previous)


def _run(args: argparse.Namespace) -> int:
    """校验输入并创建运行清单，再按参数选择仅准备或执行任务。

    准备模式不读取密钥；执行模式把模型与工具循环交给异步 Runner。
    返回准备操作或真实执行的退出码，执行结束但未验收时返回 3。
    """
    overrides: dict = {}
    if args.model is not None:
        overrides["requested_model"] = args.model
    if args.max_model_turns is not None:
        overrides["limits"] = {"max_model_turns": args.max_model_turns}
    config = load_config(args.config, overrides)
    task = prepare_task(_prompt(args), args.workspace, args.verifier, config.limits.verifier_timeout_seconds, args.expected_output)
    validate_output_paths(task, args.runs_dir)
    manifest = RunManifest(run_id=uuid4(), task=task, config=config)
    if args.prepare_only:
        store = RunStore.create(args.runs_dir, manifest)
        result = RunResult(run_id=manifest.run_id)
        run_dir, event_count = store.run_dir, 1
    else:
        # 密钥值只交给请求适配器；配置快照与事件中仅记录环境变量名称。
        api_key = os.environ.get(config.api_key_env, "")
        outcome = asyncio.run(_execute_with_signals(manifest, args.runs_dir, api_key))
        result = outcome.result
        run_dir, event_count = outcome.run_dir, outcome.event_count
    _print_result(
        result.model_dump(mode="json")
        | {"run_dir": str(run_dir), "event_count": event_count, "interrupted": False, "warnings": []},
        as_json=args.json,
        prepared=args.prepare_only,
    )
    # 准备流程的 0 只表示准备成功；真实执行未验收时返回 3。
    return 0 if args.prepare_only else exit_code(result)


def _show(args: argparse.Namespace) -> int:
    """只读展示指定运行；返回 0 表示读取完成，不代表历史任务执行成功。"""
    history = RunStore.read(args.runs_dir, args.run_id)
    # 缺少终态时仅展示记录能够支持的状态，不推断旧进程是否还在运行。
    result = history.result or RunResult(
        run_id=history.manifest.run_id,
        execution_status="running" if history.interrupted else "created",
    )
    _print_result(
        result.model_dump(mode="json")
        | {
            "run_dir": str(args.runs_dir.resolve() / str(history.manifest.run_id)),
            "event_count": len(history.events),
            "interrupted": history.interrupted,
            "warnings": history.warnings,
            "task": history.manifest.task.model_dump(mode="json"),
            "config": history.manifest.config.model_dump(mode="json"),
        },
        as_json=args.json,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析命令并分派操作，将配置、存储错误和用户中断转换为 CLI 退出码。"""
    # Windows 管道和不同终端编码下也保持中文 JSON 可读取。
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        return _run(args) if args.command == "run" else _show(args)
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except StorageError as exc:
        print(f"记录错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
