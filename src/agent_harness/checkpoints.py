"""在完整工具批次边界封存恢复证据；读取和派生均不重放历史工具。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from .compression import encode, message_groups
from .context_archive import ContextArchive
from .schemas import DataModel, ProjectInstructionSnapshot, ResumeBudget
from .store import RunHistory, RunStore, StorageError
from .workspace import WorkspaceError
from .projects.manager import ProjectError, ProjectManager

if TYPE_CHECKING:
    from .runner import RunContext
    from .compression import ContextCompressor
    from .context import ContextBuilder
    from .projects.models import ProjectPreset, ProjectReview


class CheckpointError(Exception):
    """携带稳定恢复错误码和中文说明，禁止带入原始命令输出或密钥。"""

    def __init__(self, code: str, message: str) -> None:
        """固定code和可公开message，返回None，不修改运行记录。"""
        self.code = code  # API与页面可据此区分阻断条件。
        self.message = message  # 可直接展示的中文原因。
        super().__init__(message)


class Checkpoint(DataModel):
    """完整工具批次边界的不可变恢复证据；sha256不参与自身摘要。"""

    checkpoint_id: UUID  # 本运行内独立检查点标识。
    run_id: UUID  # 唯一来源运行。
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 其余字段规范JSON的摘要。
    created_at: datetime  # 封存时间，只用于展示，不用于计时。
    last_seq: int = Field(ge=1)  # 已封存有效事件前缀的最后序号。
    events_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 规范事件前缀摘要。
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 固定配置/任务/来源清单摘要。
    messages: list[dict]  # 完整且全部配对的原始消息，不重复追加用户要求。
    instruction: ProjectInstructionSnapshot  # 原始项目指令快照，恢复时不重读。
    conversation_start: int = Field(ge=1, le=2)  # 宿主与可选项目指令前缀长度。
    projection: dict  # 已生效的外置、遮蔽、摘要投影及估算状态。
    references: list[str]  # 仅此白名单中的外置证据可从中断来源回读。
    budget: ResumeBudget  # 封存时整条恢复链累计消耗。
    project_tree: str = Field(pattern=r"^[a-f0-9]{40,64}$")  # 原固定基线上的完整Git树。
    project_patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 独立binary patch摘要。
    tool_batch_count: int = Field(ge=0)  # 原始消息中完整工具批次数。


def digest(value: object) -> str:
    """返回value规范JSON的SHA256；仅支持无NaN的JSON值，不读写文件。"""
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def accumulated_budget(history: RunHistory) -> ResumeBudget:
    """从有效事件和可信终态计算恢复链累计预算；未知响应用量显式标不完整。

    history只读；未结束运行的最后在途模型请求保守预扣一次请求超时，不把停机时间
    当主动时长。model_turns按开始事件计，token只累计已返回的真实usage。
    """
    previous = history.result.resume_budget if history.result is not None else None
    if previous is None and history.manifest.resume_source is not None:
        # 中断的恢复尝试会在resume_loaded中固定继承消耗，不依赖终态缓存。
        loaded = [event for event in history.events if event.type == "resume_loaded"]
        if loaded:
            previous = ResumeBudget.model_validate(loaded[0].payload["budget"])
    previous = previous or ResumeBudget()
    values = previous.model_dump()
    requests = [event for event in history.events if event.type == "model_request_started"]
    responses = [event for event in history.events if event.type == "model_response"]
    values["model_turns"] += len(requests)
    values["http_attempts"] += sum(event.type == "model_http_attempt" for event in history.events)
    values["tool_calls"] += sum(event.type == "tool_started" for event in history.events)
    values["summary_calls"] += sum(event.payload.get("purpose") == "summary" for event in requests)
    complete = values["usage_complete"] and len(requests) == len(responses)
    for response in responses:
        usage = response.payload.get("usage")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name) if isinstance(usage, dict) else None
            if type(value) is int and value >= 0:
                values[name] += value
            else:
                complete = False
    if history.result is not None:
        # 注入测试客户端可能不发HTTP事件，终态计数仍由Runner事实优先。
        result = history.result
        values["model_turns"] = previous.model_turns + result.model_turns
        values["http_attempts"] = previous.http_attempts + result.http_attempts
        values["tool_calls"] = previous.tool_calls + result.tool_calls
        complete = complete and result.usage_complete
        duration = result.active_duration_seconds or (history.events[-1].elapsed_ms / 1000 if history.events else 0)
    else:
        duration = history.events[-1].elapsed_ms / 1000 if history.events else 0
        if len(requests) > len(responses):
            duration += history.manifest.config.limits.model_turn_timeout_seconds
    if any(event.type == "model_http_attempt" and event.payload.get("attempt", 0) > 1 for event in history.events):
        complete = False
    values["active_duration_seconds"] += duration
    values["usage_complete"] = complete
    return ResumeBudget.model_validate(values)


class CheckpointStore:
    """管理运行目录下有界不可变检查点；项目管理器只用于POST的强核验与派生。"""

    MAX_CHECKPOINTS = 64  # 每个尝试最多封存64份，超限不删除旧证据。
    MAX_TOTAL_BYTES = 128 * 1024 * 1024  # 每次尝试封存总上限128MiB，超限保留既有记录。
    MAX_BYTES = 16 * 1024 * 1024  # 单份消息/投影JSON上限16MiB。
    MAX_REFERENCES = 512  # 单份检查点最多授权512个确定ctx对象。
    _REFERENCES = re.compile(r"ctx:[a-f0-9-]{36}:[a-f0-9]{64}")  # 只识别内容寻址引用。

    def __init__(self, runs_root: Path, project_manager: ProjectManager | None = None) -> None:
        """绑定runs_root及可选项目管理器；None仅允许静态读取，不创建目录。"""
        self.runs_root = runs_root.resolve()  # 可信记录根目录。
        self.project_manager = project_manager  # Git捕获、核验与派生入口，可选。
        self._failed: set[UUID] = set()  # 捕获失败后该尝试不再反复写相同失败事件。

    def _folder(self, run_id: UUID) -> Path:
        """返回实体运行内checkpoints目录；链接、越界或目录替换抛CheckpointError。"""
        folder = self.runs_root / str(UUID(str(run_id))) / "checkpoints"
        for candidate in (self.runs_root, folder.parent, folder):
            if candidate.resolve() != candidate or candidate.is_symlink() or candidate.is_junction():
                raise CheckpointError("checkpoint_invalid", "检查点目录归属发生变化。")
        return folder

    def _read(self, path: Path, maximum: int) -> bytes:
        """有界读取实体普通文件，前后核验身份；缺失、损坏或链接统一抛CheckpointError。"""
        try:
            if path.resolve() != path or path.is_symlink() or path.is_junction():
                raise ValueError("path")
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError("size")
            with path.open("rb") as source:
                opened = os.fstat(source.fileno())
                raw = source.read(maximum + 1)
                after = os.fstat(source.fileno())
            identities = [(item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) for item in (before, opened, after, path.stat())]
            if len(set(identities)) != 1 or len(raw) > maximum or path.resolve() != path:
                raise ValueError("changed")
            return raw
        except (OSError, ValueError):
            raise CheckpointError("checkpoint_invalid", "检查点缺失、损坏或无法核对。") from None

    def _write(self, path: Path, raw: bytes) -> None:
        """排他创建并fsync完整raw，返回None；不会覆盖已存在证据，失败抛OSError。"""
        with path.open("xb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())

    def load(self, history: RunHistory, checkpoint_id: UUID) -> Checkpoint:
        """静态加载并核验来源、事件前缀、消息配对和独立补丁；无Git操作与副作用。"""
        try:
            folder = self._folder(history.manifest.run_id)
            checkpoint = Checkpoint.model_validate_json(self._read(folder / (str(checkpoint_id) + ".json"), self.MAX_BYTES))
            if checkpoint.run_id != history.manifest.run_id or checkpoint.checkpoint_id != checkpoint_id:
                raise ValueError("identity")
            if digest(checkpoint.model_dump(mode="json", exclude={"sha256"})) != checkpoint.sha256:
                raise ValueError("digest")
            if digest(history.manifest.model_dump(mode="json")) != checkpoint.manifest_sha256:
                raise ValueError("manifest")
            prefix = history.events[:checkpoint.last_seq]
            if len(prefix) != checkpoint.last_seq or digest([event.model_dump(mode="json") for event in prefix]) != checkpoint.events_sha256:
                raise ValueError("prefix")
            published = [event for event in history.events if event.type == "checkpoint_created" and event.payload.get("checkpoint_id") == str(checkpoint_id)]
            if len(published) != 1 or published[0].seq != checkpoint.last_seq + 1 or published[0].payload.get("sha256") != checkpoint.sha256:
                raise ValueError("unpublished")
            message_groups(checkpoint.messages)
            starts = [event for event in prefix if event.type == "run_started"]
            if len(starts) != 1:
                raise ValueError("messages")
            reconstructed = deepcopy(starts[0].payload["messages"])
            for event in prefix:
                if event.type == "assistant_message":
                    reconstructed.append(event.payload["message"])
                elif event.type == "tool_result":
                    reconstructed.append({"role": "tool", "tool_call_id": event.payload["call_id"],
                                          "content": json.dumps(event.payload["result"], ensure_ascii=False, allow_nan=False)})
            if reconstructed != checkpoint.messages or checkpoint.conversation_start != starts[0].payload["conversation_start"]:
                raise ValueError("messages changed")
            if len(checkpoint.references) > self.MAX_REFERENCES or len(set(checkpoint.references)) != len(checkpoint.references):
                raise ValueError("references")
            self.patch(checkpoint)
            return checkpoint
        except CheckpointError:
            raise
        except (ValueError, TypeError, KeyError, ValidationError):
            raise CheckpointError("checkpoint_invalid", "检查点来源、消息或摘要校验失败。") from None

    def patch(self, checkpoint: Checkpoint) -> bytes:
        """读取checkpoint对应独立补丁并核验SHA，返回完整字节；不使用审阅changes.patch。"""
        path = self._folder(checkpoint.run_id) / (str(checkpoint.checkpoint_id) + ".patch")
        raw = self._read(path, ProjectManager.MAX_PATCH_BYTES)
        if hashlib.sha256(raw).hexdigest() != checkpoint.project_patch_sha256:
            raise CheckpointError("checkpoint_invalid", "检查点代码补丁校验失败。")
        return raw

    def latest(self, history: RunHistory) -> Checkpoint | None:
        """返回最后已发布检查点；无发布事件返回None，已发布证据损坏明确报错，不倒退。"""
        events = [event for event in history.events if event.type == "checkpoint_created"]
        if not events:
            return None
        try:
            return self.load(history, UUID(events[-1].payload["checkpoint_id"]))
        except (ValueError, KeyError, TypeError):
            raise CheckpointError("checkpoint_invalid", "检查点发布记录损坏。") from None

    def check_readiness(self, history: RunHistory, checkpoint: Checkpoint, check_workspace: bool = False) -> Checkpoint:
        """检查中断来源、封存后风险及累计额度；默认纯静态，True额外核验Git现场。

        返回budget包含封存后实际模型请求的深拷贝；sha仍指原封存证据。
        任何工具、验收或完成事实出现在封存后都拒绝，禁止盲目回退副作用。
        """
        origin = history.manifest.origin
        if origin is None or origin.kind != "project":
            raise CheckpointError("checkpoint_project_required", "当前只支持Git代码任务恢复。")
        if history.warnings or (history.result is not None and history.result.cleanup_status not in {"not_needed", "complete"}):
            raise CheckpointError("checkpoint_source_uncertain", "运行记录或进程清理状态无法确认。")
        if history.result is not None and history.result.execution_status == "completed":
            raise CheckpointError("checkpoint_task_completed", "任务已经完成，可发送追加要求。")
        tail = history.events[checkpoint.last_seq:]
        forbidden = {"tool_started", "tool_result", "verification_started", "verification_result", "execution_completed"}
        if any(event.type in forbidden for event in tail):
            raise CheckpointError("checkpoint_side_effect_uncertain", "检查点之后已有工具或验收行为，不能自动重放，请人工核对。")
        budget = accumulated_budget(history)
        limits = history.manifest.config.limits
        if budget.model_turns >= limits.max_model_turns or budget.tool_calls >= limits.max_tool_calls or budget.active_duration_seconds >= limits.run_timeout_seconds:
            raise CheckpointError("checkpoint_budget_exhausted", "原任务累计预算已经耗尽，不能通过恢复重置额度。")
        if check_workspace:
            if self.project_manager is None:
                raise CheckpointError("checkpoint_project_unavailable", "恢复项目管理器尚未配置。")
            try:
                ContextArchive(self.runs_root / str(checkpoint.run_id), history.manifest).bind_checkpoint_references(checkpoint.references)
                self.project_manager.verify_checkpoint(checkpoint.run_id, checkpoint.project_tree)
            except (ProjectError, WorkspaceError) as error:
                raise CheckpointError(error.code, error.message) from None
        return checkpoint.model_copy(update={"budget": budget}, deep=True)

    def verify(self, history: RunHistory, checkpoint_id: UUID | None = None) -> Checkpoint:
        """为显式恢复执行完整核验；None选择最新检查点，返回含tail累计预算的证据副本。"""
        checkpoint = self.latest(history) if checkpoint_id is None else self.load(history, checkpoint_id)
        if checkpoint is None:
            raise CheckpointError("checkpoint_missing", "该任务尚无完整检查点。")
        return self.check_readiness(history, checkpoint, check_workspace=True)

    def prepare_resume(self, preset: ProjectPreset, source_history: RunHistory, checkpoint: Checkpoint,
                       new_run_id: UUID) -> ProjectReview:
        """强核验后从独立补丁派生new_run_id工作树，不改变源记录或审阅补丁。"""
        verified = self.verify(source_history, checkpoint.checkpoint_id)
        if verified.sha256 != checkpoint.sha256 or self.project_manager is None:
            raise CheckpointError("checkpoint_invalid", "恢复来源发生变化。")
        try:
            return self.project_manager.prepare_checkpoint(preset, checkpoint.run_id, new_run_id,
                                                          checkpoint.project_tree, self.patch(checkpoint))
        except ProjectError as error:
            raise CheckpointError(error.code, error.message) from None

    def capture(self, context: RunContext, messages: list[dict], compressor: ContextCompressor,
                builder: ContextBuilder) -> Checkpoint | None:
        """在完整工具边界同步封存证据；调用方在线程运行，Runner随后单写发布事件。

        非Git、清理不可信或本尝试先前捕获失败返回None；可预期捕获失败交由调用方记录。
        此方法只写不可变文件，绝不直接追加JSONL，避免与取消收尾竞争。
        """
        origin = context.manifest.origin
        if origin is None or origin.kind != "project" or self.project_manager is None:
            return None
        if context.result.cleanup_status not in {"not_needed", "complete"} or context.manifest.run_id in self._failed:
            return None
        try:
            folder = self._folder(context.manifest.run_id)
            folder.mkdir(exist_ok=True)
            history = RunStore.read(self.runs_root, str(context.manifest.run_id))
            if sum(event.type == "checkpoint_created" for event in history.events) >= self.MAX_CHECKPOINTS:
                raise CheckpointError("checkpoint_limit", "本次尝试的检查点数量已达到64份上限。")
            groups = message_groups(messages)
            projection = compressor.export_state()
            references = sorted(set(self._REFERENCES.findall(encode(projection) + encode(messages))))
            if len(references) > self.MAX_REFERENCES:
                raise CheckpointError("checkpoint_reference_limit", "检查点外置引用超过上限。")
            # 回读原文可能嵌套更早引用；只沿封存时已获授权的对象有界闭包，不扩大祖先权限。
            checked = set()
            while set(references) - checked:
                reference = next(iter(set(references) - checked))
                raw_reference = compressor.archive.load(reference)
                checked.add(reference)
                references = sorted(set(references) | set(self._REFERENCES.findall(raw_reference.decode("utf-8", errors="replace"))))
                if len(references) > self.MAX_REFERENCES:
                    raise CheckpointError("checkpoint_reference_limit", "检查点外置引用超过上限。")
            tree, patch = self.project_manager.checkpoint_snapshot(context.manifest.run_id)
            budget = accumulated_budget(history)
            budget.active_duration_seconds = (context.result.resume_budget.active_duration_seconds if context.result.resume_budget else 0) + context.active_duration()
            starts = [event for event in history.events if event.type == "run_started"]
            data = {
                "checkpoint_id": str(uuid4()), "run_id": str(context.manifest.run_id),
                "created_at": datetime.now(UTC).isoformat(), "last_seq": history.events[-1].seq,
                "events_sha256": digest([event.model_dump(mode="json") for event in history.events]),
                "manifest_sha256": digest(context.manifest.model_dump(mode="json")),
                "messages": deepcopy(messages), "instruction": builder.instruction.model_dump(mode="json"),
                "conversation_start": starts[0].payload["conversation_start"], "projection": projection,
                "references": references, "budget": budget.model_dump(mode="json"),
                "project_tree": tree, "project_patch_sha256": hashlib.sha256(patch).hexdigest(),
                "tool_batch_count": sum(bool(group.messages[0].get("tool_calls")) for group in groups),
            }
            checkpoint = Checkpoint.model_validate_json(encode({**data, "sha256": digest(data)}))
            # 用模型规范化后的UTC/字段默认值再次计算，保证重新读取时摘要稳定。
            checkpoint.sha256 = digest(checkpoint.model_dump(mode="json", exclude={"sha256"}))
            raw = checkpoint.model_dump_json().encode("utf-8")
            if len(raw) > self.MAX_BYTES:
                raise CheckpointError("checkpoint_limit", "检查点消息超过16MiB封存上限。")
            total = sum(path.stat().st_size for path in folder.iterdir() if path.is_file())
            if total + len(raw) + len(patch) > self.MAX_TOTAL_BYTES:
                raise CheckpointError("checkpoint_limit", "本次尝试检查点总大小达到128MiB上限。")
            self._write(folder / (str(checkpoint.checkpoint_id) + ".patch"), patch)
            self._write(folder / (str(checkpoint.checkpoint_id) + ".json"), raw)
            return checkpoint
        except (ProjectError, WorkspaceError, OSError, ValueError, StorageError) as error:
            self._failed.add(context.manifest.run_id)
            if isinstance(error, (ProjectError, WorkspaceError)):
                raise CheckpointError(error.code, error.message) from None
            raise CheckpointError("checkpoint_capture_failed", "检查点保存失败，本次任务继续执行，恢复不可用。") from None
        except CheckpointError:
            self._failed.add(context.manifest.run_id)
            raise


def load_resume(runs_root: Path, manifest) -> tuple[Checkpoint, RunHistory]:
    """核验manifest固定恢复来源与累计预算，返回检查点和源历史；不操作Git或执行工具。"""
    source = manifest.resume_source
    if source is None:
        raise CheckpointError("checkpoint_missing", "没有指定恢复来源。")
    history = RunStore.read(runs_root, str(source.source_run_id))
    store = CheckpointStore(runs_root)
    checkpoint = store.load(history, source.checkpoint_id)
    if checkpoint.sha256 != source.checkpoint_sha256 or manifest.config != history.manifest.config:
        raise CheckpointError("checkpoint_source_changed", "恢复来源或固定配置发生变化。")
    if manifest.origin != history.manifest.origin:
        raise CheckpointError("checkpoint_source_changed", "恢复项目来源发生变化。")
    if manifest.task.prompt != history.manifest.task.prompt or manifest.turn_index != history.manifest.turn_index:
        raise CheckpointError("checkpoint_source_changed", "恢复必须继续原用户要求和原轮次。")
    if manifest.parent_run_id != source.source_run_id or manifest.session_id != (history.manifest.session_id or history.manifest.run_id):
        raise CheckpointError("checkpoint_source_changed", "恢复会话关系无法核对。")
    if manifest.attempt_index != history.manifest.attempt_index + 1:
        raise CheckpointError("checkpoint_source_changed", "恢复尝试序号无效。")
    return store.check_readiness(history, checkpoint), history
