"""登记用户明确打开的本机目录，持久保存最近项目及可选测试设置。"""

from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator
from typing import Literal

from ..config import ConfigurationError
from ..execution import prepare_python_execution
from ..processes import _child_environment
from ..projects import ProjectPreset
from ..schemas import DataModel, UtcDatetime
from .config import WebConfig, WebWorkspace, prepare_project_test


class CatalogError(Exception):
    """提供项目登记的受控原因，不回显系统异常或文件正文。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        """保存 code、中文 message 及 HTTP status；构造无文件副作用。"""
        super().__init__(message)
        self.code = code  # 页面识别的稳定原因。
        self.message = message  # 可直接展示的中文说明。
        self.status = status  # 用户输入错误400，记录故障503。


class LocalProject(DataModel):
    """用户已明确选择的目录；登记本身不创建工作树或调用模型。"""

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")  # 路径去重后的稳定编号。
    label: str = Field(min_length=1, max_length=100)  # 默认采用目录名。
    path: Path  # 选择时固定的规范绝对目录。
    kind: Literal["project", "workspace"]  # Git 根目录或普通文件目录。
    last_opened_at: UtcDatetime  # 用户最后明确打开时间，读取页面不更新。
    test_argv: list[str] = Field(default_factory=list)  # 空列表表示未配置独立项目测试。
    test_timeout_seconds: float = Field(default=30.0, gt=0, le=3600, allow_inf_nan=False)  # 测试秒级上限。
    python_executable: str | None = Field(default=None, max_length=4096)  # 项目解释器；None 使用 PATH，随明确保存持久化。
    python_paths: list[str] | None = None  # 共享源码目录；None 自动根/src，[] 仅根目录，按每轮实际工作区解析。


class CatalogState(DataModel):
    """有界的本地项目登记文件；列表只存路径与公开设置。"""

    version: Literal[1] = 1  # 明确存储版本，未知版本拒绝读取。
    entries: list[LocalProject] = Field(default_factory=list, max_length=100)  # 最多100个最近目录。


    @model_validator(mode="after")
    def unique_sources(self) -> "CatalogState":
        """核验条目ID和规范绝对路径唯一，返回自身；读写共用校验，拒绝无法重新读取的记录。"""
        identifiers = [entry.id for entry in self.entries]
        paths = [os.path.normcase(str(entry.path)) for entry in self.entries]
        if len(set(identifiers)) != len(identifiers) or len(set(paths)) != len(paths):
            raise ValueError("目录编号和路径不能重复")
        if any(not entry.path.is_absolute() for entry in self.entries):
            raise ValueError("目录记录必须使用绝对路径")
        return self


def _split_test_command(command: str) -> list[str]:
    """将用户测试 command 解析为参数；空白返回空列表，不执行命令，Windows遵循系统引号规则。"""
    if not command.strip():
        return []
    if os.name != "nt":
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes

    # shlex的非POSIX模式不能还原list2cmdline产生的转义引号；系统API与页面回显保持一致。
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    split = shell.CommandLineToArgvW
    split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(wintypes.LPWSTR)
    release = kernel.LocalFree
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = split(command.strip(), ctypes.byref(count))
    if not arguments:
        raise OSError("无法解析测试命令")
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        release(ctypes.cast(arguments, ctypes.c_void_p))


class ProjectCatalog:
    """将启动配置与用户目录登记统一供任务提交和后续会话查询。"""

    MAX_BYTES = 256 * 1024  # 登记文件最大256KiB，防止无界读取。

    def __init__(self, runs_root: Path, config: WebConfig) -> None:
        """绑定 runs_root 的受保护登记文件与 config 初始目录；不读写或启动进程。"""
        self.root = runs_root / ".local-projects"  # 登记文件不位于用户项目代码中。
        self.path = self.root / "catalog.json"  # 只原子替换此文件。
        self.config = config  # TOML 作为初始登记来源。
        self._lock = threading.RLock()  # 同进程目录与设置更新串行。

    def _check_path(self) -> None:
        """核对登记路径没有链接或重解析点；异常抛 CatalogError，不创建目录。"""
        if self.root.resolve() != self.root or self.path.resolve() != self.path:
            raise CatalogError("project_catalog_unreadable", "最近项目记录路径发生变化。", 503)

    def _read(self) -> list[LocalProject]:
        """读取并严格核验最多256KiB的登记文件，返回条目；缺失正常返回空列表。"""
        try:
            self._check_path()
            if not self.path.exists():
                return []
            if not self.path.is_file():
                raise ValueError("not regular")
            with self.path.open("rb") as source:
                raw = source.read(self.MAX_BYTES + 1)
            if len(raw) > self.MAX_BYTES:
                raise ValueError("limit")
            state = CatalogState.model_validate_json(raw)
            return state.entries
        except CatalogError:
            raise
        except (OSError, ValueError, ValidationError):
            raise CatalogError("project_catalog_unreadable", "最近项目记录无法读取，请检查本地记录。", 503) from None

    def _write(self, entries: list[LocalProject]) -> None:
        """原子保存 entries；失败保留旧文件并抛 CatalogError，不修改任何项目代码。"""
        temporary = None
        try:
            self._check_path()
            self.root.mkdir(parents=True, exist_ok=True)
            self._check_path()
            raw = CatalogState(entries=entries).model_dump_json(indent=2).encode("utf-8")
            if len(raw) > self.MAX_BYTES:
                raise ValueError("limit")
            temporary = self.root / (".pending-" + uuid4().hex)
            with temporary.open("xb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except CatalogError:
            raise
        except (OSError, ValueError):
            raise CatalogError("project_catalog_write_failed", "无法保存最近项目，项目文件未被修改。", 503) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def snapshot(self) -> tuple[list[ProjectPreset], list[WebWorkspace], list[dict]]:
        """一次读取登记，返回Git预设、普通目录预设和最近列表；各列表属于同一不可变来源快照。"""
        with self._lock:
            entries = self._read()
            projects = {item.id: item.model_copy(deep=True) for item in self.config.projects}
            workspaces = {item.id: item.model_copy(deep=True) for item in self.config.workspaces}
            for item in entries:
                path_key = os.path.normcase(str(item.path))
                same_id = [preset for preset in (*self.config.projects, *self.config.workspaces) if preset.id == item.id]
                if any(os.path.normcase(str(preset.path)) != path_key for preset in same_id):
                    continue  # 宿主重新分配编号时，不用旧的本地目录覆盖新的来源。
                # 路径身份不随Git初始化/移除而变化，明确打开后的类型覆盖同路径初始配置。
                projects = {key: preset for key, preset in projects.items() if os.path.normcase(str(preset.path)) != path_key}
                workspaces = {key: preset for key, preset in workspaces.items() if os.path.normcase(str(preset.path)) != path_key}
                if item.kind == "project":
                    projects[item.id] = ProjectPreset(**item.model_dump(exclude={"kind", "last_opened_at"}))
                else:
                    workspaces[item.id] = WebWorkspace(id=item.id, label=item.label, path=item.path, python_executable=item.python_executable, python_paths=item.python_paths)
            opened = {(item.id, os.path.normcase(str(item.path))): item.last_opened_at.isoformat() for item in entries}
            recent = [
                {"id": item.id, "label": item.label, "path": str(item.path), "kind": kind, "python_executable": item.python_executable,
                 "last_opened_at": opened.get((item.id, os.path.normcase(str(item.path))))}
                for kind, values in (("project", projects.values()), ("workspace", workspaces.values()))
                for item in values if kind == "project" or not item.is_example
            ]
            recent.sort(key=lambda item: item["last_opened_at"] or "", reverse=True)
            return list(projects.values()), list(workspaces.values()), recent

    def project_presets(self) -> list[ProjectPreset]:
        """返回同一次登记快照的Git预设；不执行测试或修改记录。"""
        return self.snapshot()[0]

    def workspace_presets(self) -> list[WebWorkspace]:
        """返回同一次登记快照的普通目录预设；个人目录始终原地执行，示例由专用入口选择。"""
        return self.snapshot()[1]

    def recent(self) -> list[dict]:
        """返回同一次登记快照的最近列表；读取页面不改变最近打开时间。"""
        return self.snapshot()[2]

    def register(self, selected: Path) -> str:
        """核验用户选择的 selected 并登记，返回稳定编号；只读识别 Git，不执行代码或模型。"""
        with self._lock:
            try:
                root = selected.resolve(strict=True)
                if not root.is_dir():
                    raise ValueError("not directory")
                # 运行记录不得变成任务来源，避免模型读到其他会话证据。
                if root.is_relative_to(self.root.parent):
                    raise CatalogError("project_records_forbidden", "请选择运行记录目录以外的项目文件夹。")
                marker = next((parent / ".git" for parent in (root, *root.parents) if (parent / ".git").exists()), None)
                kind = "workspace"
                program = shutil.which("git")
                if program is None and marker is not None:
                    raise CatalogError("project_git_unavailable", "此目录属于 Git 项目，但本机无法调用 Git。")
                if program is not None:
                    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    found = subprocess.run(
                        [program, "-C", str(root), "rev-parse", "--show-toplevel"],
                        capture_output=True, timeout=10, env=_child_environment(), **flags,
                    )
                    if found.returncode == 0:
                        git_root = Path(found.stdout.decode("utf-8").strip()).resolve()
                        if git_root != root:
                            raise CatalogError("project_root_required", "请选择 Git 仓库根目录，当前选择的是仓库内的子文件夹。")
                        kind = "project"
                    else:
                        bare = subprocess.run([program, "-C", str(root), "rev-parse", "--is-bare-repository"],
                            capture_output=True, timeout=10, env=_child_environment(), **flags)
                        if bare.returncode == 0 and bare.stdout.strip() == b"true":
                            raise CatalogError("project_bare_unsupported", "请选择带工作目录的 Git 项目，当前选择的是裸仓库。")
                        if marker is not None:
                            raise CatalogError("project_git_invalid", "Git 仓库无法识别，请检查所选目录。")
                candidates = self.recent()
                existing = next((item for item in candidates if os.path.normcase(item["path"]) == os.path.normcase(str(root))), None)
                identifier = existing["id"] if existing else "local-" + hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:24]
                entries = self._read()
                old = next((item for item in entries if item.id == identifier), None)
                preset = next((item for item in self.project_presets() if item.id == identifier), None)
                values = old.model_dump() if old else {}
                if preset is not None and old is None:
                    values.update(preset.model_dump())
                values.update(id=identifier, label=existing["label"] if existing else (root.name or str(root))[:100], path=root, kind=kind, last_opened_at=datetime.now(UTC))
                entry = LocalProject(**values)
                updated = [entry, *(item for item in entries if item.id != identifier and os.path.normcase(str(item.path)) != os.path.normcase(str(root)))]
                if len(updated) > 100:
                    raise CatalogError("project_catalog_full", "最近项目已达100个，请整理本地登记后再添加。")
                self._write(updated)
                return identifier
            except CatalogError:
                raise
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                raise CatalogError("project_directory_unavailable", "无法打开所选文件夹，请确认目录存在且可以访问。") from None

    def reopen(self, identifier: str) -> str:
        """重新核验已登记 identifier 并更新最近时间；不存在时拒绝，不接受新路径。"""
        item = next((item for item in self.recent() if item["id"] == identifier), None)
        if item is None:
            raise CatalogError("unknown_project", "该项目尚未登记，请重新选择文件夹。")
        return self.register(Path(item["path"]))

    def configure_execution(
        self, identifier: str, command: str, timeout: float, python_executable: str | None = None,
    ) -> None:
        """保存 identifier 的测试 command、秒级 timeout 与项目 python_executable；返回 None。

        解释器 None/空白按 PATH，普通目录也可保存；项目测试仍限 Git 项目。
        只校验入口与来源，不执行程序；无效输入抛 CatalogError，原登记不变。
        """
        with self._lock:
            preset = next((item for item in self.project_presets() if item.id == identifier), None)
            workspace = next((item for item in self.workspace_presets() if item.id == identifier), None)
            source = preset or workspace
            if source is None or (workspace is not None and workspace.is_example):
                raise CatalogError("project_settings_unsupported", "请先打开自己的项目文件夹再保存运行设置。")
            try:
                interpreter = python_executable.strip() if python_executable and python_executable.strip() else None
                prepare_python_execution(source.path, source.python_paths, python_executable=interpreter)
                argv = _split_test_command(command)
                if preset is None and argv:
                    raise CatalogError("project_test_unsupported", "普通目录可设置 Python 解释器，项目测试请在 Git 项目中配置。")
                values = {**source.model_dump(exclude={"is_example"}), "python_executable": interpreter,
                          "test_argv": argv, "test_timeout_seconds": timeout}
                if preset is not None:
                    values = prepare_project_test(ProjectPreset(**values), preset.path).model_dump()
                record = LocalProject(**values, kind="project" if preset is not None else "workspace",
                                      last_opened_at=datetime.now(UTC))
                self._write([record, *(item for item in self._read() if item.id != identifier)])
            except CatalogError:
                raise
            except (ValueError, OSError, RuntimeError, ConfigurationError):
                raise CatalogError("project_settings_invalid", "项目设置无效，请检查 Python 解释器、测试程序路径、引号和超时。") from None
