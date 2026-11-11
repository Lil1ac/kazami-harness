"""统一管理宿主登记目录和从页面导入的技能包。"""

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from uuid import uuid4

from agent_harness.context.skills import SkillCatalog, SkillError, _linked, _valid_name
from agent_harness.schemas import SkillOption, SkillResourceSnapshot, SkillSnapshot
from agent_harness.work.tasks import WorkStore, encode


MAX_PACKAGE_FILES = 64  # 每个技能包含正文最多保留的文本文件数。
MAX_PACKAGE_BYTES = 512 * 1024  # 单个导入包的 UTF-8 原始内容总量上限。
MAX_RESOURCE_ENTRIES = 256  # 单个外部技能最多检查的资源目录项。


def resource_path(value: str) -> bool:
    """判断 value 是否为技能资源的可移植相对路径，不访问文件系统。"""
    path = PurePosixPath(value)
    return (len(value) <= 240 and path.as_posix() == value and 2 <= len(path.parts) <= 9
            and path.parts[0] in {"references", "assets"}
            and all(part not in {".", ".."} and not part.endswith((".", " "))
                    and not PureWindowsPath(part).is_reserved()
                    and not any(ord(char) < 32 or char in '\\:<>"|?*' for char in part)
                    for part in path.parts))


def _digest(value: object) -> str:
    """返回 value 稳定 JSON 的 SHA-256，用于包和管理版本标识。"""
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


