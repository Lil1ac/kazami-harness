"""浏览器可提交的受限请求与运行视图；后端内部配置不直接作为提交入口。"""

from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import Field, model_validator

from ..schemas import DataModel, JsonValue, RunConfig, RunEvent, RunResult, ToolName
from ..projects import ProjectReview


# 页面仅覆盖模型、工具和预算；系统提示词与凭据来源由宿主配置固定。
class RunConfigPatch(DataModel):
    requested_model: str | None = Field(default=None, min_length=1, max_length=200)  # None 沿用宿主模型。
    tools: list[ToolName] | None = Field(default=None, min_length=1)  # None 沿用宿主工具。
    limits: dict[str, JsonValue] | None = None  # 按预算字段局部合并，再由 RunLimits 严格校验。


# 提交任务时不接受宿主任意路径、程序参数或密钥。
class CreateRunRequest(DataModel):
    prompt: str = Field(min_length=1, max_length=65536)  # 本次任务描述，空白由 TaskSpec 拒绝。
    workspace_id: str | None = Field(default=None, min_length=1, max_length=64)  # 普通任务工作区，与 project_id 二选一。
    project_id: str | None = Field(default=None, min_length=1, max_length=64)  # Git 项目任务编号。
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)  # 固定提交引用，默认提交时 HEAD。
    verifier_id: str | None = Field(default=None, min_length=1, max_length=64)  # None 表示不独立验收。
    expected_outputs: list[str] = Field(default_factory=list, max_length=100)  # 工作区内相对产物引用。
    config: RunConfigPatch = Field(default_factory=RunConfigPatch)  # 每次运行配置，不修改服务默认值。


    @model_validator(mode="after")
    def task_source(self) -> "CreateRunRequest":
        """要求普通工作区与项目二选一，项目检查只能来自宿主配置；返回自身，无副作用。"""
        if (self.workspace_id is None) == (self.project_id is None):
            raise ValueError("工作区与项目必须二选一")
        if self.project_id is not None and self.verifier_id is not None:
            raise ValueError("项目任务不接受普通验收器")
        if self.project_id is None and self.base_ref != "HEAD":
            raise ValueError("普通任务不能指定 Git 基线")
        return self


# 审阅动作只决定工作树去留，不改变 Runner 执行和测试结论。
class ReviewDecision(DataModel):
    decision: Literal["keep", "discard"]  # 保留工作树或移除本任务工作树。


# 活动与历史运行共用同一视图，interrupted 表示缺少可信终态，绝不伪造失败结论。
class RunView(DataModel):
    run_id: UUID  # 与落盘登记一致的运行编号。
    prompt: str  # 原任务描述。
    workspace: str  # 本次实际使用目录，样例复制后指向独立副本。
    created_at: datetime  # 运行创建 UTC 时间。
    result: RunResult  # 当前内存快照或日志恢复的可信结果。
    phase: str  # 最近提交的真实执行阶段。
    event_count: int = Field(ge=0)  # 已落盘有效事件数。
    last_seq: int = Field(ge=0)  # 最后一条有效事件的序号。
    active: bool  # 服务当前仍持有的后台执行任务。
    cancelling: bool  # 取消已请求且后台任务尚未收尾。
    interrupted: bool  # 历史缺少终态且当前服务没有执行此任务。
    warnings: list[str] = Field(default_factory=list)  # 受控的读取或后台服务提示。
    config: RunConfig  # 本次配置快照，不含密钥值。
    project: ProjectReview | None = None  # 旧记录与普通任务为 None；项目审阅独立于执行状态。


# 分页事件返回有效前缀，客户端按 seq 合并并去重。
class EventPage(DataModel):
    items: list[RunEvent]  # after 之后最多 limit 条事件。
    has_more: bool  # 当前磁盘已有更多事件可继续读取。
    last_seq: int  # 当前有效事件前缀的最大序号，不是本页最后序号。
