"""保存模型输入外置内容，按不可变引用有界回读；不向模型开放运行目录路径。"""

import codecs
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from pydantic import Field

from .schemas import ArtifactRef, DataModel, RunManifest
from .store import RunStore, StorageError
from .workspace import WorkspaceError


_REFERENCE = re.compile(r"^ctx:([a-f0-9-]{36}):([a-f0-9]{64})$")


# 参数只允许内容引用，不接受磁盘路径；分页单位为字节。
class ReadContextArguments(DataModel):
    reference: str = Field(pattern=r"^ctx:[a-f0-9-]{36}:[a-f0-9]{64}$", description="外置结果或历史摘要提供的 ctx 引用。")
    offset: int = Field(default=0, ge=0, description="UTF-8 字节偏移，首次为0，后续使用 next_offset。")
    max_bytes: int = Field(default=4096, ge=64, le=16384, description="本次最多读取64至16384字节，默认4096。")


class ContextArchive:
    """管理内容寻址证据；只读工具限于本运行和经过线性父链核验的祖先。"""

    MAX_OBJECT_BYTES = 8 * 1024 * 1024  # 单对象上限8MiB，超过时保留原输入而不外置。
    MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 本运行新增对象总上限64MiB。
    MAX_ANCESTORS = 256  # 回读祖先时最多核对256个父关系，拒绝循环和无界访问。

    def __init__(self, run_dir: Path, manifest: RunManifest) -> None:
        """绑定已创建的 run_dir 和当前 manifest；不创建对象、不启动外部执行。"""
        self.run_dir = run_dir.resolve(strict=True)  # Runner已创建的实际运行目录。
        self.runs_root = self.run_dir.parent  # 固定的记录根目录。
        self.manifest = manifest.model_copy(deep=True)  # 固定会话身份及直接父关系。
        self.total_bytes = 0  # 本运行新写对象的累计字节数。
        self._checkpoint_references: set[str] = set()  # 仅封存白名单可从中断来源回读，不缓存为整owner授权。
        self._authorized = {str(manifest.run_id)}  # 已核验归属的运行编号缓存。

    def bind_checkpoint_references(self, references: list[str]) -> None:
        """绑定已核验检查点的精确引用白名单；逐个检查SHA，失败不发布部分授权。

        references仅允许宿主从检查点读取，最多512项；不接受模型自选运行或文件路径。
        返回None，不执行工具、不修改任何祖先记录。
        """
        verified = set()
        if len(references) > 512:
            raise WorkspaceError("context_reference_forbidden", "检查点历史引用超过上限。")
        for reference in references:
            match = _REFERENCE.fullmatch(reference)
            if match is None:
                raise WorkspaceError("context_reference_invalid", "检查点历史引用无效。")
            owner, digest = match.groups()
            self._read_raw(str(UUID(owner)), digest)
            verified.add(reference)
        self._checkpoint_references = verified

    def _authorize_reference(self, reference: str, owner: str) -> None:
        """允许精确封存reference或正常可信父链owner；禁止将中断来源整目录加入缓存。"""
        if reference in self._checkpoint_references:
            return
        try:
            self._authorize(owner)
            return
        except WorkspaceError:
            pass
        # 已完成的恢复父轮可传递当时固定白名单，不能将中断祖先整体视为可信。
        parent = self.manifest.parent_run_id
        session = self.manifest.session_id or self.manifest.run_id
        visited = {str(self.manifest.run_id)}
        try:
            from .checkpoints import CheckpointError, CheckpointStore
            for _ in range(self.MAX_ANCESTORS):
                if parent is None or str(parent) in visited:
                    break
                identifier = str(parent)
                visited.add(identifier)
                history = RunStore.read(self.runs_root, identifier)
                if (history.manifest.session_id or history.manifest.run_id) != session or history.result is None or history.warnings or history.interrupted:
                    break
                source = history.manifest.resume_source
                if source is not None:
                    source_history = RunStore.read(self.runs_root, str(source.source_run_id))
                    checkpoint = CheckpointStore(self.runs_root).load(source_history, source.checkpoint_id)
                    if checkpoint.sha256 != source.checkpoint_sha256:
                        break
                    if reference in checkpoint.references:
                        return
                parent = history.manifest.parent_run_id
        except (StorageError, CheckpointError):
            pass
        raise WorkspaceError("context_reference_forbidden", "只能读取本运行、可信前轮或已封存的精确历史证据。")

    def _path(self, owner: str, digest: str) -> Path:
        """生成固定对象路径；owner/digest 必须来自合法引用，链接和越界抛 WorkspaceError。"""
        run = self.runs_root / owner
        folder = run / "context"
        path = folder / (digest + ".txt")
        for candidate in (run, folder, path):
            if candidate.resolve() != candidate or candidate.is_symlink():
                raise WorkspaceError("context_reference_changed", "历史证据路径发生变化。")
        return path

    def save(self, raw: str | bytes) -> str | None:
        """不可变保存 raw，返回内容引用；超过8MiB/累计64MiB返回None，写入失败抛StorageError。"""
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(data) > self.MAX_OBJECT_BYTES:
            return None
        digest = hashlib.sha256(data).hexdigest()
        owner = str(self.manifest.run_id)
        try:
            path = self._path(owner, digest)
            if path.exists():
                if self._read_raw(owner, digest) != data:
                    raise ValueError("digest mismatch")
            else:
                if self.total_bytes + len(data) > self.MAX_TOTAL_BYTES:
                    return None
                path.parent.mkdir(exist_ok=True)
                path = self._path(owner, digest)
                # 排他创建，不覆盖既有证据；引用只会在完整写入并fsync后发布。
                with path.open("xb") as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                self.total_bytes += len(data)
            return f"ctx:{owner}:{digest}"
        except (OSError, ValueError, WorkspaceError):
            raise StorageError("无法保存上下文证据，未发布外置引用。") from None

    def save_artifact(self, owner: str, relative: str) -> str | None:
        """将已捕获的stdout/stderr有界归档；owner须属当前父链，relative限artifacts内。

        返回当前运行引用；来源缺失、变化、不是普通文件或容量不足时返回None，
        调用方必须展示不可用，不将已有预览称为完整输出。不执行命令或读取任意路径。
        """
        try:
            owner = str(UUID(owner))
            self._authorize(owner)
            ArtifactRef(location="run", path=relative)
            root = self.runs_root / owner / "artifacts"
            path = self.runs_root / owner / relative
            if root.resolve() != root or path.resolve() != path or not path.is_relative_to(root):
                return None
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > self.MAX_OBJECT_BYTES:
                return None
            with path.open("rb") as source:
                opened = os.fstat(source.fileno())
                raw = source.read(self.MAX_OBJECT_BYTES + 1)
                after = os.fstat(source.fileno())
            values = [(item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) for item in (before, opened, after, path.stat())]
            if len(set(values)) != 1 or len(raw) > self.MAX_OBJECT_BYTES or path.resolve() != path:
                return None
            return self.save(raw)
        except (OSError, ValueError, WorkspaceError):
            return None

    def _authorize(self, owner: str) -> None:
        """核对 owner 是本运行的可信祖先；仅沿manifest父链读取，其他会话或缺失记录抛受控错误。"""
        if owner in self._authorized:
            return
        parent = self.manifest.parent_run_id
        child_manifest = self.manifest  # 恢复边界不能把来源整个owner提升为可信祖先。
        session = self.manifest.session_id or self.manifest.run_id
        visited = {str(self.manifest.run_id)}
        try:
            for _ in range(self.MAX_ANCESTORS):
                if parent is None or str(parent) in visited:
                    break
                identifier = str(parent)
                if child_manifest.resume_source is not None and identifier == str(child_manifest.resume_source.source_run_id):
                    break
                visited.add(identifier)
                history = RunStore.read(self.runs_root, identifier)
                parent_session = history.manifest.session_id or history.manifest.run_id
                if parent_session != session or history.warnings:
                    break
                if history.result is None or history.interrupted:
                    from .sessions import SessionError, history_messages, payload_digest
                    source = child_manifest.history_source
                    try:
                        if source is None or not source.interrupted or not history.events or history.events[-1].seq != source.last_seq:
                            break
                        if payload_digest(history_messages(history, allow_interrupted=True)) != source.messages_sha256:
                            break
                    except SessionError:
                        break
                self._authorized.add(identifier)
                if identifier == owner:
                    return
                child_manifest = history.manifest
                parent = history.manifest.parent_run_id
        except StorageError:
            pass
        raise WorkspaceError("context_reference_forbidden", "只能读取本运行及可信前轮提供的历史证据。")

    def _read_raw(self, owner: str, digest: str) -> bytes:
        """读取有界原始对象并核验摘要和身份；缺失、替换、特殊文件或损坏抛受控错误。"""
        try:
            path = self._path(owner, digest)
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > self.MAX_OBJECT_BYTES:
                raise ValueError("object type or size")
            with path.open("rb") as source:
                opened = os.fstat(source.fileno())
                data = source.read(self.MAX_OBJECT_BYTES + 1)
                after = os.fstat(source.fileno())
            # 防止读取期间替换为其他对象；最终摘要还验证内容。
            identities = [(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) for value in (before, opened, after, path.stat())]
            if len(set(identities)) != 1 or len(data) > self.MAX_OBJECT_BYTES or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("changed object")
            self._path(owner, digest)
            return data
        except WorkspaceError:
            raise
        except (OSError, ValueError):
            raise WorkspaceError("context_reference_unreadable", "历史证据缺失、损坏或无法核对。") from None

    def load(self, reference: str) -> bytes:
        """供宿主完整核验reference原文；最多8MiB，返回字节，不向模型开放无界读取入口。"""
        match = _REFERENCE.fullmatch(reference)
        if match is None:
            raise WorkspaceError("context_reference_invalid", "历史证据引用无效。")
        owner, digest = match.groups()
        owner = str(UUID(owner))
        self._authorize_reference(reference, owner)
        return self._read_raw(owner, digest)

    def read(self, arguments: ReadContextArguments) -> dict:
        """按reference读取一页，返回文本和字节游标；无副作用，不自动执行或恢复历史工具。"""
        match = _REFERENCE.fullmatch(arguments.reference)
        if match is None:
            raise WorkspaceError("context_reference_invalid", "历史证据引用无效。")
        owner, digest = match.groups()
        try:
            owner = str(UUID(owner))
        except ValueError:
            raise WorkspaceError("context_reference_invalid", "历史证据引用无效。") from None
        self._authorize_reference(arguments.reference, owner)
        raw = self._read_raw(owner, digest)
        if arguments.offset > len(raw):
            raise WorkspaceError("context_offset_invalid", "历史证据偏移超过内容大小。")
        end = min(arguments.offset + arguments.max_bytes, len(raw))
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(raw[arguments.offset:end], final=end == len(raw))
        buffered, _ = decoder.getstate()
        next_offset = end - len(buffered)
        return {
            "reference": arguments.reference, "sha256": digest, "size_bytes": len(raw),
            "offset": arguments.offset, "next_offset": next_offset, "eof": next_offset == len(raw),
            "content": text, "decoding_replaced": "\ufffd" in text,
        }