class SkillLibrary:
    """在同一数据目录复用技能目录、管理状态和加载校验。"""

    def __init__(self, root: Path, roots: list[Path]) -> None:
        """绑定运行根 root 和宿主登记 roots，构造时不读文件或写库。"""
        self.host = SkillCatalog(roots)  # TOML 登记的技能文件校验器。
        self.store = WorkStore(root)  # 受保护的本机 SQLite 存储。

    @contextmanager
    def _read(self):
        """返回现有数据目录事务，库尚不存在时 yield None，不为查询创建新库。"""
        if self.store.path.exists():
            with self.store.transaction() as db:
                yield db
        else:
            yield None

    def _options(self, db) -> list[SkillOption]:
        """从当前 db 和宿主目录组装选项，正文只在实际加载或预览时读取。"""
        host = self.host.discover()
        managed = [] if db is None else [SkillOption.model_validate_json(row[0]) for row in db.execute(
            "SELECT metadata FROM skill_packages ORDER BY name")]
        overrides = {} if db is None else {row[0]: (bool(row[1]), row[2]) for row in db.execute(
            "SELECT id, enabled, revision FROM skill_preferences")}
        for item in host:
            item.id = "host-" + _digest(item.source)
            item.enabled, generation = overrides.get(item.id, (True, ""))
            item.revision = _digest([item.revision, generation])
        options = [*host, *managed]
        counts = Counter(item.name for item in options if item.name)
        for item in options:
            if counts[item.name] > 1:
                item.available = False
                item.error_code = "skill_duplicate_name"
                item.error_message = "多个技能目录使用相同名称，请先消除冲突。"
            elif not item.enabled and item.available:
                item.available = False
                item.error_code = "skill_disabled"
                item.error_message = "技能已停用。"
        return sorted(options, key=lambda item: (item.name, item.source))

    def discover(self) -> list[SkillOption]:
        """列出统一技能目录及诊断，不启用技能或加载资源正文。"""
        with self._read() as db:
            return self._options(db)

    def _option(self, db, identifier: str) -> SkillOption:
        """从当前 db 查找管理标识 identifier，缺失抛 SkillError。"""
        item = next((item for item in self._options(db) if item.id == identifier), None)
        if item is None:
            raise SkillError("skill_not_found", "该技能已移除或不在宿主登记目录中。")
        return item

    def _external_resources(self, source: Path) -> dict[str, str]:
        """枚举 source 所属技能的安全资源，返回路径到文件身份的映射，不读取资料正文。"""
        root = source.parent
        pending = [root / "references", root / "assets"]
        result = {}
        scanned = 0
        while pending:
            directory = pending.pop()
            try:
                before = self.host._checked_path(directory, directory=True)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_RESOURCE_ENTRIES:
                            raise SkillError("skill_resource_limit", "技能资源目录项超过扫描上限。")
                        path = Path(entry.path)
                        relative = path.relative_to(root).as_posix()
                        info = entry.stat(follow_symlinks=False)
                        if not resource_path(relative) or _linked(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(path)
                        elif stat.S_ISREG(info.st_mode) and info.st_size <= self.host.MAX_FILE_BYTES:
                            # Windows 的 DirEntry.stat 可能省略文件身份，统一使用与实际读取相同的核验结果。
                            result[relative] = repr(self.host._checked_path(path)[-1])
                if self.host._checked_path(directory, directory=True) != before:
                    raise SkillError("skill_file_changed", "扫描期间技能资源目录发生替换。")
            except FileNotFoundError:
                if directory.parent != root:
                    raise SkillError("skill_file_changed", "扫描期间技能资源目录已移除。") from None
            except OSError:
                raise SkillError("skill_unreadable", "技能资源目录无法读取。") from None
        if len(result) >= MAX_PACKAGE_FILES or len({path.casefold() for path in result}) != len(result):
            raise SkillError("skill_resource_limit", "技能资源数量超限或存在大小写冲突。")
        return dict(sorted(result.items()))

    def _snapshot(self, db, item: SkillOption, *, resources: bool) -> SkillSnapshot:
        """读取目录项 item 的完整正文，可选固定附属资源身份，返回运行快照。"""
        if item.managed:
            files = json.loads(db.execute("SELECT files FROM skill_packages WHERE name=?", (item.name,)).fetchone()[0])
            content = files["SKILL.md"]
            versions = {key: _digest(value) for key, value in files.items() if key != "SKILL.md"} if resources else {}
            version = _digest(files)
        else:
            path = Path(item.source)
            content, identity = self.host.read_text(path)
            generation = db.execute("SELECT revision FROM skill_preferences WHERE id=?", (item.id,)).fetchone() if db is not None else None
            if item.revision != _digest([repr(identity), generation[0] if generation else ""]):
                raise SkillError("skill_file_changed", "技能正文发生变化，请重新读取目录。", item.name)
            self.host._metadata(content, item.name)
            versions = self._external_resources(path) if resources else {}
            version = _digest([content, versions])
        raw = content.encode("utf-8")
        return SkillSnapshot(name=item.name, description=item.description, source=item.source, content=content,
                             sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), version=version, resources=versions)

    def load(self, names: list[str], *, resources: bool = False, expected: str | None = None) -> list[SkillSnapshot]:
        """按 names 顺序冻结正文和可选资源。expected 绑定单项自动选择时看到的目录版本。"""
        if not names:
            return []
        if len(names) > self.host.MAX_SELECTED:
            raise SkillError("skill_selection_limit", "本轮选择的技能超过数量上限。")
        for name in names:
            if not _valid_name(name):
                raise SkillError("skill_invalid_name", "所选技能名称不符合规范。", name)
        if len(set(names)) != len(names):
            raise SkillError("skill_duplicate_name", "同一技能不能在一轮中重复选择。")
        with self._read() as db:
            options = {item.name: item for item in self._options(db)}
            result = []
            for name in names:
                item = options.get(name)
                if item is None:
                    if options.get("") and options[""].error_code == "skill_catalog_limit":
                        raise SkillError("skill_catalog_limit", "技能集合目录项超过扫描上限。", name)
                    raise SkillError("skill_not_found", "所选技能不存在或所在目录无法读取。", name)
                if not item.available:
                    raise SkillError(item.error_code or "skill_unavailable", item.error_message or "技能不可用。", name)
                if expected is not None and (item.revision != expected or item.manual_only):
                    raise SkillError("skill_file_changed", "技能版本或调用方式已变化，请在下一轮重试。", name)
                try:
                    result.append(self._snapshot(db, item, resources=resources))
                except SkillError as error:
                    raise SkillError(error.code, error.message, name) from None
            if sum(item.size_bytes for item in result) > self.host.MAX_TOTAL_BYTES:
                raise SkillError("skill_total_too_large", "所选技能正文合计超过字节上限。")
            return result

    def preview(self, identifier: str) -> SkillSnapshot:
        """预览 identifier 的正文与资源列表，允许查看已停用技能。"""
        with self._read() as db:
            item = self._option(db, identifier)
            if not item.available and item.error_code != "skill_disabled":
                raise SkillError(item.error_code or "skill_unavailable", item.error_message or "技能不可用。", item.name)
            return self._snapshot(db, item, resources=True)

    def import_package(self, name: str, files: list[tuple[str, str]], expected: str | None) -> SkillOption:
        """校验 name 和文本 files 后新增停用的副本。expected 非空表示替换该已审阅版本。"""
        if not _valid_name(name) or not 1 <= len(files) <= MAX_PACKAGE_FILES:
            raise SkillError("skill_invalid_package", "技能目录名或包内文件数量不符合要求。", name)
        contents = dict(files)
        if len({path.casefold() for path, _ in files}) != len(files) or "SKILL.md" not in contents or any(
                path != "SKILL.md" and not resource_path(path) for path in contents):
            raise SkillError("skill_invalid_package", "技能包缺少 SKILL.md，或包含重复及越界文件。", name)
        try:
            sizes = [len(text.encode("utf-8")) for text in contents.values()]
        except UnicodeEncodeError:
            raise SkillError("skill_invalid_encoding", "技能包必须包含有效的 UTF-8 文本。", name) from None
        if any(size > self.host.MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_PACKAGE_BYTES or any("\0" in text for text in contents.values()):
            raise SkillError("skill_invalid_package", "技能包含非文本内容或超过字节上限。", name)
        metadata = self.host._metadata(contents["SKILL.md"], name)
        with self.store.transaction() as db:
            options = self._options(db)
            if any(item.name == name and not item.managed for item in options):
                raise SkillError("skill_duplicate_name", "该名称已由宿主目录登记，请使用其他名称。", name)
            old = next((item for item in options if item.name == name and item.managed), None)
            if (old.revision if old else None) != expected:
                raise SkillError("skill_stale", "技能版本已变化或同名副本已存在，请刷新后确认。", name)
            if old is None and sum(item.managed for item in options) >= self.host.MAX_ENTRIES:
                raise SkillError("skill_catalog_limit", "已导入的技能数量达到上限。")
            enabled = old.enabled if old else False
            item = SkillOption(id=f"managed-{name}", name=name, source=f"受管技能/{name}/SKILL.md",
                               description=metadata["description"], managed=True, enabled=enabled, revision=uuid4().hex,
                               manual_only=metadata.get("disable-model-invocation", False))
            db.execute("INSERT OR REPLACE INTO skill_packages VALUES (?,?,?)", (name, item.model_dump_json(), encode(contents)))
            if enabled:
                return item
            return item.model_copy(update={"available": False, "error_code": "skill_disabled", "error_message": "技能已停用。"})

    def change(self, identifier: str, expected: str, action: str) -> None:
        """按 expected 版本启用、停用或移除 identifier，移除只作用于受管副本。"""
        with self.store.transaction() as db:
            item = self._option(db, identifier)
            if item.revision != expected:
                raise SkillError("skill_stale", "技能已变化，请刷新后重新操作。", item.name)
            if action == "remove":
                if not item.managed:
                    raise SkillError("skill_external", "宿主登记的技能可通过运行配置停用。", item.name)
                db.execute("DELETE FROM skill_packages WHERE name=?", (item.name,))
                return
            if action == "enable" and item.error_code not in {None, "skill_disabled"}:
                raise SkillError(item.error_code, item.error_message or "技能不可启用。", item.name)
            if action == "enable":
                self._snapshot(db, item, resources=False)
            if item.managed:
                item.enabled, item.revision = action == "enable", uuid4().hex
                item.available, item.error_code, item.error_message = True, None, None
                db.execute("UPDATE skill_packages SET metadata=? WHERE name=?", (item.model_dump_json(), item.name))
            else:
                db.execute("INSERT OR REPLACE INTO skill_preferences VALUES (?,?,?)", (identifier, action == "enable", uuid4().hex))

    def read_resource(self, skill: SkillSnapshot, path: str) -> SkillResourceSnapshot:
        """仅按 skill 快照中允许的 path 读取固定版本，返回完整资料及指纹。"""
        if not resource_path(path) or path not in skill.resources:
            raise SkillError("skill_resource_denied", "只能读取已启用技能中登记的资料。", skill.name)
        if skill.source.startswith("受管技能/"):
            with self._read() as db:
                row = db.execute("SELECT files FROM skill_packages WHERE name=?", (skill.name,)).fetchone() if db is not None else None
                files = json.loads(row[0]) if row is not None else {}
            if _digest(files) != skill.version:
                raise SkillError("skill_file_changed", "技能包已更新或移除，请在下一轮读取新资料。", skill.name)
            content = files[path]
        else:
            target = Path(skill.source).parent.joinpath(*PurePosixPath(path).parts)
            content, identity = self.host.read_text(target)
            if repr(identity) != skill.resources[path] or "\0" in content:
                raise SkillError("skill_file_changed", "技能资料已变化或不是可读取的文本。", skill.name)
        raw = content.encode("utf-8")
        return SkillResourceSnapshot(name=skill.name, path=path, content=content,
                                     sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
