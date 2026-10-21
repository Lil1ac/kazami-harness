"""本地控制台的宿主预设：页面只选择登记编号，不接受任意路径和验收命令。"""

from pathlib import Path
import tomllib
import os
import shutil

from pydantic import Field, ValidationError, model_validator

from ..config import ConfigurationError
from ..schemas import DataModel
from ..projects import ProjectPreset


# 受控工作区登记；普通目录直接使用，样例目录可在每次运行时复制。
class WebWorkspace(DataModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")  # 页面选择使用的稳定编号。
    label: str = Field(min_length=1, max_length=100)  # 面向用户的目录用途说明。
    path: Path  # 解析后的宿主目录；仅服务端配置能够登记。
    copy_on_run: bool = False  # True 时复制样例，为每次运行提供独立目录。


# 宿主预置验收器索引；实际 argv 仍由既有验收 TOML 定义。
class WebVerifier(DataModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")  # 页面可提交的验收器编号。
    label: str = Field(min_length=1, max_length=100)  # 界面显示名称。
    description: str = Field(default="", max_length=500)  # 使用范围，尤其说明固定样例限制。
    path: Path  # 验收 TOML 的绝对路径，不向提交接口开放。


# 本地服务的固定目录与允许页面选择的预设，不保存任何凭据。
class WebConfig(DataModel):
    project_dir: Path  # 项目根目录，用于默认记录位置与构建提示。
    runtime_root: Path  # 每次样例复制的受控父目录。
    frontend_dist: Path  # Vite 构建目录；未构建时服务仍可提供 API。
    workspaces: list[WebWorkspace] = Field(default_factory=list)  # 普通工作区；可由项目预设替代。
    projects: list[ProjectPreset] = Field(default_factory=list)  # 宿主 Git 项目与固定测试命令。
    verifiers: list[WebVerifier] = Field(default_factory=list)  # 空列表表示不提供独立验收预设。

    @model_validator(mode="after")
    def unique_identifiers(self) -> "WebConfig":
        """校验各预设列表内部编号唯一并至少有一种任务入口；返回自身，不访问文件系统。"""
        if not self.workspaces and not self.projects:
            raise ValueError("至少登记一个普通工作区或 Git 项目")
        for entries in (self.workspaces, self.verifiers, self.projects):
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError("预设编号不能重复")
        return self


def _absolute(base: Path, raw: object) -> Path:
    """将配置字符串 raw 相对 base 解析为绝对路径；无效类型抛受控配置错误。"""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ConfigurationError("Web 配置路径必须是非空字符串。")
    return (base / raw).resolve()


def load_web_config(path: Path | None = None, *, project_dir: Path | None = None) -> WebConfig:
    """读取本地预设并返回绝对路径 WebConfig；None 使用内置安全默认值。

    project_dir=None 取当前目录；显式 TOML 的相对路径按其父目录解释。
    仅默认普通工作区会按需创建，自定义工作区必须已存在；不读取凭据。
    文件、语法、字段及目录错误统一抛不含原始配置正文的 ConfigurationError。
    """
    try:
        project = (project_dir or Path.cwd()).resolve(strict=True)
        if path is None:
            ordinary = project / ".runtime" / "workspace"
            ordinary.mkdir(parents=True, exist_ok=True)
            workspaces = [WebWorkspace(id="workspace", label="本地任务工作区", path=ordinary)]
            verifiers: list[WebVerifier] = []
            sample = project / "examples" / "logs"
            verifier = project / "examples" / "verifiers" / "logs.toml"
            if sample.is_dir():
                workspaces.append(
                    WebWorkspace(
                        id="logs", label="日志样例（每次独立复制）", path=sample, copy_on_run=True
                    )
                )
            if verifier.is_file():
                verifiers.append(
                    WebVerifier(
                        id="logs",
                        label="固定日志样例验收",
                        description="仅用于仓库自带日志样例；参考次数固定，不适用于任意日志。",
                        path=verifier,
                    )
                )
            return WebConfig(
                project_dir=project,
                runtime_root=project / ".runtime" / "web-workspaces",
                frontend_dist=project / "frontend" / "dist",
                workspaces=workspaces,
                verifiers=verifiers,
            )
        location = path.resolve(strict=True)
        with location.open("rb") as source:
            raw = tomllib.load(source)
        # 允许的顶层字段全部固定，凭据与任意命令不能混入 Web 配置。
        if set(raw) - {"runtime_root", "frontend_dist", "workspaces", "verifiers", "projects"}:
            raise ConfigurationError("Web 配置包含不支持的字段。")
        base = location.parent
        for key in ("workspaces", "verifiers", "projects"):
            entries = raw.get(key, [])
            if not isinstance(entries, list):
                raise ConfigurationError("Web 配置的预设必须是表数组。")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ConfigurationError("Web 预设格式无效。")
                entry["path"] = _absolute(base, entry.get("path"))
        config = WebConfig(
            project_dir=project,
            runtime_root=_absolute(base, raw.get("runtime_root", ".runtime/web-workspaces")),
            frontend_dist=_absolute(base, raw.get("frontend_dist", "frontend/dist")),
            workspaces=raw.get("workspaces", []),
            verifiers=raw.get("verifiers", []),
            projects=raw.get("projects", []),
        )
        for workspace in config.workspaces:
            # 显式配置也只允许自动创建项目固定默认目录，任意登记路径不享有此权限。
            if workspace.path == (project / ".runtime" / "workspace").resolve():
                workspace.path.mkdir(parents=True, exist_ok=True)
            if not workspace.path.is_dir():
                raise ConfigurationError("登记的工作区必须是已存在的目录。")
        for verifier in config.verifiers:
            if not verifier.path.is_file():
                raise ConfigurationError("登记的验收配置文件不存在。")
        for preset in config.projects:
            # 命令入口在宿主配置边界固定，参数仅允许工作树路径占位。
            argv = preset.test_argv
            program = argv[0]
            if "{workspace}" in program:
                raise ConfigurationError("项目测试入口不能来自任务工作树。")
            local = base / program
            if Path(program).is_absolute() or "/" in program or "\\" in program or local.exists():
                executable = local.resolve(strict=True)
            else:
                found = shutil.which(program)
                if found is None:
                    raise ConfigurationError("未找到项目测试入口，请登记已配置依赖的解释器或程序。")
                executable = Path(found).resolve(strict=True)
            if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
                raise ConfigurationError("项目测试入口必须是可执行文件。")
            if os.name == "nt" and executable.suffix.lower() not in {".exe", ".com"}:
                raise ConfigurationError("Windows 项目测试需通过 .exe 或 .com 解释器启动。")
            preset.test_argv = [str(executable), *argv[1:]]
        return config
    except ConfigurationError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, ValidationError):
        raise ConfigurationError("无法读取 Web 配置，请检查文件、字段与目录。") from None
