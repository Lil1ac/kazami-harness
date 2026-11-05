"""浏览器可提交的受限请求与运行视图；后端内部配置不直接作为提交入口。"""

from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import Field, model_validator

from agent_harness.schemas import PythonExecution, ContextView, DataModel, HumanDecision, JsonValue, RunConfig, RunEvent, RunResult, ToolName
from agent_harness.projects import ProjectReview, ProjectWorkspace
from agent_harness.web.conversation_management import ConversationManagement
from agent_harness.execution.local import LocalChangeView
from agent_harness.schemas import MemorySelection
from agent_harness.permissions.models import ActionDecision, PermissionMode


# 页面仅覆盖模型、工具、预算和上下文策略；系统提示词与凭据来源由宿主配置固定。
class RunConfigPatch(DataModel):
    permission_mode: PermissionMode | None = None  # None继承；只能选择会话模式，不能修改宿主规则。
    memory: MemorySelection | None = None  # None 继承，只开放使用和候选选择，不允许页面修改宿主限额。
    auto_repair: bool | None = None  # None继承；显式开启要求宿主公开项目测试，首版最多两次修复。
    requested_model: str | None = Field(default=None, min_length=1, max_length=200)  # None 沿用宿主模型。
    tools: list[ToolName] | None = Field(default=None, min_length=1)  # None 沿用宿主工具。
    limits: dict[str, JsonValue] | None = None  # 按预算字段局部合并，再由 RunLimits 严格校验。
    skills: list[str] | None = Field(default=None, max_length=8)  # None 沿用宿主或前轮选择；空列表明确清空。
    context: dict[str, JsonValue] | None = None  # 上下文策略按字段覆盖；不开放任意指令文件路径。


# 提交任务时不接受宿主任意路径、程序参数或密钥。
class CreateRunRequest(DataModel):
    mode: Literal["auto", "plan"] = "auto"  # auto允许Agent主动规划；plan由用户明确限制为先规划。
    prompt: str = Field(min_length=1, max_length=65536)  # 本次任务描述，空白由 TaskSpec 拒绝。
    workspace_id: str | None = Field(default=None, min_length=1, max_length=64)  # 普通任务工作区，与 project_id 二选一。
    project_id: str | None = Field(default=None, min_length=1, max_length=64)  # Git 项目任务编号。
    execution_mode: Literal["local", "isolated"] = "local"  # 默认所选目录；isolated仅用于显式创建Git工作树。
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)  # 固定提交引用，默认提交时 HEAD。
    verifier_id: str | None = Field(default=None, min_length=1, max_length=64)  # None 表示不独立验收。
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
        if self.execution_mode == "local" and self.base_ref != "HEAD":
            raise ValueError("原目录执行不能切换 Git 基线，请选择隔离执行")
        return self


# 追加要求只接受会话请求内容，工作区和验收来源继承父运行并由宿主核对。
class FollowupRequest(DataModel):
    resume: bool = False  # True 仅恢复安全暂停，沿用原预算且不能混入配置或审批决定。
    action_decision: ActionDecision | None = None  # 具体动作决定，与计划决定互斥，不接受动作参数。
    mode: Literal["auto", "plan"] | None = None  # None继承；auto不能解除未批准计划的只读状态。
    decision: HumanDecision | None = None  # 当前问题回答或计划决定；不接受模型生成的批准。
    request_id: UUID = Field(strict=False)  # 浏览器生成的UUID字符串；相同编号和载荷只执行一次。
    prompt: str = Field(min_length=1, max_length=65536)  # 本次新要求，不要求用户重述历史。
    config: RunConfigPatch = Field(default_factory=RunConfigPatch)  # 按字段覆盖前轮可编辑配置。

    @model_validator(mode="after")
    def decision_kind(self) -> "FollowupRequest":
        """保证一次请求只处理一种决定；动作审批不允许夹带设置或重新规划，返回自身。"""
        if self.action_decision is not None and (self.decision is not None or self.mode is not None or self.config.model_dump(exclude_unset=True)):
            raise ValueError("动作决定不能同时修改配置、模式或计划")
        if self.resume and (self.action_decision is not None or self.decision is not None or self.mode is not None or self.config.model_dump(exclude_unset=True)):
            raise ValueError("暂停恢复不能修改配置、权限或计划")
        return self


