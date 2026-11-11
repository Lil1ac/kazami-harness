"""安全扫描宿主登记目录，读取技能文件的头部或正文。"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

import yaml

from agent_harness.schemas import SkillOption


_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # 名称同时用作子目录名，不接受路径分隔符。
_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools", "disable-model-invocation"}  # 支持的 frontmatter 字段。


class SkillError(Exception):
    """向调用方提供受控原因；code/message 不包含底层异常或技能文件原文。"""

    def __init__(self, code: str, message: str, name: str | None = None) -> None:
        """固定错误码、中文说明和可选技能名；不访问文件，返回异常实例。"""
        super().__init__(message)
        self.code = code  # 供 API 与运行记录识别的稳定原因码。
        self.message = message  # 可安全展示的固定中文解释。
        self.name = name  # 技能选择失败时的名称；目录级故障为 None。


@dataclass(frozen=True)
class _Candidate:
    """一次有界扫描找到的候选项；错误项保留在目录中供界面解释。"""

    name: str  # 子目录名；空串表示整个配置根目录不可扫描。
    source: Path  # 候选 SKILL.md 的绝对路径，目录级错误则是配置根目录。
    error: SkillError | None = None  # 扫描期间发现的问题，None 表示仍需检查文件头部。


def _valid_name(name: object) -> bool:
    """检查 name 是否为规范允许的 1 至 64 字符名称，返回布尔值，无文件副作用。"""
    return isinstance(name, str) and 1 <= len(name) <= 64 and _NAME.fullmatch(name) is not None


def _linked(info: os.stat_result) -> bool:
    """识别 lstat 结果中的符号链接和 Windows 重解析点，返回是否拒绝访问。"""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _signature(info: os.stat_result) -> tuple:
    """将文件状态转换为身份及内容状态元组，供读取前后核验，时间均为纳秒。"""
    # Windows 的 lstat/fstat 对 ctime 语义存在差异；统一比较对象身份、模式、大小和修改时间。
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


class SkillCatalog:
    """核验宿主登记集合中的文件，供统一技能库读取和解析。"""

    MAX_ROOTS = 8  # 一个目录集合配置最多 8 个根目录。
    MAX_ENTRIES = 256  # 所有根目录合计最多检查 256 个直接目录项，不递归。
    MAX_SELECTED = 8  # 一轮最多显式加载 8 个技能。
    MAX_FILE_BYTES = 64 * 1024  # 单个 SKILL.md 最大 64 KiB；超过时整项拒绝。
    MAX_TOTAL_BYTES = 256 * 1024  # 一次选择的原始 UTF-8 文件总计最大 256 KiB。

    def __init__(self, roots: list[Path]) -> None:
        """复制宿主配置 roots；最多 8 项，只做词法绝对化，不扫描、不创建或读取文件。"""
        if len(roots) > self.MAX_ROOTS:
            raise SkillError("skill_catalog_limit", "技能集合根目录超过数量上限。")
        self.roots = tuple(Path(os.path.abspath(root)) for root in roots)  # 保留配置顺序的只读根目录集合。

    def _checked_path(self, path: Path, *, directory: bool = False) -> tuple:
        """核验 path 全路径无链接，且目标为目录或普通文件；返回各目录身份和目标状态。

        directory 为 True 时目标必须是目录，默认必须为普通文件；链接或特殊类型抛
        SkillError，底层 OSError 由上层转换，避免把系统异常和任意路径泄露到错误说明。
        """
        chain = []
        for item in (*reversed(path.parents), path):
            info = item.lstat()
            if _linked(info):
                raise SkillError("skill_path_unsafe", "技能路径包含链接或重解析点。")
            is_directory = item != path or directory
            if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
                raise SkillError("skill_path_unsafe", "技能必须位于普通目录，且正文必须是普通文件。")
            # 目录只比较身份；无关文件的变化不应使同一技能读取失败。
            chain.append((info.st_dev, info.st_ino, info.st_mode) if is_directory else _signature(info))
        return tuple(chain)

    def _scan(self) -> list[_Candidate]:
        """有界枚举直接子目录，返回候选项及明确错误；不读取未选正文，不递归目录。"""
        candidates = []
        seen_entries = 0
        for root in self.roots:
            try:
                before = self._checked_path(root, directory=True)
                with os.scandir(root) as entries:
                    for entry in entries:
                        seen_entries += 1
                        if seen_entries > self.MAX_ENTRIES:
                            raise SkillError("skill_catalog_limit", "技能集合目录项超过扫描上限。")
                        path = root / entry.name
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode):
                                continue  # 集合目录中的 README 等普通文件不是候选技能。
                            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                                raise SkillError("skill_path_unsafe", "技能目录不能使用链接或特殊文件。")
                            if not _valid_name(entry.name):
                                raise SkillError("skill_invalid_name", "技能目录名称不符合规范。")
                            candidates.append(_Candidate(entry.name, path / "SKILL.md"))
                        except SkillError as error:
                            candidates.append(_Candidate(entry.name, path / "SKILL.md", error))
                        except OSError:
                            candidates.append(_Candidate(entry.name, path / "SKILL.md",
                                SkillError("skill_unreadable", "技能目录无法读取。")))
                if self._checked_path(root, directory=True) != before:
                    raise SkillError("skill_file_changed", "扫描期间技能集合目录发生替换。")
            except SkillError as error:
                if error.code == "skill_catalog_limit":
                    raise
                candidates.append(_Candidate("", root, error))
            except (OSError, ValueError):
                candidates.append(_Candidate("", root, SkillError("skill_unreadable", "技能集合目录无法读取。")))
        return sorted(candidates, key=lambda item: (item.name, str(item.source)))

    def read_text(self, path: Path, *, header_only: bool = False) -> tuple[str, tuple]:
        """读取 path 的完整 UTF-8 文本或仅头部，返回文本及已核对的文件身份。"""
        try:
            before = self._checked_path(path)
            if before[-1][3] > self.MAX_FILE_BYTES:
                raise SkillError("skill_file_too_large", "技能文件超过单文件字节上限。")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or _signature(opened) != before[-1]:
                    raise SkillError("skill_file_changed", "读取期间技能文件发生变化。")
                if header_only:
                    # 只解析 frontmatter，正文错误留到预览或真正启用时检查。
                    lines = [stream.readline(self.MAX_FILE_BYTES + 1)]
                    remaining = self.MAX_FILE_BYTES + 1 - len(lines[0])
                    while lines[0].rstrip(b"\r\n") == b"---" and remaining > 0:
                        line = stream.readline(remaining)
                        if not line:
                            break
                        lines.append(line)
                        remaining -= len(line)
                        if line.rstrip(b"\r\n") == b"---":
                            break
                    raw = b"".join(lines)
                else:
                    raw = stream.read(self.MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
            if len(raw) > self.MAX_FILE_BYTES:
                raise SkillError("skill_file_too_large", "技能文件超过单文件字节上限。")
            if (not header_only and len(raw) != after.st_size) or _signature(after) != before[-1] or self._checked_path(path) != before:
                raise SkillError("skill_file_changed", "读取期间技能文件或所在目录发生变化。")
            return raw.decode("utf-8"), before[-1]
        except UnicodeDecodeError:
            raise SkillError("skill_invalid_encoding", "技能文件必须是完整的 UTF-8 文本。") from None
        except (OSError, ValueError):
            raise SkillError("skill_unreadable", "技能文件缺失、无法读取或已发生变化。") from None

    @staticmethod
    def _metadata(content: str, name: str) -> dict:
        """校验完整 content 的 YAML frontmatter，返回元数据；不解释正文或执行附属资源。"""
        lines = content.splitlines()
        try:
            if not lines or lines[0] != "---":
                raise ValueError("missing frontmatter")
            end = lines.index("---", 1)
            data = yaml.safe_load("\n".join(lines[1:end]))
            if not isinstance(data, dict) or not all(isinstance(key, str) for key in data) or data.keys() - _FIELDS:
                raise ValueError("metadata mapping")
            if not _valid_name(data.get("name")) or data["name"] != name:
                raise ValueError("name mismatch")
            description = data.get("description")
            if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                raise ValueError("description")
            for key in ("license", "compatibility", "allowed-tools"):
                if key in data and not isinstance(data[key], str):
                    raise ValueError("optional metadata")
            if "compatibility" in data and not 1 <= len(data["compatibility"]) <= 500:
                raise ValueError("compatibility")
            if "metadata" in data and (not isinstance(data["metadata"], dict) or
                    not all(isinstance(key, str) and isinstance(value, str) for key, value in data["metadata"].items())):
                raise ValueError("metadata values")
            if "disable-model-invocation" in data and type(data["disable-model-invocation"]) is not bool:
                raise ValueError("invocation policy")
            # allowed-tools 仅作为原文保存；权限始终由宿主 Runner/工具注册表控制。
            return data
        except (ValueError, TypeError, RecursionError, yaml.YAMLError):
            raise SkillError("skill_invalid_metadata", "技能 frontmatter 不符合 Agent Skills 规范。", name) from None

    def discover(self) -> list[SkillOption]:
        """返回可选技能与损坏项说明；全局扫描超限返回单个不可用项，不静默截断列表。"""
        try:
            candidates = self._scan()
        except SkillError as error:
            return [SkillOption(name="", description="", source="", available=False,
                error_code=error.code, error_message=error.message)]
        options = []
        for candidate in candidates:
            try:
                if candidate.error is not None:
                    raise candidate.error
                header, signature = self.read_text(candidate.source, header_only=True)
                metadata = self._metadata(header, candidate.name)
                options.append(SkillOption(name=candidate.name, description=metadata["description"], source=str(candidate.source),
                    revision=repr(signature), manual_only=metadata.get("disable-model-invocation", False)))
            except SkillError as error:
                options.append(SkillOption(name=candidate.name, description="", source=str(candidate.source),
                    available=False, error_code=error.code, error_message=error.message))
        return options
