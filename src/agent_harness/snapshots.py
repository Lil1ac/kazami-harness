"""普通文件任务的有界快照与派生副本；无法核对时保持单次任务结果并拒绝追加。"""

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from uuid import UUID, uuid4

from .sessions import payload_digest
from .workspace import Workspace, WorkspaceError


class SnapshotError(Exception):
    """携带受控文件快照错误，供页面解释不可继续原因。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定 code 和安全 message，返回 None；不保存文件正文或原始异常。"""
        self.code = code  # 可展示原因码。
        self.message = message  # 不含不可信路径的中文说明。
        super().__init__(message)


def _identity(path: Path) -> tuple[int, int]:
    """返回 path 的设备和对象标识；仅用于当前目录归属检查，不依赖 Windows ctime。"""
    value = path.stat()
    return value.st_dev, value.st_ino


def _relative(value: str) -> str:
    """验证快照内部相对路径 value；拒绝越界、设备路径和控制目录，返回规范 POSIX 路径。"""
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if not value or '\\' in value or '\0' in value or posix.is_absolute() or windows.drive or windows.root or '..' in posix.parts or any(part in ('.git',) for part in posix.parts):
        raise SnapshotError('workspace_snapshot_unsafe', '工作区包含不适合普通文件快照的路径，请使用代码项目模式。')
    if posix.as_posix() != value or any(':' in part or part.endswith(('.', ' ')) for part in posix.parts):
        raise SnapshotError('workspace_snapshot_unsafe', '普通工作区包含无法安全继承的文件路径。')
    return value


