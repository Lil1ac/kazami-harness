"""原目录执行的身份核验与逐轮只读变更记录；不复制执行目录、不操作Git索引。"""

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from .schemas import DataModel, RunOrigin
from .snapshots import FileSnapshotStore, SnapshotError


class LocalFileChange(DataModel):
    """原目录本轮前后快照中的文件变化，不包含早已存在的未修改文件。"""

    path: str  # 相对于实际执行目录的文件路径。
    status: Literal["added", "modified", "deleted"]  # 相对于本轮开始时，而非Git HEAD。
    binary: bool  # 无法按UTF-8文本展示时为True；原始字节保存在快照中。


class LocalChangeView(DataModel):
    """原目录执行的独立差异状态；报告失败不改写Runner结果，也不阻止继续当前目录。"""

    status: Literal["pending", "ready", "error"] = "pending"  # pending表示尚未固化，不能冒充无变化。
    files: list[LocalFileChange] = Field(default_factory=list)  # 本轮实际变化的完整文件列表。
    patch_available: bool = False  # 可下载文本差异报告；二进制仅列变化，不宣称可自动回滚。
    warnings: list[str] = Field(default_factory=list)  # 采集受限或报告损坏的受控提示。


class LocalChangeStore:
    """按运行UUID保存原目录执行前后的有界快照与差异，不把快照作为继续的前提。"""

    EXCLUDE_NAMES = frozenset({'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.uv-cache', '.next', '.tox', '.nox'})  # 依赖与控制目录不属于源码差异采集范围。
    MAX_PATCH_BYTES = 32 * 1024 * 1024  # 完整文本差异上限，超限明确报告失败。
    MAX_METADATA_BYTES = 2 * 1024 * 1024  # 报告清单的最大读取字节数。

    def __init__(self, runs_root: Path, protected_roots: tuple[Path, ...] = ()) -> None:
        """绑定记录区及宿主派生目录；参数只由服务传入，构造不读项目、不创建目录。"""
        self.runs_root = runs_root.resolve()  # 工具访问隔离的运行记录区。
        self.root = self.runs_root / '.local-changes'  # 只存报告元数据，不是执行目录。
        self.protected_roots = tuple(path.resolve() for path in (self.runs_root, *protected_roots))  # 实际任务禁止位于这些派生/记录目录内。
        protected = self.protected_roots  # 防止打开本项目时把记录区采集进自身。
        self.before = FileSnapshotStore(self.runs_root, namespace='.local-before', exclude_names=self.EXCLUDE_NAMES, protected_roots=protected)  # 固定每轮开始时的文件。
        self.after = FileSnapshotStore(self.runs_root, namespace='.local-after', exclude_names=self.EXCLUDE_NAMES, protected_roots=protected)  # 固定每轮结束时的文件及产物。

    def identity(self, root: Path) -> tuple[int, int]:
        """返回实体root的设备/对象编号；路径替换、链接或记录区内目录抛SnapshotError。"""
        try:
            if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir() or root.is_symlink() or root.is_junction() or any(root.is_relative_to(path) for path in self.protected_roots):
                raise ValueError('invalid root')
            stat = root.stat()
            return stat.st_dev, stat.st_ino
        except (OSError, ValueError, RuntimeError):
            raise SnapshotError('local_workspace_changed', '所选原目录已不可用或被替换，请重新打开项目。') from None

    def verify(self, origin: RunOrigin, workspace: Path) -> Path:
        """核对origin固定的目录和workspace身份，返回原目录；无副作用，不核对文件内容。"""
        root = origin.registered_path
        if origin.execution_mode != 'local' or origin.workspace_identity is None or root != workspace or self.identity(root) != origin.workspace_identity:
            raise SnapshotError('local_workspace_changed', '会话实际目录与所选项目不一致，请重新打开项目。')
        return root

    def begin(self, run_id: UUID, root: Path) -> None:
        """运行模型前固定run_id的root文件基线；有界采集失败由快照清单明确记录。"""
        self.before.capture(run_id, root)

    def _directory(self, run_id: UUID) -> Path:
        """返回run_id报告目录；拒绝控制区的链接替换，不创建目录。"""
        directory = self.root / str(UUID(str(run_id)))
        if self.root.resolve() != self.root or directory.resolve() != directory:
            raise SnapshotError('local_changes_unreadable', '本轮变更记录目录无法核对。')
        return directory

    def _write(self, directory: Path, name: str, raw: bytes) -> None:
        """在已核验directory原子写入内部name文件，返回None，不操作项目或运行事件。"""
        temporary = directory / ('.pending-' + uuid4().hex)
        try:
            with temporary.open('xb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, directory / name)
        finally:
            temporary.unlink(missing_ok=True)

    def finish(self, run_id: UUID, root: Path) -> None:
        """固定本轮结束文件并生成相对开始状态的差异；失败记录error，不伪造完整报告。"""
        directory = self._directory(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        view = LocalChangeView()
        patch = b''
        try:
            self.after.capture(run_id, root)
            before, after = self.before.info(run_id), self.after.info(run_id)
            for snapshot in (before, after):
                if snapshot is None or snapshot['status'] != 'ready':
                    raise SnapshotError('local_changes_incomplete', '本轮文件超过采集范围、包含不支持的文件或读取期间发生变化；差异未完整保存，当前目录仍可继续。')
            if before['workspace'] != after['workspace'] or before['identity'] != after['identity']:
                raise SnapshotError('local_workspace_changed', '本轮执行期间原目录身份发生变化，不能生成可信差异。')
            old = {item['path']: item for item in before['entries'] if item['kind'] == 'file'}
            new = {item['path']: item for item in after['entries'] if item['kind'] == 'file'}
            files = []
            parts = []
            size = 0
            for name in sorted(old.keys() | new.keys()):
                if old.get(name) == new.get(name):
                    continue
                previous = self.before.saved_file(run_id, name).read_bytes() if name in old else b''
                current = self.after.saved_file(run_id, name).read_bytes() if name in new else b''
                try:
                    binary = b'\0' in previous or b'\0' in current
                    previous.decode('utf-8')
                    current.decode('utf-8')
                except UnicodeError:
                    binary = True
                files.append(LocalFileChange(path=name, status='added' if name not in old else 'deleted' if name not in new else 'modified', binary=binary))
                if binary:
                    part = ('Binary file changed: ' + name + '\n').encode('utf-8')
                else:
                    lines = difflib.diff_bytes(difflib.unified_diff, previous.splitlines(keepends=True), current.splitlines(keepends=True), fromfile=('a/' + name).encode('utf-8') if name in old else b'/dev/null', tofile=('b/' + name).encode('utf-8') if name in new else b'/dev/null')
                    part = b''.join(line if line.endswith(b'\n') else line + b'\n\\ No newline at end of file\n' for line in lines)
                size += len(part)
                if size > self.MAX_PATCH_BYTES:
                    raise SnapshotError('local_changes_limit', '本轮文本差异超过32MiB，未保存截断报告；可在原目录核对。')
                parts.append(part)
            patch = b''.join(parts)
            view = LocalChangeView(status='ready', files=files, patch_available=True)
            self._write(directory, 'changes.diff', patch)
        except (SnapshotError, OSError, ValueError) as error:
            message = error.message if isinstance(error, SnapshotError) else '本轮差异无法完整保存，当前目录仍可继续；请检查磁盘与访问权限。'
            view = LocalChangeView(status='error', warnings=[message])
        metadata = {'run_id': str(run_id), 'view': view.model_dump(mode='json'), 'patch_size': len(patch), 'patch_sha256': hashlib.sha256(patch).hexdigest()}
        self._write(directory, 'changes.json', json.dumps(metadata, ensure_ascii=False).encode('utf-8'))

    def _metadata(self, run_id: UUID) -> dict | None:
        """有界读取run_id报告；尚未完成返回None，元数据损坏/链接抛SnapshotError。"""
        path = self._directory(run_id) / 'changes.json'
        if not path.exists():
            return None
        try:
            if path.resolve() != path:
                raise ValueError('linked report')
            with path.open('rb') as source:
                raw = source.read(self.MAX_METADATA_BYTES + 1)
            if len(raw) > self.MAX_METADATA_BYTES:
                raise ValueError('report too large')
            value = json.loads(raw)
            if value['run_id'] != str(run_id):
                raise ValueError('wrong report')
            LocalChangeView.model_validate_json(json.dumps(value['view']))
            return value
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            raise SnapshotError('local_changes_unreadable', '本轮变更记录损坏或无法读取。') from None

    def summary(self, run_id: UUID) -> LocalChangeView:
        """只读返回run_id差异摘要；未完成为pending，坏记录明确为error，不扫描当前项目。"""
        try:
            value = self._metadata(run_id)
            return LocalChangeView.model_validate_json(json.dumps(value['view'])) if value else LocalChangeView()
        except SnapshotError as error:
            return LocalChangeView(status='error', warnings=[error.message])

    def patch_path(self, run_id: UUID) -> Path:
        """返回run_id已保存文本差异的路径；核对大小/SHA，缺失或损坏抛SnapshotError。"""
        try:
            value = self._metadata(run_id)
            if value is None or not value['view']['patch_available'] or value['view']['status'] != 'ready':
                raise ValueError('not ready')
            path = self._directory(run_id) / 'changes.diff'
            if path.resolve() != path:
                raise ValueError('linked diff')
            with path.open('rb') as source:
                raw = source.read(self.MAX_PATCH_BYTES + 1)
            if len(raw) > self.MAX_PATCH_BYTES or len(raw) != value['patch_size'] or hashlib.sha256(raw).hexdigest() != value['patch_sha256']:
                raise ValueError('invalid diff')
            return path
        except (OSError, ValueError, KeyError, TypeError):
            raise SnapshotError('local_changes_unreadable', '本轮差异尚未准备完成或完整性检查失败。') from None
