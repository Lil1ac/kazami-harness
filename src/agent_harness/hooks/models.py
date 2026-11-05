"""Hooks 的配置与脚本协议；拒绝未知字段，避免输出意外获得控制权限。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


HookEvent = Literal["RunStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "CommandFinished", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop"]  # 对外稳定事件名。


class HookModel(BaseModel):
    """共享严格 JSON/TOML 契约；未知属性和隐式类型转换均拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True)  # 输入边界不容忍拼错字段。


class HookRule(HookModel):
    """声明一项本地脚本及其触发条件；启用不等于已审核或已获命令权限。"""

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")  # 来源内唯一稳定标识。
    event: HookEvent  # 只有对应事件可以解释该脚本的控制结果。
    argv: list[str] = Field(min_length=1, max_length=64)  # 显式程序及参数；不做 shell 字符串插值。
    cwd: str = "."  # 相对实际工作区的目录；不允许越界。
    tools: list[str] = Field(default_factory=list, max_length=64)  # 工具名 glob；空列表匹配全部。
    paths: list[str] = Field(default_factory=list, max_length=64)  # 工作区相对路径 glob；无文件路径时不匹配。
    agent_scopes: list[Literal["root", "develop"]] = Field(default_factory=lambda: ["root"])  # 开发子执行需显式选择；只读子项禁止命令。
    timeout_seconds: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)  # 单次秒数，仍受总截止时间约束。
    on_failure: Literal["continue", "block"] = "continue"  # 脚本故障策略，与脚本主动拒绝分开。
    required: bool = False  # True 时未审核或被禁用也不能默默跳过门禁。
    enabled: bool = True  # 配置默认启用，宿主/页面禁用仍独立记录。

    @model_validator(mode="after")
    def validate_rule(self):
        """校验非空命令、相对目录和门禁用途；返回自身，非法配置抛 ValueError。"""
        from pathlib import PureWindowsPath, PurePosixPath
        if any(not value.strip() or "\x00" in value for value in self.argv):
            raise ValueError("Hook 命令参数不能为空或包含空字节")
        path = PureWindowsPath(self.cwd)
        if path.drive or path.root or ".." in path.parts or PurePosixPath(self.cwd).is_absolute():
            raise ValueError("Hook 工作目录必须位于实际工作区内")
        if self.required and self.event not in {"PreToolUse", "RunStart", "Stop"}:
            raise ValueError("必需门禁只能用于执行前或完成检查")
        return self


class HookPolicy(HookModel):
    """宿主扩展策略；页面不能扩大限额或通过运行覆盖注册脚本。"""

    rules: list[HookRule] = Field(default_factory=list, max_length=64)  # 全局规则，按声明顺序先于项目规则。
    project_enabled: bool = True  # 是否发现工作区 .agents/hooks.toml。
    max_calls: int = Field(default=64, ge=1, le=512)  # 一次逻辑执行的父子累计脚本调用上限。
    max_seconds: float = Field(default=120.0, gt=0, le=600, allow_inf_nan=False)  # 父子累计活动秒数上限。
    max_context_bytes: int = Field(default=8192, ge=128, le=32768)  # 一次反馈的 UTF-8 字节上限。


class HookOutput(HookModel):
    """脚本标准输出；不同事件只接受各自允许的 decision，不允许重写工具或权限。"""

    decision: Literal["continue", "deny", "retry"] = "continue"  # deny 只适用于前置；retry 只适用于 Stop。
    reason: str = Field(default="", max_length=4000)  # 拒绝/续跑的可读理由，不能为空白。
    additional_context: str = Field(default="", max_length=32768)  # 协作数据，不能覆盖用户要求或宿主约定。

    @model_validator(mode="after")
    def require_reason(self):
        """拒绝或续跑必须提供 reason；合法时返回自身，否则抛 ValueError。"""
        if self.decision != "continue" and not self.reason.strip():
            raise ValueError("拒绝或续跑必须说明原因")
        return self


class HookReview(HookModel):
    """用户对页面实际展示定义的审核或启停操作。"""

    key: str = Field(min_length=1, max_length=100)  # host/project 与规则编号的组合。
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")  # 当前定义、脚本和工作区身份。
    action: Literal["trust", "enable", "disable"]  # 信任不替代执行时的具体动作授权。