# 连续对话使用精简逐轮视图；完整事件、测试和差异仍通过每轮详情查看。
class ConversationTurn(DataModel):
    stop_reason: str | None = None  # 区分规划结束、拒绝和实际开发完成，缺少可信终态时为None。
    run_id: UUID  # 本轮独立运行编号。
    parent_run_id: UUID | None  # 首轮为空。
    turn_index: int = Field(ge=1)  # 用户请求序号。
    prompt: str  # 本轮用户要求。
    final_answer: str | None = None  # 尚未完成或没有最终回答时为空。
    execution_status: str | None = None  # 没有建立Runner记录时为None。
    validation_status: str | None = None  # 未读取到本轮结果时不猜测通过。
    created_at: datetime | None = None  # 可信清单创建时间；准备遗留时未知。
    interrupted: bool = False  # 本轮因服务中断缺少可信终态；缺省为False。


class ConversationSource(DataModel):
    """会话创建时的项目身份；同编号改指其他路径时，不把旧会话移到新项目。"""

    id: str  # 原登记编号，普通目录与Git目录统一用于列表筛选。
    kind: Literal["project", "workspace"]  # 固定来源类型，不取实际派生工作区类型。
    label: str  # 当前同身份登记名称；登记移除时使用原目录名称。
    path: str  # 会话来源绝对目录，与逐轮工作树分别保存。
    registered: bool  # 当前目录登记仍匹配编号、类型和规范路径。
    execution_mode: Literal["local", "isolated"]  # 会话固定方式；不存在从旧记录猜测模式的兼容默认值。


class ConversationRunState(DataModel):
    """会话列表使用的最新轮状态；不携带完整回答、配置或工具输出。"""

    execution_status: Literal["created", "running", "completed", "failed", "cancelled", "timed_out", "limit_exceeded", "waiting_input"]  # 等待用户也是真实终态，不标为开发完成。
    validation_status: Literal["unverified", "passed", "failed", "error"]  # 最新轮验收事实。
    cleanup_status: Literal["not_needed", "complete", "incomplete", "unknown"]  # 最新轮清理事实。
    active: bool  # 当前服务仍持有该轮任务，包括快照收尾。
    cancelling: bool  # 当前轮已明确请求取消。
    interrupted: bool  # 缺少可信终态且本服务未执行该轮。
    phase: str  # 当前真实执行阶段。


class ConversationSummary(DataModel):
    """一个会话只有一条导航记录；无法核对最新轮时保留入口并明确提示。"""

    session_id: UUID  # 稳定会话编号，等于首次运行编号。
    title: str  # 统一有效标题；人工名称优先，否则从首条要求生成。
    management: ConversationManagement  # 统一管理标题、整理归属及生命周期，不替换执行来源。
    entry_run_id: UUID  # 正常指向最新轮；记录异常时指向仍可读取的入口。
    latest_run_id: UUID | None  # 以会话元数据为准，无法核对元数据时为空。
    turn_count: int | None  # 已接纳用户轮数；元数据损坏时不猜测。
    created_at: datetime  # 首轮创建时间；根缺失时取最早可读清单。
    updated_at: datetime  # 已读取轮次的最后真实事件时间，不因浏览更新。
    source: ConversationSource | None  # 旧CLI等无可核对项目来源时为空，仍可全局查看。
    latest: ConversationRunState | None  # 最新轮缺失或关联异常时为空，不沿用旧轮成功。
    error: str | None = None  # 记录异常的受控中文原因。


class ConversationPage(DataModel):
    """聚合和筛选之后分页的会话列表，不是对运行分页后再去重。"""

    items: list[ConversationSummary]  # 当前页会话，按更新时间倒序。
    total: int = Field(ge=0)  # 筛选后的会话总数。
    active_run_ids: list[UUID]  # 全部实际活动主运行，不包含尚未建立 Run 的准备作业。


