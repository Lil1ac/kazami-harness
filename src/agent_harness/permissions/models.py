"""权限配置与审批数据契约，独立于运行模型以避免循环依赖。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PermissionMode = Literal["research", "workspace_edit", "strict"]  # 会话可选择的研究、编辑和逐项审批模式。
Decision = Literal["allow", "deny", "ask"]  # 宿主判定；ask 只能由匹配的单次批准解除。


class PermissionModel(BaseModel):
    """严格拒绝未知字段的权限契约；不接受模型构造的隐式授权字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)  # 配置与 HTTP 使用相同的明确字段。


class PermissionPolicy(PermissionModel):
    """宿主定义的权限上限；页面只能选择模式，不能覆盖本实例。"""

    read: Decision = "allow"  # 项目文件读取上限，首版不支持读取审批。
    write: Decision = "allow"  # 文件创建与编辑上限，规划阶段仍可进一步拒绝。
    command: Decision = "ask"  # 本机命令默认逐项审批；显式 allow 仅由宿主可信配置设置。
    verification: Decision = "ask"  # 项目测试与独立验收默认审批，不能借自动检查绕过限制。
    read_roots: list[str] = Field(default_factory=lambda: ["."], max_length=32)  # 工作区内可读相对目录；空列表禁止项目读取。
    write_roots: list[str] = Field(default_factory=lambda: ["."], max_length=32)  # 工作区内可写相对目录；不能扩大到工作区外。
    approval_ttl_seconds: int = Field(default=1800, ge=1, le=86400)  # 从请求创建起计的有效秒数，包含人工等待与排队。

    @field_validator("read_roots", "write_roots")
    @classmethod
    def relative_roots(cls, values: list[str]) -> list[str]:
        """校验目录前缀 values 并返回原列表；绝对路径、父级穿越或通配符抛 ValueError。"""
        from pathlib import PureWindowsPath, PurePosixPath
        for value in values:
            path = PureWindowsPath(value)
            if not value.strip() or path.drive or path.root or ".." in path.parts or any(c in value for c in "*?\x00") or PurePosixPath(value).is_absolute():
                raise ValueError("权限目录必须是工作区内无通配符的相对目录")
        return values


class ActionDecision(PermissionModel):
    """用户对某一固定动作的决定；request_id 由外层幂等作业请求提供。"""

    action_id: str = Field(min_length=36, max_length=36)  # 待处理动作 UUID。
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")  # 页面实际审阅的不可变动作指纹。
    action: Literal["approve", "reject", "cancel"]  # 批准本次、拒绝或取消等待，不支持批量提升权限。


class PermissionResume(PermissionModel):
    """宿主生成的动作接续检查点，页面和模型不能直接提交。"""

    action_id: str  # 事务内绑定接续作业的待执行动作。
    decision: Literal["approve", "reject", "cancel"]  # 本轮消费的人类决定。
    spent_model_turns: int = Field(default=0, ge=0)  # 同一审批链此前已经消耗的模型轮次。
    spent_tool_calls: int = Field(default=0, ge=0)  # 此前接纳的工具调用；接续原动作不重复计数。
    spent_seconds: float = Field(default=0.0, ge=0)  # 此前实际运行秒数，不包含人工等待和排队。
    spent_usage: dict[str, int] = Field(default_factory=dict)  # 此前各段已知prompt/completion/total_tokens；缺失用量不推算。
    spent_usage_complete: bool = True  # 任一此前模型请求缺失用量时为False。
    repair_attempts: int = Field(default=0, ge=0, le=2)  # 已经开始的自动修复次数，审批不得清零。
    delegation_used: bool = False  # 原执行已经使用委派时，继续保留末轮收尾约束。


class PermissionState(PermissionModel):
    """运行开始或结束时固定的授权阅读视图；历史不回查当前审批覆盖此快照。"""

    mode: PermissionMode = "workspace_edit"  # 实际会话模式，独立于先规划入口。
    policy_fingerprint: str = ""  # 宿主配置、模式、目录与规划版本的组合指纹。
    pending: dict | None = None  # 本轮待审批动作的固定公开快照；没有等待时为空。
    effective: dict[str, Decision] = Field(default_factory=dict)  # 宿主计算的文件、命令与验收实际判定，页面不重复实现规则。
    spent_model_turns: int = Field(default=0, ge=0)  # 进入本段 Run 前已用的模型轮次。
    spent_tool_calls: int = Field(default=0, ge=0)  # 进入本段 Run 前已用的工具次数。
    spent_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)  # 进入本段 Run 前已用的活动秒数。
    spent_usage: dict[str, int] = Field(default_factory=dict)  # 此前各段已知Token累计，当前段仍在RunResult.usage。
    spent_usage_complete: bool = True  # 此前各段用量是否都已知，不影响当前段的独立标记。
