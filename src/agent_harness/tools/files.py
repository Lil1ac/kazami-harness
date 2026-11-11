"""工作区内的 UTF-8 分页、字面量搜索、原子写入与版本核验的局部编辑。"""

import codecs
import fnmatch
import hashlib
import os
import stat
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import Field, field_validator

from agent_harness.schemas import DataModel
from agent_harness.execution.workspace import Workspace, WorkspaceError


# 文件工具的严格参数结构；使用类前注释，保持自动生成的工具 JSON Schema 不变。
class ReadFileArguments(DataModel):
    path: str = Field(min_length=1, description="工作区内的文件路径，推荐相对路径。")
    offset: int = Field(default=0, ge=0, description="从 UTF-8 字符边界开始的字节偏移。")
    max_bytes: int = Field(default=8192, ge=1, le=65536, description="本页最多返回的 UTF-8 字节数。")

    @field_validator("path")
    @classmethod
    def nonblank_path(cls, value: str) -> str:
        """拒绝纯空白路径和空字节，合法路径原样交给工作区边界检查。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("路径不能为空白或包含空字节")
        return value


# 保持既有文件工具异常导入接口，同时让所有工具共用同一套边界错误协议。
FileToolError = WorkspaceError


def _resolve_file(workspace: Path, runs_root: Path, requested: str) -> Path:
    """将文件工具的路径解析委托给统一工作区边界，保留既有调用接口。"""
    return Workspace(workspace, runs_root).resolve(requested)


def _decode_page(raw: bytes, max_bytes: int, preview_chars: int) -> tuple[str, int]:
    """在完整 UTF-8 字符边界截取一页，返回文本及其实际占用的字节数。

    raw 包含最多四字节前瞻，用于区分分页截断与损坏编码；文本再按字符限额裁剪，
    返回的字节数按最终文本重新计算，供调用方生成准确的 next_offset。
    """
    candidate = raw[:max_bytes]
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        # 增量解码器保留末尾未完整的字符，返回内容不会出现替代符或半个码点。
        content = decoder.decode(candidate, final=False)
        pending = decoder.getstate()[0]
        if pending:
            used = len(candidate) - len(pending)
            first_byte = pending[0]
            character_bytes = 2 if first_byte < 0xE0 else 3 if first_byte < 0xF0 else 4
            # 多读的至多四个字节只用来区分合法分页边界与真正损坏的 UTF-8。
            raw[used : used + character_bytes].decode("utf-8", errors="strict")
            if used == 0:
                raise FileToolError(
                    "max_bytes_too_small", "max_bytes 无法容纳下一个完整字符，请设置为至少 4。"
                )
    except UnicodeDecodeError:
        raise FileToolError("invalid_encoding", "文件不是有效的 UTF-8 文本，请先转换编码。") from None
    content = content[:preview_chars]
    return content, len(content.encode("utf-8"))


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """返回对象、长度、修改时间及权限；info 为 stat/fstat 结果，内容版本另用 SHA-256。

    Windows 的 stat/fstat 对 ctime 的语义可能不同，不能用它比较同一文件的句柄和路径。
    """
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


# 整份文件的换行类型；none表示没有换行，mixed包含多种换行，超出扫描预算另用None表示未知。
NewlineStyle = Literal["lf", "crlf", "cr", "mixed", "none"]


def _newline_style(line_feeds: int, carriage_returns: int, pairs: int) -> NewlineStyle:
    """根据完整计数返回换行类型，不访问文件；line_feeds/carriage_returns包含pairs中的字符。

    三个参数分别是LF、CR字符总数与CRLF对数；无换行返回none，多种换行返回mixed。
    """
    counts = {"lf": line_feeds - pairs, "crlf": pairs, "cr": carriage_returns - pairs}
    present = [kind for kind, count in counts.items() if count]
    return "none" if not present else present[0] if len(present) == 1 else "mixed"


def _inspect_open_file(stream: BinaryIO, max_bytes: int) -> tuple[str, NewlineStyle]:
    """从stream同时计算完整SHA-256和换行类型，返回二元组；max_bytes为完整检查的字节预算。

    最多读取max_bytes+1字节，超限抛file_changed；以64KiB块扫描且处理跨块CRLF。
    句柄位置移至末尾，调用方负责路径、普通文件和前后状态检查，不扩大模型可见内容。
    """
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = max_bytes + 1
    line_feeds = carriage_returns = pairs = 0  # 原始字符和配对数量，用于完整文件分类。
    previous_cr = False  # 上一个块以CR结尾时，下一块首个LF仍与其属于同一CRLF。
    while remaining:
        block = stream.read(min(65536, remaining))
        if not block:
            return digest.hexdigest(), _newline_style(line_feeds, carriage_returns, pairs)
        digest.update(block)
        line_feeds += block.count(b"\n")
        carriage_returns += block.count(b"\r")
        pairs += block.count(b"\r\n") + int(previous_cr and block.startswith(b"\n"))
        previous_cr = block.endswith(b"\r")
        remaining -= len(block)
    raise FileToolError("file_changed", "文件在读取期间变大，请重新读取后编辑。")


@contextmanager
def _open_checked_file(workspace: Path, runs_root: Path, requested: str) -> Iterator[tuple[Path, BinaryIO, os.stat_result]]:
    """在工作区内打开 requested，提供实际路径、只读句柄和初始状态；退出时核对路径和文件版本。"""
    path = _resolve_file(workspace, runs_root, requested)
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise FileToolError("not_regular_file", "只能读取普通文件，不能读取目录或设备。")
        # POSIX 使用 NONBLOCK 避免路径被替换为 FIFO 后阻塞；再用 fstat 检查实际句柄。
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise FileToolError("not_regular_file", "只能读取普通文件，不能读取目录或设备。")
            # 文件解析与打开之间若发生变化，拒绝本次读取；本地模式不承诺抵御恶意宿主。
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise FileToolError("file_read_error", "读取期间文件发生变化，请重试。")
            if _resolve_file(workspace, runs_root, requested) != path:
                raise FileToolError("file_read_error", "读取期间文件路径发生变化，请重试。")
            yield path, stream, opened
            if (_file_signature(before) != _file_signature(opened)
                    or _file_signature(os.fstat(stream.fileno())) != _file_signature(opened)
                    or _file_signature(path.stat()) != _file_signature(opened)
                    or _resolve_file(workspace, runs_root, requested) != path):
                raise FileToolError("file_changed", "读取期间文件发生变化，请重新读取后编辑。")
    except FileToolError:
        raise
    except FileNotFoundError:
        raise FileToolError("file_not_found", "文件不存在，请检查工作区内的路径。") from None
    except PermissionError:
        raise FileToolError("permission_denied", "没有读取此文件的权限。") from None
    except (OSError, ValueError, OverflowError):
        raise FileToolError("file_read_error", "文件读取失败，请检查文件状态后重试。") from None


def read_file(
    workspace: Path,
    runs_root: Path,
    arguments: ReadFileArguments,
    preview_chars: int,
    max_edit_bytes: int,
) -> dict:
    """读取工作区普通文件的一页UTF-8内容，返回分页位置、大小、完整内容指纹与换行类型。

    workspace/runs_root 限定访问范围；arguments 的 offset/max_bytes 均为字节，
    preview_chars 限制可见字符。大小不超过 max_edit_bytes 的文件额外按有界块计算
    sha256与newline，供edit_file校验和选择原文；更大文件仍可分页，两字段均为None。
    newline为lf/crlf/cr/mixed/none之一，始终描述完整文件而非当前页，不整文件加载。
    """
    with _open_checked_file(workspace, runs_root, arguments.path) as (path, stream, opened):
        if arguments.offset > opened.st_size:
            raise FileToolError("invalid_offset", "offset 超过文件末尾，请使用上页的 next_offset。")
        stream.seek(arguments.offset)
        # 分页与指纹共用句柄；指纹最多扫描编辑预算内的文件，不扩大模型可见内容。
        raw = stream.read(arguments.max_bytes + 4)
        sha256, newline = _inspect_open_file(stream, max_edit_bytes) if opened.st_size <= max_edit_bytes else (None, None)
    if raw and raw[0] & 0xC0 == 0x80 and arguments.offset == 0:
        raise FileToolError("invalid_encoding", "文件不是有效的 UTF-8 文本，请先转换编码。")
    if raw and raw[0] & 0xC0 == 0x80:
        raise FileToolError("invalid_offset", "offset 位于字符内部，请使用上页的 next_offset。")
    content, bytes_read = _decode_page(raw, arguments.max_bytes, preview_chars)
    eof = bytes_read == len(raw)
    return {
        "path": path.relative_to(workspace).as_posix(),
        "content": content,
        "offset": arguments.offset,
        "next_offset": None if eof else arguments.offset + bytes_read,
        "bytes_read": bytes_read,
        "eof": eof,
        "size_bytes": opened.st_size,
        "sha256": sha256,
        "newline": newline,
    }


def read_file_snapshot(workspace: Path, runs_root: Path, requested: str, max_bytes: int) -> bytes:
    """读取并核对 workspace 内 requested 的完整字节；max_bytes 是上限，超限拒绝，无写入。"""
    with _open_checked_file(workspace, runs_root, requested) as (_, stream, opened):
        if opened.st_size > max_bytes:
            raise FileToolError("reference_source_too_large", "源文件超过项目引用的读取上限。")
        raw = stream.read(max_bytes + 1)
        if len(raw) != opened.st_size:
            raise FileToolError("file_changed", "读取期间文件发生变化，请重新预览。")
    return raw

# 搜索参数只表达字面量匹配与有界结果，不接受正则执行或额外宿主路径。
class SearchFilesArguments(DataModel):
    query: str = Field(min_length=1, max_length=4096, description="区分大小写的单行字面量文本，不使用正则。")
    directory: str = Field(default=".", min_length=1, description="工作区内的起始目录。")
    glob: str = Field(default="*", min_length=1, max_length=512, description="相对于起始目录的通配模式；**/ 匹配零或多级目录，例如 **/*.py 包含根目录和嵌套文件。")
    limit: int = Field(default=50, ge=1, le=200, description="最多返回的匹配行数。")

    @field_validator("query")
    @classmethod
    def single_line_query(cls, value: str) -> str:
        """限制查询为非空单行字面量，避免跨行含义和空字节路径混淆。"""
        if any(character in value for character in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\x00"):
            raise ValueError("查询必须为单行字面量")
        return value

    @field_validator("directory", "glob")
    @classmethod
    def valid_search_path(cls, value: str) -> str:
        """拒绝空白路径或模式及空字节，其余路径交由统一边界判断。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("路径或模式无效")
        return value


