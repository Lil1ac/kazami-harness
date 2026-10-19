"""单写入者运行记录：事件日志是事实来源，summary 仅作为读取缓存。"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid4

from .schemas import RunEvent, RunManifest, RunResult


class StorageError(Exception):
    """存储边界统一向调用者暴露的异常。"""


@dataclass
class RunHistory:
    """只读历史视图，包含登记清单、有效事件前缀、可信终态和读取警告。"""

    manifest: RunManifest
    events: list[RunEvent]
    result: RunResult | None
    warnings: list[str] = field(default_factory=list)
    interrupted: bool = False


_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out", "limit_exceeded"}


def _contained(path: Path, directory: Path) -> Path:
    """检查解析后的路径仍在指定目录内，通过时返回原路径，越界则报存储错误。"""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise StorageError(f"无法解析运行记录路径（{type(exc).__name__}）") from exc
    if not resolved.is_relative_to(directory):
        raise StorageError("路径越出运行记录目录")
    return path


def _write_json(path: Path, value: dict) -> None:
    """排他创建 UTF-8 JSON 文件并刷新到磁盘；目标已存在时拒绝覆盖。"""
    # 排他创建防止覆盖已有记录；关键文件落盘后才向调用者报告成功。
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


class RunStore:
    """管理单次运行的追加日志和派生摘要；设计为单写入者，不负责重新执行动作。"""

    def __init__(self, run_dir: Path, manifest: RunManifest) -> None:
        """保存独立清单副本并初始化序号、耗时基准和写入状态；不创建文件。"""
        self.run_dir = run_dir
        self._manifest = manifest.model_copy(deep=True)
        self._started_ns = time.monotonic_ns()
        self._seq = 0
        self._finalized = False
        self._broken = False

    @classmethod
    def create(cls, root: Path, manifest: RunManifest) -> "RunStore":
        """新建 UUID 运行目录，保存清单并提交首条 run_created 事件。"""
        try:
            root = root.resolve()
            root.mkdir(parents=True, exist_ok=True)
            run_dir = _contained(root / str(manifest.run_id), root)
            run_dir.mkdir()  # 不复用已有 UUID，包含残留的部分记录。
            _write_json(run_dir / "run.json", manifest.model_dump(mode="json"))
            (run_dir / "events.jsonl").touch(exist_ok=False)
            store = cls(run_dir, manifest)
            store._append("run_created", {})
            return store
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise StorageError(f"创建运行记录失败（{type(exc).__name__}）") from exc

    def append(self, event_type: str, payload: dict | None = None) -> RunEvent:
        """追加普通事件并返回已提交对象；创建和结束事件只能由专用入口写入。"""
        if event_type in {"run_created", "run_finished"}:
            raise StorageError("创建和结束事件只能由 create/finalize 写入")
        return self._append(event_type, {} if payload is None else payload)

    def _append(self, event_type: str, payload: dict) -> RunEvent:
        """分配下一序号并同步提交一条 JSONL 事件，写入故障后禁止继续追加。"""
        if self._finalized or self._broken:
            raise StorageError("运行已结束或写入失败，不能继续追加事件")
        try:
            event = RunEvent(
                run_id=self._manifest.run_id,
                seq=self._seq + 1,
                elapsed_ms=(time.monotonic_ns() - self._started_ns) // 1_000_000,
                type=event_type,
                payload=payload,
            )
            path = _contained(self.run_dir / "events.jsonl", self.run_dir)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                # 一条事件只写一次；写失败后禁止继续，保留可诊断的有效前缀。
                handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
                handle.flush()
                # 单写入者首版优先保证提交顺序，后续工具开始事件也必须先落盘。
                os.fsync(handle.fileno())
            self._seq = event.seq
            return event
        except (OSError, ValueError, TypeError) as exc:
            self._broken = True
            raise StorageError(f"events.jsonl 追加失败（{type(exc).__name__}）") from exc

    def finalize(self, result: RunResult) -> None:
        """仅提交一次匹配本次运行的终态，再以临时文件替换方式生成摘要。

        run_finished 是结果的事实来源，summary.json 只是派生缓存；即使缓存写入
        失败，日志中的终态仍有效，调用者不能通过再次 finalize 重复提交结束事件。
        """
        if self._finalized or self._broken:
            raise StorageError("运行已结束或写入失败，不能再次结束")
        if result.run_id != self._manifest.run_id:
            raise StorageError("结果的 run_id 与运行记录不一致")
        if result.execution_status not in _TERMINAL_STATUSES:
            raise StorageError("只能以终态结束运行")
        self._append("run_finished", {"result": result.model_dump(mode="json")})
        # 终态先记入日志；即使缓存写失败，也不能产生第二个结束事件。
        self._finalized = True
        temporary = self.run_dir / f".summary-{uuid4()}.tmp"
        try:
            _write_json(_contained(temporary, self.run_dir), result.model_dump(mode="json"))
            destination = _contained(self.run_dir / "summary.json", self.run_dir)
            os.replace(temporary, destination)
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError(f"结果已保存到日志，但 summary.json 写入失败（{type(exc).__name__}）") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 临时缓存残留不影响已经提交的日志，也不覆盖原始存储异常。
                pass

    @classmethod
    def read(cls, root: Path, run_id: str) -> RunHistory:
        """按 UUID 只读加载运行，从连续有效事件中恢复最终结果。

        首个损坏事件之后的内容不再采用，已有有效前缀保留并附警告。
        只通过日志判断是否缺少结束事件，不改写文件，也不恢复外部执行动作。
        """
        location = "run_id"
        try:
            # 仅由 UUID 生成子目录名，拒绝把调用者输入当成相对路径。
            identifier = UUID(run_id)
            root = root.resolve()
            run_dir = _contained(root / str(identifier), root)
            location = "run.json"
            manifest_path = _contained(run_dir / "run.json", run_dir.resolve())
            manifest = RunManifest.model_validate_json(manifest_path.read_bytes())
            if manifest.run_id != identifier:
                raise StorageError("运行目录与清单中的 run_id 不一致")
            location = "events.jsonl"
            events_path = _contained(run_dir / "events.jsonl", run_dir.resolve())
            lines = events_path.read_bytes().splitlines(keepends=True)
            history = RunHistory(manifest=manifest, events=[], result=None)
            for number, line in enumerate(lines, start=1):
                try:
                    event = RunEvent.model_validate_json(line.decode("utf-8"))
                    cls._check_event(history, event)
                    if event.type == "run_finished":
                        result = RunResult.model_validate_json(
                            json.dumps(event.payload["result"], ensure_ascii=False)
                        )
                        if result.run_id != identifier or result.execution_status not in _TERMINAL_STATUSES:
                            raise ValueError("结束事件的结果无效")
                        history.result = result
                    history.events.append(event)
                    if not line.endswith(b"\n"):
                        history.warnings.append(f"第 {number} 行缺少换行，已保留有效事件")
                except (ValueError, TypeError, KeyError) as exc:
                    # 不跳过坏行：后续事件可能依赖缺失的动作，只返回可靠前缀。
                    # 校验异常会携带 input_value；只记录位置与类别，避免回显敏感正文。
                    history.warnings.append(
                        f"events.jsonl 第 {number} 行事件损坏，已停止读取（{type(exc).__name__}）"
                    )
                    break
            history.interrupted = history.result is None and any(
                event.type == "run_started" for event in history.events
            )
            cls._check_summary(run_dir, history)
            return history
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise StorageError(f"{location} 读取失败（{type(exc).__name__}）") from exc

    @staticmethod
    def _check_event(history: RunHistory, event: RunEvent) -> None:
        """检查事件归属、连续序号、创建与结束顺序，以及累计耗时是否倒退。"""
        if event.run_id != history.manifest.run_id or event.seq != len(history.events) + 1:
            raise ValueError("事件的 run_id 或序号不连续")
        if history.result is not None:
            raise ValueError("结束事件之后出现额外事件")
        if not history.events and event.type != "run_created":
            raise ValueError("首条事件必须是 run_created")
        if history.events and event.type == "run_created":
            raise ValueError("重复的 run_created 事件")
        if history.events and event.elapsed_ms < history.events[-1].elapsed_ms:
            raise ValueError("事件累计耗时倒退")

    @staticmethod
    def _check_summary(run_dir: Path, history: RunHistory) -> None:
        """对照日志检查可选摘要，只追加警告，绝不让摘要覆盖日志恢复的结果。"""
        path = _contained(run_dir / "summary.json", run_dir.resolve())
        if not path.exists():
            return
        try:
            summary = RunResult.model_validate_json(path.read_bytes())
            if summary != history.result:
                history.warnings.append("summary.json 与事件日志不一致，已忽略摘要")
        except (OSError, ValueError, TypeError) as exc:
            history.warnings.append(f"summary.json 不可用，已忽略摘要（{type(exc).__name__}）")
