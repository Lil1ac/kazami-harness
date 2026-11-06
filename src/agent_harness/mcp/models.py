"""外部连接的公开契约；只允许凭据引用，不接受密钥正文。"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class McpModel(BaseModel):
    """拒绝未知字段的 MCP 请求基类，防止页面注入宿主策略。"""

    model_config = ConfigDict(extra="forbid", strict=True)  # 与运行配置保持同一严格边界。


class McpServerConfig(McpModel):
    """用户审核的连接配置；本机进程不提供操作系统沙箱。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")  # 稳定连接标识，也用于桌面凭据分区。
    label: str = Field(min_length=1, max_length=100)  # 页面及历史显示名称。
    transport: Literal["stdio", "http"]  # 本地标准流或远程 Streamable HTTP。
    url: str = Field(default="", max_length=4096)  # HTTP 完整端点；本地传输必须为空。
    command: str = Field(default="", max_length=4096)  # stdio 绝对可执行程序；不经 Shell。
    args: list[str] = Field(default_factory=list, max_length=64)  # 固定 argv，不能存放密钥。
    cwd: str = Field(default="", max_length=4096)  # stdio 固定绝对工作目录，与模型项目目录分开。
    env_vars: list[str] = Field(default_factory=list, max_length=32)  # 明确传递的宿主变量名，不保存变量值。
    credential_env: str = Field(default="", pattern=r"^$|^[A-Za-z_][A-Za-z0-9_]{0,127}$")  # 非空从宿主环境读取；空时尝试桌面分区凭据。
    credential_header: Literal["Authorization", "X-API-Key"] = "Authorization"  # Bearer 或 API Key 的固定头名。
    startup_timeout_seconds: float = Field(default=15.0, gt=0, le=120)  # 连接及发现超时，单位秒。
    tool_timeout_seconds: float = Field(default=60.0, gt=0, le=600)  # 单次调用超时，仍受本轮总时限限制。

    @model_validator(mode="after")
    def targets(self) -> "McpServerConfig":
        """核验传输目标并返回自身；拒绝混合配置、带凭据 URL 和控制字符。"""
        import re
        values = [self.label, self.url, self.command, self.cwd, *self.args]
        if any(any(char in value for char in "\x00\r\n") for value in values) or any(len(arg) > 4096 for arg in self.args):
            raise ValueError("连接字段含控制字符或参数过长")
        if len(set(self.env_vars)) != len(self.env_vars) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value) for value in self.env_vars):
            raise ValueError("环境变量名无效")
        if self.transport == "http":
            parsed = urlsplit(self.url)
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or self.command or self.cwd or self.args or self.env_vars:
                raise ValueError("HTTP 配置无效；端点不允许嵌入凭据和查询参数")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("远程服务必须使用 HTTPS，本机环回可用 HTTP")
        elif self.url or self.credential_env or not Path(self.command).is_absolute() or not Path(self.cwd).is_absolute():
            raise ValueError("stdio 需要绝对程序和工作目录")
        return self


class McpReview(McpModel):
    """对当前固定版本作审核决定，不提供覆盖服务配置的隐式入口。"""

    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")  # 用户真正看到的配置和目录指纹。
    action: Literal["trust", "enable", "disable", "research"]  # 信任允许启动；启用开放选择；research 审核研究工具。
    research_tools: list[str] = Field(default_factory=list, max_length=128)  # 宿主人工确认的研究工具原名；不采信服务注解。


class McpToolSelection(McpModel):
    """本轮选择项携带定义指纹，避免排队期间静默换用新版工具。"""

    name: str = Field(pattern=r"^mcp_[a-f0-9]{24}$")  # 命名空间内稳定公开工具名。
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")  # 选中时的连接、目录和授权指纹。