# 写入参数要求调用方显式允许覆盖，默认仅能创建新文件。
class WriteFileArguments(DataModel):
    path: str = Field(min_length=1, description="工作区内的目标文件路径，父目录可自动创建。")
    content: str = Field(description="要写入的完整 UTF-8 文本。")
    overwrite: bool = Field(default=False, description="仅为 true 时允许原子替换已有普通文件。")

    @field_validator("path")
    @classmethod
    def nonblank_path(cls, value: str) -> str:
        """拒绝空白路径和空字节，保持与读取工具一致的路径输入约束。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("路径不能为空白或包含空字节")
        return value


# 固定扫描上限独立于结果上限，避免无匹配目录或超大文件拖住 Agent 循环。
MAX_SEARCH_FILES = 1000
MAX_SEARCH_ENTRIES = 10000
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 8 * 1024 * 1024


def _search_file_text(boundary: Workspace, path: Path, available_bytes: int) -> tuple[str | None, int, str | None]:
    """有界读取单个普通 UTF-8 文件，返回文本、已读字节和受控跳过原因。"""
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            return None, 0, "not_regular_file"
        if before.st_size > MAX_SEARCH_FILE_BYTES:
            return None, 0, "file_size_limit"
        if before.st_size > available_bytes:
            return None, 0, "total_bytes_limit"
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return None, 0, "not_regular_file"
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return None, 0, "file_changed"
            if boundary.resolve(str(path)) != path:
                return None, 0, "file_changed"
            raw = stream.read(min(MAX_SEARCH_FILE_BYTES, available_bytes) + 1)
        if len(raw) > MAX_SEARCH_FILE_BYTES:
            return None, len(raw), "file_size_limit"
        if len(raw) > available_bytes:
            return None, len(raw), "total_bytes_limit"
        if b"\x00" in raw:
            return None, len(raw), "binary_file"
        try:
            return raw.decode("utf-8", errors="strict"), len(raw), None
        except UnicodeDecodeError:
            return None, len(raw), "invalid_encoding"
    except WorkspaceError:
        return None, 0, "path_changed"
    except PermissionError:
        return None, 0, "permission_denied"
    except (OSError, ValueError, OverflowError):
        return None, 0, "unreadable_file"


def _entry_name(entry: os.DirEntry) -> str:
    """按目录项名称稳定排序，保证同样目录结构下的搜索顺序可复现。"""
    return entry.name


def _glob_matches(relative: str, pattern: str) -> bool:
    """匹配 relative 相对路径与 pattern；独立 ** 段匹配零或多级目录，返回布尔值。

    不含独立 ** 的模式沿用 fnmatchcase，保留普通 * 跨目录及大小写行为。
    递归模式按 POSIX 路径分段，用可达下标迭代避免深层路径或重复 ** 递归爆栈。
    """
    patterns = pattern.split("/")
    if "**" not in patterns:
        return fnmatch.fnmatchcase(relative, pattern)
    parts = relative.split("/")
    positions = {0}  # 已匹配的路径分段数；每次只保留当前模式可到达的下标。
    for segment in patterns:
        if segment == "**":
            positions = set(range(min(positions), len(parts) + 1))
        else:
            positions = {index + 1 for index in positions
                         if index < len(parts) and fnmatch.fnmatchcase(parts[index], segment)}
        if not positions:
            return False
    return len(parts) in positions


def search_files(
    workspace: Path,
    runs_root: Path,
    arguments: SearchFilesArguments,
    preview_chars: int,
) -> dict:
    """递归搜索有界普通 UTF-8 文件，返回相对路径、行号与明确的不完整标记。

    不跟随链接或 junction 目录，记录目录也不会进入扫描。匹配数、扫描目录项、
    文件数、单文件与累计字节均设上限；跳过只报告固定原因的计数而不泄露路径。
    """
    boundary = Workspace(workspace, runs_root)
    start = boundary.resolve(arguments.directory, must_exist=True)
    if not start.is_dir():
        raise FileToolError("not_directory", "搜索起点必须是工作区内的目录。")
    if preview_chars < 1:
        raise FileToolError("invalid_limit", "搜索预览字符限额必须大于零。")
    matches: list[dict] = []
    skipped: Counter[str] = Counter()
    scanned_files = 0
    scanned_entries = 0
    scanned_bytes = 0
    remaining_chars = preview_chars
    truncated = False
    directories = [start]
    stop = False
    while directories and not stop:
        directory = directories.pop()
        try:
            if boundary.resolve(str(directory), must_exist=True) != directory:
                skipped["path_changed"] += 1
                continue
            entries = []
            # 只缓存受全局目录项预算约束的一小段，避免先整目录排序造成无界内存占用。
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if scanned_entries >= MAX_SEARCH_ENTRIES:
                        skipped["entry_limit"] += 1
                        truncated = stop = True
                        break
                    scanned_entries += 1
                    entries.append(entry)
        except WorkspaceError:
            skipped["path_changed"] += 1
            continue
        except PermissionError:
            skipped["permission_denied"] += 1
            continue
        except OSError:
            skipped["unreadable_directory"] += 1
            continue
        children: list[Path] = []
        for entry in sorted(entries, key=_entry_name):
            path = Path(entry.path)
            try:
                if entry.is_symlink() or path.is_junction():
                    skipped["symbolic_link"] += 1
                    continue
                resolved = boundary.resolve(str(path))
                if entry.is_dir(follow_symlinks=False):
                    children.append(resolved)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped["not_regular_file"] += 1
                    continue
                relative = path.relative_to(start).as_posix()
                if not _glob_matches(relative, arguments.glob):
                    continue
                if scanned_files >= MAX_SEARCH_FILES:
                    skipped["file_count_limit"] += 1
                    truncated = stop = True
                    break
                if scanned_bytes >= MAX_SEARCH_TOTAL_BYTES:
                    skipped["total_bytes_limit"] += 1
                    truncated = stop = True
                    break
                scanned_files += 1
                text, count, reason = _search_file_text(
                    boundary, resolved, MAX_SEARCH_TOTAL_BYTES - scanned_bytes
                )
                scanned_bytes += count
                if reason is not None:
                    skipped[reason] += 1
                    if reason in {"file_size_limit", "total_bytes_limit"}:
                        truncated = True
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if arguments.query not in line:
                        continue
                    visible = line[:remaining_chars]
                    clipped = len(visible) < len(line)
                    match = {
                        "path": resolved.relative_to(boundary.workspace).as_posix(),
                        "line_number": line_number,
                        "line": visible,
                    }
                    if clipped:
                        match["line_truncated"] = True
                    matches.append(match)
                    remaining_chars -= len(visible)
                    if clipped or remaining_chars == 0 or len(matches) >= arguments.limit:
                        truncated = stop = True
                        break
                if stop:
                    break
            except WorkspaceError as error:
                reason = "run_records" if error.code == "run_records_forbidden" else "outside_workspace"
                skipped[reason] += 1
            except PermissionError:
                skipped["permission_denied"] += 1
            except (OSError, ValueError):
                skipped["unreadable_file"] += 1
        directories.extend(reversed(children))
    return {
        "matches": matches,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "skipped_count": sum(skipped.values()),
        "skipped": dict(sorted(skipped.items())),
        "truncated": truncated,
    }


def _ensure_write_parent(boundary: Workspace, path: Path) -> None:
    """逐级创建目标父目录，每步重验边界并拒绝变化后的链接或非目录组件。"""
    current = boundary.workspace
    for component in path.parent.relative_to(boundary.workspace).parts:
        current = current / component
        if boundary.resolve(str(current)) != current:
            raise FileToolError("file_write_error", "写入期间父目录路径发生变化，请重试。")
        current.mkdir(exist_ok=True)
        if boundary.resolve(str(current), must_exist=True) != current or not current.is_dir():
            raise FileToolError("file_write_error", "目标父路径不是可写目录。")


def write_file(
    workspace: Path,
    runs_root: Path,
    arguments: WriteFileArguments,
    max_bytes: int,
) -> dict:
    """将完整 UTF-8 内容先写入同目录临时文件，再原子提交为目标文件。

    默认使用原子硬链接创建而非先判断再替换，避免竞态覆盖现有文件。显式覆盖才用
    replace；任何提交前失败都保留原目标，临时文件在结束时尽力清理。
    """
    boundary = Workspace(workspace, runs_root)
    path = boundary.resolve(arguments.path)
    try:
        raw = arguments.content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise FileToolError("invalid_encoding", "内容不能编码为有效 UTF-8 文本。") from None
    if len(raw) > max_bytes:
        raise FileToolError("file_size_limit", "文件内容超过本次运行允许的写入字节数。")
    temporary: Path | None = None
    try:
        existed = path.exists()
        if existed and not path.is_file():
            raise FileToolError("not_regular_file", "只能写入普通文件，不能替换目录或设备。")
        if existed and not arguments.overwrite:
            raise FileToolError("file_exists", "目标文件已存在；如需替换，请显式设置 overwrite 为 true。")
        _ensure_write_parent(boundary, path)
        if boundary.resolve(arguments.path) != path:
            raise FileToolError("file_write_error", "写入期间文件路径发生变化，请重试。")
        descriptor, name = tempfile.mkstemp(prefix=".harness-write-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if boundary.resolve(arguments.path) != path or path.parent.resolve() != path.parent:
            raise FileToolError("file_write_error", "写入期间文件路径发生变化，请重试。")
        existed = path.exists()
        if existed and not path.is_file():
            raise FileToolError("not_regular_file", "只能写入普通文件，不能替换目录或设备。")
        if arguments.overwrite:
            os.replace(temporary, path)
        else:
            # link 的目标创建是排他的；不支持硬链接的文件系统安全失败，绝不退回无条件覆盖。
            os.link(temporary, path)
        return {
            "path": path.relative_to(boundary.workspace).as_posix(),
            "bytes_written": len(raw),
            "created": not existed,
            "overwritten": existed,
        }
    except FileToolError:
        raise
    except FileExistsError:
        raise FileToolError("file_exists", "目标文件已存在；如需替换，请显式设置 overwrite 为 true。") from None
    except PermissionError:
        raise FileToolError("permission_denied", "没有写入此文件的权限。") from None
    except (OSError, ValueError, OverflowError):
        raise FileToolError("file_write_error", "文件写入失败；原目标未被本次失败提交替换。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 不能用清理异常覆盖真实提交结果，且不将临时路径或系统异常暴露给模型。
                pass


class EditFileArguments(DataModel):
    """已有文件的单片段替换；仅统一LF/CRLF等价，版本与唯一匹配通过后才允许提交。"""

    path: str = Field(min_length=1, description="工作区内已存在的普通 UTF-8 文件。")
    old_text: str = Field(min_length=1, description="区分大小写的完整旧片段，必须唯一匹配。统一LF/CRLF文件允许这两种换行等价；混合换行或仅CR文件须保留原换行。")
    new_text: str = Field(description="替换文本，空字符串表示删除；统一LF/CRLF文件按原格式写入，混合换行、仅CR或无换行文件按传入文本写入。不改缩进或其他内容。")
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="最近 read_file 或成功 edit_file 返回的完整文件 sha256；不可使用历史证据中的过期值。")

    @field_validator("path")
    @classmethod
    def nonblank_path(cls, value: str) -> str:
        """校验请求路径；空白/空字节抛 ValueError，其余原样交给工作区边界解析。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("路径不能为空白或包含空字节")
        return value


