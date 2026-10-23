"""浏览器可提交的受限请求与运行视图；后端内部配置不直接作为提交入口。"""

from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import Field, model_validator

from ..schemas import ContextView, DataModel, JsonValue, RunConfig, RunEvent, RunResult, ToolName, ResumeSource
from ..projects import ProjectReview


# 页面仅覆盖模型、工具、预算和上下文策略；系统提示词与凭据来源由宿主配置固定。
class RunConfigPatch(DataModel):
    requested_model: str | None = Field(default=None, min_length=1, max_length=200)  # None 沿用宿主模型。
    tools: list[ToolName] | None = Field(default=None, min_length=1)  # None 沿用宿主工具。
    limits: dict[str, JsonValue] | None = None  # 按预算字段局部合并，再由 RunLimits 严格校验。
    context: dict[str, JsonValue] | None = None  # 上下文策略按字段覆盖；不开放任意指令文件路径。


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


# 追加要求只接受会话请求内容，工作区和验收来源继承父运行并由宿主核对。
class FollowupRequest(DataModel):
    request_id: UUID = Field(strict=False)  # 浏览器生成的UUID字符串；相同编号和载荷只执行一次。
    prompt: str = Field(min_length=1, max_length=65536)  # 本次新要求，不要求用户重述历史。
    config: RunConfigPatch = Field(default_factory=RunConfigPatch)  # 按字段覆盖前轮可编辑配置。
    expected_outputs: list[str] | None = Field(default=None, max_length=100)  # None沿用前轮预期产物。


# 恢复不接受新提示词或配置，来源和预算必须固定继承检查点。
class ResumeRequest(DataModel):
    """显式恢复请求；同一编号及检查点只接纳一次，返回独立执行尝试。"""

    request_id: UUID = Field(strict=False)  # 浏览器生成的幂等编号，重复请求返回原运行。
    checkpoint_id: UUID = Field(strict=False)  # 必须为源运行最新且通过核验的检查点。


class CheckpointView(DataModel):
    """页面可展示的最新检查点；仅暴露来源时间和累计预算，不泄露内部文件路径。"""

    checkpoint_id: UUID  # 同一源运行内不可变的检查点编号。
    created_at: datetime  # 检查点提交的UTC时间。
    event_seq: int = Field(ge=1)  # 检查点对应的源事件前缀末序号。
    model_turns: int = Field(ge=0)  # 恢复链累计模型请求次数，包含摘要调用。
    tool_calls: int = Field(ge=0)  # 恢复链累计已开始的工具调用次数。
    active_duration_seconds: float = Field(ge=0, allow_inf_nan=False)  # 累计主动执行秒数，不包括停机。
    summary_calls: int = Field(ge=0)  # 恢复链累计摘要请求次数。


class RecoveryView(DataModel):
    """只读恢复能力初检；can_resume不承诺代码未变，提交时仍强核验完整内容。"""

    supported: bool  # 当前记录是否属于本轮支持的Git项目任务。
    can_resume: bool  # 静态记录与单槽条件允许用户尝试恢复。
    blocked_code: str | None = None  # 不允许恢复的稳定原因，允许时为空。
    blocked_reason: str | None = None  # 面向用户的受控说明，允许时为空。
    checkpoint: CheckpointView | None = None  # 尚未保存完整安全检查点时为空。
    busy_run_id: UUID | None = None  # 全局执行槽当前运行编号。


class ConversationAttempt(DataModel):
    """同一用户要求下的一次执行；首次及恢复尝试分别保留结果和详情入口。"""

    run_id: UUID  # 独立运行编号，详情与事件均使用此编号。
    attempt_index: int = Field(default=1, ge=1)  # 同一用户轮内从1递增的执行序号。
    resume_source: ResumeSource | None = None  # 首次为空，恢复时固定源检查点。
    final_answer: str | None = None  # 本次尚未完成或未产生回答时为空。
    execution_status: str | None = None  # 尚未建立可信记录时为空。
    validation_status: str | None = None  # 尚无宿主验收结果时为空。
    created_at: datetime | None = None  # 已建立运行记录的UTC时间。
    interrupted: bool = False  # 当前服务未执行且缺少可信终态；保留原execution_status并明确中断。


# 连续对话使用精简逐轮视图；完整事件、测试和差异仍通过每轮详情查看。
class ConversationTurn(DataModel):
    run_id: UUID  # 本轮独立运行编号。
    parent_run_id: UUID | None  # 首轮为空。
    turn_index: int = Field(ge=1)  # 用户请求序号。
    prompt: str  # 本轮用户要求。
    final_answer: str | None = None  # 尚未完成或没有最终回答时为空。
    execution_status: str | None = None  # 没有建立Runner记录时为None。
    validation_status: str | None = None  # 未读取到本轮结果时不猜测通过。
    created_at: datetime | None = None  # 可信清单创建时间；准备遗留时未知。
    attempts: list[ConversationAttempt] = Field(default_factory=list)  # 同一要求的执行尝试，按attempt_index升序。
    interrupted: bool = False  # 本轮最新尝试中断；旧会话响应缺省为False。


# 会话读取不启动任何任务，can_continue仅为初步条件，提交时重新核验代码。
class ConversationView(DataModel):
    session_id: UUID  # 会话根运行编号。
    latest_run_id: UUID  # 线性会话最新已提交轮次。
    turns: list[ConversationTurn]  # 顺序展示用户要求和各轮回答。
    can_continue: bool  # 无活动任务且最新轮拥有可核对来源时为True。
    blocked_code: str | None = None  # 当前不可继续的稳定原因。
    blocked_reason: str | None = None  # 页面可读说明。
    busy_run_id: UUID | None = None  # 全局单槽中的实际活动任务。
    continuation_notice: str | None = None  # 中断接续提示；允许发送时展示，不作为阻断原因。


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
    context: ContextView | None = None  # 从已落盘事件还原的指令快照与最新输入检查；旧记录为 None。
    session_id: UUID | None = None  # 旧前端可缺省；服务对旧记录返回单轮会话编号。
    parent_run_id: UUID | None = None  # 本轮继承的直接前轮；首轮为空。
    turn_index: int = 1  # 用户要求轮次，与模型请求次数分别展示。
    attempt_index: int = Field(default=1, ge=1)  # 同一用户轮的执行尝试，恢复递增而追加新要求归1。
    resume_source: ResumeSource | None = None  # 恢复尝试的不可变来源，普通新轮为空。


# 分页事件返回有效前缀，客户端按 seq 合并并去重。
class EventPage(DataModel):
    items: list[RunEvent]  # after 之后最多 limit 条事件。
    has_more: bool  # 当前磁盘已有更多事件可继续读取。
    last_seq: int  # 当前有效事件前缀的最大序号，不是本页最后序号。
