"""审批与安全暂停共用的活动预算检查点；等待和排队不产生新额度。"""

from pydantic import BaseModel, ConfigDict, Field


class RunBudget(BaseModel):
    """此前执行段已花费的预算，供宿主接续使用，页面不能直接构造。"""

    model_config = ConfigDict(extra="forbid", strict=True)  # 持久输入拒绝额外或隐式类型转换。
    spent_model_turns: int = Field(default=0, ge=0)  # 所有此前段已启动的模型请求次数。
    spent_tool_calls: int = Field(default=0, ge=0)  # 此前已接纳的工具次数，接续不会清零。
    spent_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)  # 活动秒数；不含安全暂停后的人类等待。
    spent_usage: dict[str, int] = Field(default_factory=dict)  # 此前各段已知输入、输出和总 token。
    spent_usage_complete: bool = True  # 任一段用量不完整时为 False，未知部分不推算成零。
    repair_attempts: int = Field(default=0, ge=0, le=2)  # 已开始的公开测试修复次数。
    delegation_used: bool = False  # 已使用委派时继续保留父级末轮交接约束。
