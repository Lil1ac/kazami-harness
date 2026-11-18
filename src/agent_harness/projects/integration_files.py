"""成果集成的普通文件快照、私有 Git 合并与受校验的逐文件写回。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import zlib

from agent_harness.execution.processes import _child_environment, native_io_path
from agent_harness.projects.manager import ProjectManager
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import TaskError


MAX_FILES = 10000  # 完整候选最多一万个普通文件，超限拒绝，不截断代码。
MAX_BYTES = 256 * 1024 * 1024  # 完整候选正文上限256MiB，与产物下载快照的限额独立。
PROTECTED = {".git", ".agents", ".codex"}  # 控制目录不能随成果写入目标。


def safe_name(name: str) -> str:
    """校验仓库相对文件 name 并返回原值；越界、设备名和控制路径抛 TaskError。"""
    path, windows = PurePosixPath(name), PureWindowsPath(name)
    if (not name or path.as_posix() != name or path.is_absolute() or windows.drive or windows.root
            or "\\" in name or "\0" in name or ".." in path.parts
            or any(part.lower() == ".git" or ":" in part or part.endswith((".", " "))
                   or PureWindowsPath(part).is_reserved() for part in path.parts)):
        raise TaskError("integration_unsafe_path", "成果包含不支持或受保护的文件路径。")
    return name


def git(root: Path, *arguments: str, data: bytes | None = None, allowed: tuple[int, ...] = (0,)) -> bytes:
    """在 root 执行宿主固定 Git 参数，返回完整 stdout；data 为标准输入，失败或超时拒绝。

    仅用于受控集成命令，禁用系统/用户配置、Hooks及外部驱动；不运行项目脚本。
    """
    environment = _child_environment()
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1")
    command = ["git", "--no-pager", "--no-optional-locks", "-c", f"core.hooksPath={os.devnull}",
               "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
               "-c", "core.longpaths=true", "-C", str(root), *arguments]
    try:
        result = subprocess.run(command, input=data, stdin=subprocess.DEVNULL if data is None else None, capture_output=True, timeout=30, env=environment,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("integration_git_failed", "集成所需 Git 操作无法完成，请检查 Git 和目录。") from None
    if result.returncode not in allowed or len(result.stdout) > MAX_BYTES + 16 * 1024 * 1024:
        raise TaskError("integration_git_failed", "Git 无法完成集成，或结果超出安全上限。")
    return result.stdout


def identity(path: Path) -> list[int]:
    """返回实体目录 path 的设备与对象标识；链接、替换或不可用目录拒绝。"""
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir() or path.is_symlink() or path.is_junction():
        raise TaskError("integration_directory_changed", "目录身份无法核对，请重新打开目标。")
    info = path.stat()
    return [info.st_dev, info.st_ino]


def checked_path(root: Path, name: str) -> Path:
    """解析 root 内 name，逐层拒绝链接与重解析点；不创建目录，返回普通路径。"""
    path = root
    parts = PurePosixPath(safe_name(name)).parts
    for index, component in enumerate(parts):
        path = path / component
        if path.is_symlink() or path.is_junction() or path.resolve() != path:
            raise TaskError("integration_path_changed", "文件路径经过链接或已被替换，未继续写入。")
        if index < len(parts) - 1 and path.exists() and not path.is_dir():
            raise TaskError("integration_ignored_collision", "目标父路径已被普通文件占用，不能自动覆盖或改成目录。")
    return path


def entry(raw: bytes, mode: int = 0o644) -> dict:
    """由固定 raw 字节与普通文件 mode 创建内容清单；返回SHA256、大小和可执行位。"""
    return {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "mode": 0o755 if mode & 0o111 else 0o644}


def read_file(path: Path) -> tuple[dict, bytes]:
    """稳定读取普通文件 path；返回清单和正文，读取中变化、链接或超限拒绝。"""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES or path.is_symlink() or path.is_junction():
        raise TaskError("integration_unsupported_file", "集成仅支持大小受限的普通文件。")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()
    signatures = [(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) for value in (before, opened, after, final)]
    if len(set(signatures)) != 1 or len(raw) > MAX_BYTES:
        raise TaskError("integration_files_changed", "读取代码期间文件发生变化，请重新准备。")
    return entry(raw, before.st_mode), raw


class IntegrationFiles:
    """在独立证据目录保存按内容寻址的字节；候选和目标不会成为历史读取的后备来源。"""

    def __init__(self, root: Path):
        """绑定受宿主管理的 root；构造不创建目录，文件对象保存于其 objects 子目录。"""
        self.root = root  # 每条集成记录独占的证据目录。
        self.objects = native_io_path(root / "objects")  # 嵌套团队证据目录加上 64 位摘要也能完整读写。

    def save(self, raw: bytes, mode: int = 0o644) -> dict:
        """保存 raw 并返回带 mode 的清单；同摘要对象仅核验复用，写盘失败向上传播。"""
        item = entry(raw, mode)
        self.objects.mkdir(parents=True, exist_ok=True)
        path = self.objects / item["sha256"]
        if path.exists():
            self.read(item)
        else:
            ProjectManager._atomic_write(path, raw)
        return item

    def read(self, item: dict) -> bytes:
        """核验 item 指向的固定对象并返回字节；缺失、链接或摘要不符拒绝，不读取现场代码。"""
        digest = item["sha256"]
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise TaskError("integration_evidence_invalid", "成果内容引用无效。")
        path = self.objects / digest
        if path.resolve() != path or not path.is_file():
            raise TaskError("integration_evidence_invalid", "成果固定内容缺失或被替换。")
        raw = path.read_bytes()
        if entry(raw, item["mode"]) != item:
            raise TaskError("integration_evidence_invalid", "成果内容核验失败。")
        return raw

    def git_tree(self, root: Path, tree: str, *, git_dir: Path | None = None) -> dict:
        """批量读取root固定tree的原始blob，返回完整清单；git_dir非空时显式指定来源仓库。

        不使用git archive，避免export-ignore/export-subst属性删改固定成果；不调用过滤器。
        """
        prefix = ["--git-dir=" + str(git_dir)] if git_dir is not None else []
        listing = git(root, *prefix, "ls-tree", "-r", "-z", tree)
        entries, names = [], set()
        for row in listing.split(b"\0"):
            if not row:
                continue
            metadata, path = row.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = safe_name(path.decode("utf-8"))
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise TaskError("integration_unsupported_file", "成果包含子模块、链接或特殊文件，不能自动采纳。")
            if os.name == "nt" and name.casefold() in names:
                raise TaskError("integration_case_collision", "成果包含Windows大小写路径碰撞，不能自动采纳。")
            names.add(name.casefold())
            entries.append((name, oid, int(mode[-3:], 8)))
        if len(entries) > MAX_FILES:
            raise TaskError("integration_snapshot_limit", "完整集成代码超过一万个文件。")
        wanted = list(dict.fromkeys(item[1] for item in entries))
        output = git(root, *prefix, "cat-file", "--batch", data="".join(oid + "\n" for oid in wanted).encode())
        position, contents = 0, {}
        for oid in wanted:
            end = output.index(b"\n", position)
            actual, kind, length = output[position:end].decode().split()
            size = int(length)
            if actual != oid or kind != "blob" or size > MAX_BYTES:
                raise TaskError("integration_evidence_invalid", "固定Git内容无法完整核验。")
            start = end + 1
            contents[oid] = output[start:start + size]
            if len(contents[oid]) != size or output[start + size:start + size + 1] != b"\n":
                raise TaskError("integration_evidence_invalid", "固定Git内容不完整。")
            position = start + size + 1
        if sum(len(contents[oid]) for _, oid, _ in entries) > MAX_BYTES:
            raise TaskError("integration_snapshot_limit", "完整集成代码超过256MiB。")
        return {name: self.save(contents[oid], mode) for name, oid, mode in entries}

    def scan(self, root: Path, names: list[str]) -> dict:
        """稳定采集 root 的 names 普通文件，缺失路径视为删除；返回完整有界清单。"""
        result, total = {}, 0
        for name in sorted(set(names)):
            path = checked_path(root, name)
            if not path.exists():
                continue
            item, raw = read_file(path)
            total += len(raw)
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                raise TaskError("integration_snapshot_limit", "完整集成代码超过256MiB或一万个文件。")
            result[name] = self.save(raw, item["mode"])
        return result

    def materialize(self, root: Path, manifest: dict) -> None:
        """将 manifest 固定内容写入新建 root；目录必须不存在，返回 None，不执行项目代码。"""
        root.mkdir(parents=True, exist_ok=False)
        for name, item in manifest.items():
            path = checked_path(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.read(item))
            if os.name != "nt":
                path.chmod(item["mode"])
        git(root, "init", "--object-format=sha1", "-q")  # 独立Git控制目录，避免候选命令向上发现目标仓库。

    def tree(self, repository: Path, manifest: dict) -> str:
        """在私有 repository 构造 manifest 的规范文本树，返回Git树ID；不创建提交或更新目标索引。"""
        records = []
        for name, item in sorted(manifest.items()):
            raw = normalized(self.read(item))
            packed = b"blob " + str(len(raw)).encode() + b"\0" + raw
            digest = hashlib.sha1(packed).hexdigest()
            path = native_io_path(repository / ".git" / "objects" / digest[:2] / digest[2:])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                ProjectManager._atomic_write(path, zlib.compress(packed))
            records.append(f"{100755 if item['mode'] & 0o111 else 100644} {digest}\t{name}".encode() + b"\0")
        git(repository, "read-tree", "--empty")
        git(repository, "update-index", "-z", "--index-info", data=b"".join(records))
        return git(repository, "write-tree").decode().strip()

    def merge(self, base: dict, target: dict, source: dict) -> tuple[dict, list[str]]:
        """三方合并 base/target/source，返回候选清单与冲突路径；目标字节与换行在未变时保持。"""
        repository = self.root / "merge-repository"
        repository.mkdir()
        git(repository, "init", "--object-format=sha1", "-q")
        trees = [self.tree(repository, manifest) for manifest in (base, target, source)]
        output = git(repository, "merge-tree", "--write-tree", "-z", "--merge-base=" + trees[0],
                     trees[1], trees[2], allowed=(0, 1))
        tree = output[:40].decode()
        conflicts = sorted({value.decode("utf-8") for value in re.findall(rb"[0-7]{6} [0-9a-f]{40} [123]\t([^\0]+)\0", output)})
        result = self.git_tree(repository, tree)
        for name, item in list(result.items()):
            raw = self.read(item)
            previous = target.get(name)
            if previous is not None:
                before = self.read(previous)
                if normalized(before) == raw:
                    result[name] = previous
                elif text(before) is not None and text(raw) is not None:
                    if b"\r\n" in before and b"\n" not in before.replace(b"\r\n", b""):
                        raw = raw.replace(b"\n", b"\r\n")
                    if before.startswith(b"\xef\xbb\xbf") and not raw.startswith(b"\xef\xbb\xbf"):
                        raw = b"\xef\xbb\xbf" + raw
                    result[name] = self.save(raw, item["mode"])
        return result, conflicts


def text(raw: bytes) -> str | None:
    """将无NUL的UTF-8 raw解码为可审阅文本；二进制或其他编码返回None。"""
    try:
        return raw.decode("utf-8-sig") if b"\0" not in raw else None
    except UnicodeDecodeError:
        return None


def normalized(raw: bytes) -> bytes:
    """仅规范UTF-8文本的BOM与CRLF供三方合并；二进制保留全部字节。"""
    value = text(raw)
    return value.replace("\r\n", "\n").encode("utf-8") if value is not None else raw


def fingerprint(manifest: dict) -> str:
    """返回已排序文件清单 manifest 的稳定指纹；不访问现场文件。"""
    return payload_digest(manifest)


def workspace_fingerprint(root: Path) -> str | None:
    """为Git源码root计算规范文本内容指纹；普通目录或不可完整核验返回None，不执行项目代码。

    只纳入已跟踪和非忽略文件，包含删除状态；忽略缓存、依赖及外部环境不属于该源码版本。
    """
    if not (root / ".git").exists():
        return None
    try:
        names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        result, total = {}, 0
        for raw_name in sorted(set(names)):
            if not raw_name:
                continue
            name = raw_name.decode("utf-8")
            path = checked_path(root, name)
            if not path.exists():
                continue
            item, raw = read_file(path)
            total += len(raw)
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                return None
            result[name] = entry(normalized(raw), item["mode"])
        return fingerprint(result)
    except (TaskError, OSError, UnicodeError):
        return None
