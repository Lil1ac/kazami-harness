"""桌面后台的资源定位和公开设置；窗口与加密凭据由 Electron 主进程管理。"""

from pathlib import Path
import sys
import threading

from pydantic import Field, ValidationError, field_validator

from agent_harness.config import ConfigurationError
from agent_harness.schemas import DataModel, RunConfig, RunLimits, ToolName


def resource_root() -> Path:
    """返回源码根目录或打包资源目录；无参数，不依赖启动时的当前目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def default_download_directory() -> Path:
    """返回下载默认目录；D 盘存在时遵循用户下载偏好，否则使用用户下载目录。"""
    return Path("D:/Codex/Downloads") if Path("D:/").is_dir() else Path.home() / "Downloads"


class DesktopBudget(DataModel):
    """桌面允许持久保存的三项总体预算；内部超时和输出策略仍归宿主配置。"""

    max_model_turns: int = Field(ge=1, le=10000)  # 单次任务允许的模型轮次。
    max_tool_calls: int = Field(ge=1, le=100000)  # 单次任务允许的工具调用总数。
    run_timeout_seconds: float = Field(gt=0, le=604800, allow_inf_nan=False)  # 总运行时间，单位秒。


class DesktopRunSettings(DataModel):
    """跨启动沿用的公开设置，不包含凭据、系统提示词或执行命令。"""

    requested_model: str = Field(min_length=1, max_length=200)  # 提交给模型供应商的名称。
    tools: list[ToolName] = Field(min_length=1)  # 用户明确允许的基础工具集合。
    limits: DesktopBudget  # 与前端设置页一致的总体预算。

    @field_validator("requested_model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        """清理模型名称首尾空白并返回；纯空白名称抛 ValueError，不写入设置。"""
        if not value.strip():
            raise ValueError("模型名称不能为空")
        return value.strip()

    def apply(self, base: RunConfig) -> RunConfig:
        """把公开设置合入 base 并完整校验，返回新配置；不改变活动任务的快照。"""
        raw = base.model_dump()
        raw.update(requested_model=self.requested_model, tools=self.tools)
        raw["limits"] = RunLimits.model_validate({**raw["limits"], **self.limits.model_dump()})
        return RunConfig.model_validate(raw)


class DesktopPreferences(DataModel):
    """后台公开设置的唯一格式；窗口状态和密钥不进入此模型。"""

    config: DesktopRunSettings | None = None  # None 使用宿主 TOML 默认。
    downloads_directory: Path = Field(default_factory=default_download_directory)  # 用户下载目标目录。

    @field_validator("downloads_directory")
    @classmethod
    def absolute_downloads(cls, value: Path) -> Path:
        """要求下载位置为绝对路径并返回规范路径；不创建目录，非法值抛 ValueError。"""
        if not value.is_absolute() or "\x00" in str(value):
            raise ValueError("下载目录必须为绝对路径")
        return value.resolve()


class PreferenceStore:
    """使用锁和原子替换保存公开设置，避免并发 HTTP 保存相互覆盖。"""

    def __init__(self, directory: Path) -> None:
        """在 directory 下读取设置；目录不存在时创建，损坏文件抛受控配置错误。"""
        self.directory = directory.resolve()  # Electron 指定的稳定应用数据目录。
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "settings.json"  # 不含凭据或窗口状态的公开设置。
        self._lock = threading.RLock()  # 同进程设置写锁。
        try:
            self.value = DesktopPreferences.model_validate_json(self.path.read_text(encoding="utf-8")) if self.path.exists() else DesktopPreferences()
        except (OSError, ValueError, ValidationError):
            raise ConfigurationError("桌面设置无法读取，请检查 settings.json 的格式和目录权限。") from None

    def update(self, **changes: object) -> DesktopPreferences:
        """原子合并 changes 并返回新设置；只写公开字段，失败保留内存中的原状态。"""
        with self._lock:
            value = DesktopPreferences.model_validate({**self.value.model_dump(), **changes})
            temporary = self.path.with_suffix(".tmp")
            try:
                temporary.write_text(value.model_dump_json(indent=2), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                raise ConfigurationError("桌面设置保存失败，请检查数据目录权限。") from None
            self.value = value
            return value