def _edit_source(boundary: Workspace, requested: str, path: Path, expected: str, max_bytes: int) -> tuple[bytes, os.stat_result]:
    """读取并核对待编辑文件，返回原字节和状态；版本不符时抛 file_changed。

    boundary/requested/path 固定允许的路径；expected 为完整 SHA-256；max_bytes 是
    原文件大小上限。句柄前后及路径对象都核对，不跟随外部链接，不创建任何文件。
    """
    if boundary.resolve(requested) != path:
        raise FileToolError("file_changed", "文件路径发生变化，请重新读取后编辑。")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise FileToolError("not_regular_file", "只能编辑已有普通文件，不能编辑目录或设备。")
    if before.st_size > max_bytes:
        raise FileToolError("file_size_limit", "原文件超过文件编辑字节上限，不能局部编辑。")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb", buffering=0) as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise FileToolError("not_regular_file", "只能编辑已有普通文件，不能编辑目录或设备。")
        if _file_signature(before) != _file_signature(opened):
            raise FileToolError("file_changed", "文件对象发生变化，请重新读取后编辑。")
        raw = stream.read(max_bytes + 1)
        if (len(raw) > max_bytes
                or _file_signature(os.fstat(stream.fileno())) != _file_signature(opened)
                or _file_signature(path.stat()) != _file_signature(opened)
                or boundary.resolve(requested) != path
                or hashlib.sha256(raw).hexdigest() != expected):
            raise FileToolError("file_changed", "文件已在读取后变化，请重新 read_file，再决定如何编辑。")
    return raw, opened