# 会话读取不启动任何任务，can_continue仅为初步条件，提交时重新核验代码。
class ConversationView(DataModel):
    title: str  # 与列表和侧栏一致的有效标题。
    management: ConversationManagement  # 当前管理状态，历史轮也使用同一会话管理身份。
    source: ConversationSource | None = None  # 不可变执行来源；整理导航使用management.assigned_project。
    session_id: UUID  # 会话根运行编号。
    latest_run_id: UUID  # 线性会话最新已提交轮次。
    turns: list[ConversationTurn]  # 顺序展示用户要求和各轮回答。
    can_continue: bool  # 无活动任务且最新轮拥有可核对来源时为True。
    blocked_code: str | None = None  # 当前不可继续的稳定原因。
    blocked_reason: str | None = None  # 页面可读说明。
    active_run_ids: list[UUID] = Field(default_factory=list)  # 当前会话活动运行；其他项目不阻止本会话排队。
    continuation_notice: str | None = None  # 中断接续提示；允许发送时展示，不作为阻断原因。


# 审阅动作只决定工作树去留，不改变 Runner 执行和测试结论。
class ReviewDecision(DataModel):
    decision: Literal["keep", "discard"]  # 保留工作树或移除本任务工作树。


# 活动与历史运行共用同一视图，interrupted 表示缺少可信终态，绝不伪造失败结论。
class RunView(DataModel):
    run_id: UUID  # 与落盘登记一致的运行编号。
    prompt: str  # 原任务描述。
    workspace: str  # 模型工具、命令和验收实际使用的绝对目录。
    python_execution: PythonExecution | None  # 当轮已保存的默认解释器与源码目录；未准备时为 None，不用当前值填充。
    verification_kind: Literal["independent", "project_tests"] | None = None  # 固定检查用途；只有公开项目测试允许模型获得修复反馈。
    execution_mode: Literal["local", "isolated"] | None = None  # CLI未知为None；网页明确原目录或隔离。
    source_path: str | None = None  # 网页最初选择的项目目录，旧记录未知时不猜测。
    source_kind: Literal["project", "workspace"] | None = None  # 原目录Git任务仍按项目测试展示，无需伪造工作树审阅。
    local_changes: LocalChangeView | None = None  # 原目录每轮前后差异；隔离任务沿用项目审阅。
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
    project_workspace: ProjectWorkspace | None = None  # 会话共享目录的当前状态；历史轮次无管理权限。
    project: ProjectReview | None = None  # 旧记录与普通任务为 None；项目审阅独立于执行状态。
    context: ContextView | None = None  # 从已落盘事件还原的指令快照与最新输入检查；旧记录为 None。
    session_id: UUID | None = None  # 旧前端可缺省；服务对旧记录返回单轮会话编号。
    parent_run_id: UUID | None = None  # 本轮继承的直接前轮；首轮为空。
    turn_index: int = 1  # 用户要求轮次，与模型请求次数分别展示。


# 分页事件返回有效前缀，客户端按 seq 合并并去重。
class EventPage(DataModel):
    items: list[RunEvent]  # after 之后最多 limit 条事件。
    has_more: bool  # 当前磁盘已有更多事件可继续读取。
    last_seq: int  # 当前有效事件前缀的最大序号，不是本页最后序号。


class ProjectNameRequest(DataModel):
    """用户明确填写的项目名称；目录绑定不允许通过名称修改接口改变。"""

    label: str = Field(min_length=1, max_length=100)  # 去除首尾空白后由目录登记层校验有效名称。


class CreateProjectRequest(ProjectNameRequest):
    """确认原生文件夹选择后创建项目；只接受短期选择凭据，不接受自由输入的宿主路径。"""

    selection_id: str = Field(pattern=r"^[a-f0-9]{32}$")  # 原生选择成功返回的10分钟凭据，重启后失效。


# 用户明确保存的后续项目测试设置，不属于模型可修改的任务参数。
class ProjectSettingsRequest(DataModel):
    """用户明确保存的项目运行设置；保存只校验，不执行模型、测试或解释器。"""

    python_executable: str | None = Field(default=None, max_length=4096)  # None/空白按 PATH；非空为项目解释器路径。
    test_command: str = Field(default="", max_length=4096)  # 空字符串清空测试；仅分词，不经 Shell。
    test_timeout_seconds: float = Field(default=30.0, gt=0, le=3600, allow_inf_nan=False)  # 项目测试超时，单位秒。


class QueueSettings(DataModel):
    """后台调度的用户持久偏好；容量与串行资源规则仍由宿主控制。"""

    resume_on_start: bool  # True 允许启动后重新核验并调度未开始请求；不授权未知动作重放。