class FileSnapshotStore:
    """保存普通工作区完整文件快照，继承时同时检查原目录与保存内容。"""

    MAX_BYTES = 16 * 1024 * 1024  # 沿用普通工作区复制上限16MiB。
    MAX_ENTRIES = 1000  # 包含文件和空目录，防止目录遍历无界增长。
    MAX_METADATA_BYTES = 2 * 1024 * 1024  # 快照清单读取上限。

    def __init__(self, runs_root: Path) -> None:
        """绑定受保护 runs_root；构造时不创建目录或读取工作区。"""
        self.runs_root = runs_root.resolve()  # 普通模型工具不能访问的记录根目录。
        self.root = self.runs_root / '.file-snapshots'  # 按运行 UUID 存清单和原始文件副本。

    def _directory(self, run_id: UUID) -> Path:
        """返回 run_id 的快照目录，拒绝链接替换和路径越界，不创建目录。"""
        path = self.root / str(UUID(str(run_id)))
        if self.root.resolve() != self.root or path.resolve() != path:
            raise SnapshotError('workspace_snapshot_unreadable', '文件快照目录发生变化，不能继续。')
        return path

    def _tree(self, root: Path) -> tuple[tuple[int, int], list[dict], dict[str, bytes]]:
        """有界读取 root 的完整普通文件树，返回根身份、排序清单和正文，不执行命令。

        拒绝链接、Git控制目录、特殊文件和记录目录；读取前后检查同一文件状态，
        任何不确定状态都抛 SnapshotError，不将部分内容视为可信快照。
        """
        root = root.resolve(strict=True)
        identity = _identity(root)
        workspace = Workspace(root, self.runs_root)
        entries, contents = [], {}
        total = 0
        stack = [root]
        while stack:
            directory = stack.pop()
            for path in sorted(directory.iterdir()):
                if len(entries) >= self.MAX_ENTRIES:
                    raise SnapshotError('workspace_snapshot_limit', '普通工作区超过1000个目录项，无法保存可继续快照。')
                name = _relative(path.relative_to(root).as_posix())
                if path.is_symlink() or path.is_junction():
                    raise SnapshotError('workspace_snapshot_unsafe', '普通工作区包含链接，不能自动继承。')
                checked = workspace.resolve(name, must_exist=True)
                before = checked.stat()
                if stat.S_ISDIR(before.st_mode):
                    entries.append({'path': name, 'kind': 'directory'})
                    stack.append(checked)
                elif stat.S_ISREG(before.st_mode):
                    if total + before.st_size > self.MAX_BYTES:
                        raise SnapshotError('workspace_snapshot_limit', '普通工作区超过16MiB，无法保存可继续快照。')
                    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
                    with os.fdopen(os.open(checked, flags), 'rb') as source:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                            raise SnapshotError('workspace_snapshot_changed', '快照读取期间文件发生变化。')
                        raw = source.read(self.MAX_BYTES - total + 1)
                        after = os.fstat(source.fileno())
                    final = checked.stat()
                    if path.is_symlink() or path.is_junction() or workspace.resolve(name, must_exist=True) != checked or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
                        raise SnapshotError('workspace_snapshot_changed', '快照读取期间文件发生变化。')
                    total += len(raw)
                    if total > self.MAX_BYTES:
                        raise SnapshotError('workspace_snapshot_limit', '普通工作区在读取期间超过16MiB。')
                    entries.append({'path': name, 'kind': 'file', 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'mode': stat.S_IMODE(before.st_mode)})
                    contents[name] = raw
                else:
                    raise SnapshotError('workspace_snapshot_unsafe', '普通工作区包含特殊文件，不能自动继承。')
        if _identity(root) != identity:
            raise SnapshotError('workspace_snapshot_changed', '快照读取期间工作目录发生变化。')
        return identity, sorted(entries, key=lambda item: item['path']), contents

    def _write_info(self, directory: Path, value: dict) -> None:
        """原子保存 directory 的状态清单 value；成功前刷新文件，不写入 Runner 事件。"""
        temporary = directory / ('.info-' + str(uuid4()))
        try:
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            if len(raw) > self.MAX_METADATA_BYTES:
                raise SnapshotError('workspace_snapshot_limit', '文件快照清单超过上限。')
            with temporary.open('xb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, directory / 'snapshot.json')
        finally:
            temporary.unlink(missing_ok=True)

    def info(self, run_id: UUID) -> dict | None:
        """读取 run_id 的有界清单；无快照返回 None，受损数据抛 SnapshotError，不读取当前文件树。"""
        try:
            directory = self._directory(run_id)
            path = directory / 'snapshot.json'
            if not path.exists():
                return None
            if path.resolve() != path:
                raise ValueError('linked metadata')
            with path.open('rb') as source:
                raw = source.read(self.MAX_METADATA_BYTES + 1)
            if len(raw) > self.MAX_METADATA_BYTES:
                raise ValueError('metadata limit')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('run_id') != str(run_id) or value.get('status') not in ('ready', 'error'):
                raise ValueError('invalid metadata')
            if value['status'] == 'ready':
                entries = value['entries']
                if not isinstance(entries, list) or len(entries) > self.MAX_ENTRIES or payload_digest(entries) != value['tree_sha256']:
                    raise ValueError('invalid digest')
                if not isinstance(value.get('workspace'), str) or not Path(value['workspace']).is_absolute() or not isinstance(value.get('identity'), list) or len(value['identity']) != 2 or any(type(part) is not int or part < 0 for part in value['identity']):
                    raise ValueError('invalid origin')
                names = set()
                total = 0
                for item in entries:
                    name = _relative(item['path'])
                    if name in names or item['kind'] not in ('file', 'directory'):
                        raise ValueError('invalid entry')
                    if item['kind'] == 'file':
                        if type(item.get('size_bytes')) is not int or item['size_bytes'] < 0 or type(item.get('mode')) is not int or not 0 <= item['mode'] <= 0o777 or not isinstance(item.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
                            raise ValueError('invalid file')
                        total += item['size_bytes']
                    if total > self.MAX_BYTES:
                        raise ValueError('snapshot limit')
                    names.add(name)
            return value
        except SnapshotError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise SnapshotError('workspace_snapshot_unreadable', '普通工作区快照无法核对。') from None

    def capture(self, run_id: UUID, workspace: Path) -> None:
        """在运行收尾时保存 workspace 的固定快照；失败仅保存不可继续原因，不改运行结果。"""
        directory = self._directory(run_id)
        if directory.exists():
            raise SnapshotError('workspace_snapshot_exists', '本轮文件快照已经建立，不能覆盖。')
        directory.mkdir(parents=True)
        try:
            identity, entries, contents = self._tree(workspace)
            files = directory / 'files'
            files.mkdir()
            for item in entries:
                target = files / item['path']
                if item['kind'] == 'directory':
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(contents[item['path']])
            again_identity, again_entries, _ = self._tree(workspace)
            if again_identity != identity or again_entries != entries:
                raise SnapshotError('workspace_snapshot_changed', '保存快照期间工作区发生变化。')
            self._write_info(directory, {'run_id': str(run_id), 'status': 'ready', 'workspace': str(workspace.resolve()), 'identity': list(identity), 'entries': entries, 'tree_sha256': payload_digest(entries)})
        except (SnapshotError, WorkspaceError, OSError, ValueError) as error:
            code = error.code if isinstance(error, SnapshotError) else 'workspace_snapshot_unreadable'
            message = error.message if isinstance(error, SnapshotError) else '工作区无法完整保存可继续快照，请检查目录与访问权限。'
            self._write_info(directory, {'run_id': str(run_id), 'status': 'error', 'error_code': code, 'error_message': message})

    def restore(self, run_id: UUID, workspace: Path, destination: Path) -> None:
        """核对父 workspace 与 run_id 快照后创建新 destination；禁止覆盖目录，失败不执行模型。

        destination 由宿主从新 UUID 生成，必须与父目录及记录目录分离。文件正文从保存副本
        读取并逐个验证摘要，最后核对派生树完整性；原工作区与快照均不修改。
        """
        try:
            value = self.info(run_id)
            if value is None or value['status'] != 'ready':
                raise SnapshotError('workspace_snapshot_missing', '前轮没有完整文件快照，不能自动继续。')
            identity, entries, _ = self._tree(workspace)
            if value['workspace'] != str(workspace.resolve()) or list(identity) != value['identity'] or entries != value['entries']:
                raise SnapshotError('workspace_snapshot_stale', '工作区在前轮结束后发生变化，请核对后新建任务。')
            destination = destination.resolve()
            if destination.is_relative_to(workspace.resolve()) or workspace.resolve().is_relative_to(destination) or destination.is_relative_to(self.runs_root):
                raise SnapshotError('workspace_snapshot_unsafe', '派生工作区与来源或记录目录重叠。')
            destination.mkdir(parents=True, exist_ok=False)
            files = self._directory(run_id) / 'files'
            if files.resolve() != files:
                raise SnapshotError('workspace_snapshot_unreadable', '文件快照正文目录发生变化。')
            for item in entries:
                target = destination / item['path']
                if item['kind'] == 'directory':
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    source = files / item['path']
                    if source.resolve() != source:
                        raise SnapshotError('workspace_snapshot_unreadable', '文件快照正文路径发生变化。')
                    with source.open('rb') as handle:
                        raw = handle.read(self.MAX_BYTES + 1)
                    if len(raw) != item['size_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                        raise SnapshotError('workspace_snapshot_unreadable', '文件快照正文不完整，未继续执行。')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    target.chmod(item['mode'])
            if self._tree(destination)[1] != entries or self._tree(workspace)[1] != entries:
                raise SnapshotError('workspace_snapshot_changed', '派生工作区期间文件发生变化。')
        except SnapshotError:
            raise
        except (OSError, ValueError, KeyError, WorkspaceError):
            raise SnapshotError('workspace_snapshot_unreadable', '无法核对或创建后续文件工作区，尚未调用模型。') from None