def _edit_fragments(text: str, old: str, new: str) -> tuple[str, str, NewlineStyle]:
    """按text整份原文的换行处理old/new片段，返回匹配片段、替换片段及原换行类型。

    仅统一LF/CRLF文件执行等价转换，孤立CR抛edit_newline_mismatch，避免截断CRLF；
    混合、仅CR和无换行文件按原片段精确处理。不改原文、空格、大小写或Unicode字符。
    """
    style = _newline_style(text.count("\n"), text.count("\r"), text.count("\r\n"))
    if style not in {"lf", "crlf"}:
        return old, new, style
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if "\r" in old or "\r" in new:
        raise FileToolError("edit_newline_mismatch", "片段含有独立CR，请使用完整LF或CRLF换行；原文件未修改。")
    if style == "crlf":
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    return old, new, style


def edit_file(workspace: Path, runs_root: Path, arguments: EditFileArguments, max_bytes: int) -> dict:
    """按版本校验替换已有文件的唯一片段，返回新指纹、替换数量和文件字节数。

    workspace/runs_root 固定路径边界；arguments 提供原片段、新片段及最近读取指纹。
    max_bytes同时约束原文件和结果大小；统一LF/CRLF文件转换请求片段的换行后匹配，
    混合、仅CR和无换行文件精确匹配。原BOM及未匹配内容保持，统一换行格式保持；
    不存在、歧义、版本冲突或失败提交均保留原目标。提交前再次核对版本和路径，
    然后同目录原子替换并保留权限位；这是乐观并发检查，不是跨进程文件锁。
    """
    boundary = Workspace(workspace, runs_root)
    path = boundary.resolve(arguments.path)
    temporary: Path | None = None
    try:
        raw, original = _edit_source(boundary, arguments.path, path, arguments.expected_sha256, max_bytes)
        if b"\x00" in raw or "\x00" in arguments.new_text:
            raise FileToolError("binary_file", "局部编辑只支持不含空字节的 UTF-8 文本。")
        try:
            text = raw.decode("utf-8", errors="strict")
            arguments.old_text.encode("utf-8", errors="strict")
            arguments.new_text.encode("utf-8", errors="strict")
        except (UnicodeDecodeError, UnicodeEncodeError):
            raise FileToolError("invalid_encoding", "原文件和替换片段必须是有效 UTF-8 文本。") from None
        old, new, newline = _edit_fragments(text, arguments.old_text, arguments.new_text)
        first = text.find(old)
        if first < 0:
            hint = "文件包含混合换行，old_text必须保留读取结果中的原换行。" if newline == "mixed" else "请重新read_file并核对原文、大小写和空白。"
            raise FileToolError("edit_not_found", "没有找到原片段；" + hint)
        # 从 first+1 继续检查，重复且重叠的片段也属于歧义，不能只用 str.count。
        if text.find(old, first + 1) >= 0:
            raise FileToolError("edit_ambiguous", "原片段匹配多处，请包含更多上下文使其唯一。")
        updated = text[:first] + new + text[first + len(old):]
        # BOM属于原文件编码格式，整段替换时模型省略BOM也不应把它删除。
        if text.startswith("\ufeff") and not updated.startswith("\ufeff"):
            updated = "\ufeff" + updated
        result = updated.encode("utf-8")
        if result == raw:
            raise FileToolError("edit_no_change", "替换后内容没有变化，原文件未修改。")
        if len(result) > max_bytes:
            raise FileToolError("file_size_limit", "编辑结果超过文件写入字节上限。")
        descriptor, name = tempfile.mkstemp(prefix=".harness-edit-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        # 临时文件落盘期间用户仍可能编辑目标；提交前重新读版本，避免使用过期原文。
        _, current = _edit_source(boundary, arguments.path, path, arguments.expected_sha256, max_bytes)
        if _file_signature(current) != _file_signature(original):
            raise FileToolError("file_changed", "文件对象发生变化，请重新读取后编辑。")
        os.replace(temporary, path)
        return {"path": path.relative_to(boundary.workspace).as_posix(), "replacements": 1,
                "bytes_written": len(result), "sha256": hashlib.sha256(result).hexdigest()}
    except FileToolError:
        raise
    except FileNotFoundError:
        raise FileToolError("file_not_found", "待编辑文件不存在，请重新读取确认。") from None
    except PermissionError:
        raise FileToolError("permission_denied", "没有编辑此文件的权限。") from None
    except (OSError, ValueError, OverflowError):
        raise FileToolError("file_edit_error", "文件编辑提交失败，请重新读取并检查文件状态。") from None
    finally:
        if temporary is not None:
            try:
                if temporary.exists():
                    # Windows 不允许删除只读临时文件；只调整本次拥有的临时文件，绝不改原目标。
                    temporary.chmod(temporary.stat().st_mode | stat.S_IWRITE)
                temporary.unlink(missing_ok=True)
            except OSError:
                # 清理错误不覆盖真实提交结果；不把临时路径回传模型。
                pass
