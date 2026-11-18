"""配置与运行记录的数据结构及校验规则；供应商消息由适配器保存。"""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from agent_harness.permissions.models import PermissionPolicy, PermissionMode, PermissionResume, PermissionState
from agent_harness.execution.budget import RunBudget
from agent_harness.hooks.models import HookPolicy
from agent_harness.model_selection import CustomProvider, ModelId, ModelProvider, ModelRole


def _as_utc(value: datetime) -> datetime:
    """将已经通过时区检查的时间统一转换为 UTC，便于跨时区读取记录。"""
    return value.astimezone(UTC)


# 拒绝缺少时区的信息，读取旧记录时也统一到 UTC，避免跨时区排序歧义。
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]


# 数据类使用类前注释说明职责，避免 Pydantic 把文档字符串自动加入对外 JSON Schema。
# 所有公共数据结构的校验基类：严格检查类型、默认值和未知字段。
class DataModel(BaseModel):
    # 配置与落盘数据拒绝静默忽略字段，避免拼错参数却继续运行。
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


# 单次运行的预算与输出上限；各执行模块按需读取，未接入阶段的限制仅随配置保存。
class RunLimits(DataModel):
    # 逻辑模型轮次、工具调用和单轮 HTTP 尝试分别计数，重试不重复占模型轮次。
    max_model_turns: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=40, gt=0)
    # 单次响应的输出 token 上限，与整个任务允许请求多少轮是不同的预算。
    model_max_output_tokens: int = Field(default=4096, gt=0)
    max_http_attempts_per_turn: int = Field(default=3, gt=0)
    # 单轮超时包含该轮重试与退避；总运行超时同时约束所有轮次和工具阶段。
    model_turn_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    command_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    # 单次搜索从申请槽位开始的墙钟秒数，也受 Run 剩余时间约束。
    search_timeout_seconds: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    verifier_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    cleanup_grace_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    # 预览按字符限制；原始进程输出和文件写入按字节限制，不能混用单位。
    tool_output_preview_chars: int = Field(default=12000, gt=0)
    max_process_capture_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    # 同时约束整文件写入、局部编辑的原文件/结果大小及读取时的指纹扫描字节数。
    max_file_write_bytes: int = Field(default=1024 * 1024, gt=0)


# 上下文输入与项目约定的宿主策略；按 UTF-8 JSON 字节计量，不代表供应商 token 窗口。
class ContextPolicy(DataModel):
    # 默认读取实际工作区根 AGENTS.md；关闭时不访问该文件。
    load_project_instructions: bool = True
    # messages 与 tools 的固定 JSON 编码总字节上限；等于上限允许请求。
    max_input_bytes: int = Field(default=256 * 1024, gt=0)
    # 指令原始文件的字节上限；超过时拒绝加载，不截取部分项目约定。
    max_project_instruction_bytes: int = Field(default=32 * 1024, gt=0)
    # 旧记录默认关闭；masking 只做规则处理，hybrid 额外允许摘要。
    compression: Literal["off", "window", "masking", "hybrid"] = "off"
    # 普通根 Run 的末轮收尾试点。False 时按常规执行，子任务沿用其交接协议。
    root_handoff: bool = False
    # 宿主已安排独立验收的单文件维护任务可登记题面目标，用于最后三轮提示。None 关闭，不授予写权限。
    closeout_advice_target: str | None = Field(default=None, min_length=1, max_length=1024)
    # 根 Run 收尾试验候选，仅在受控公开自测可用时允许普通输入被拒后收尾。
    root_handoff_after_input_rejection: bool = False
    # hybrid 中按实际请求预算投影工具正文；评测控制组显式关闭。
    budget_projection: bool = True
    # 达到触发比例后处理到目标比例，目标小于触发，减少频繁改写缓存前缀。
    trigger_ratio: float = Field(default=0.8, gt=0, lt=1, allow_inf_nan=False)
    target_ratio: float = Field(default=0.6, gt=0, lt=1, allow_inf_nan=False)
    # 成功工具结果超过此 UTF-8 字节数时外置；失败结果保留原文。
    inline_tool_bytes: int = Field(default=8192, ge=512)
    # 最近完整 assistant/工具批次数；用户原文和项目规则始终固定保留。
    keep_recent_groups: int = Field(default=3, ge=1, le=100)
    # 每次运行摘要调用上限；同时占总模型轮次和总超时，零表示禁止摘要。
    max_summary_calls: int = Field(default=2, ge=0, le=20)
    # 结构化摘要 UTF-8 字节上限；超限拒绝，不静默截断。
    max_summary_bytes: int = Field(default=8192, ge=512)
    # None 表示只执行字节限制；非空是宿主声明窗口，不能当作厂商保证。
    model_context_tokens: int | None = Field(default=None, gt=0)
    # 额外预留 token；另行预留最大模型输出。
    token_safety_margin: int = Field(default=2048, ge=0)

    @model_validator(mode="after")
    def validate_compression(self) -> "ContextPolicy":
        """检查压缩比例和根收尾开关的依赖；返回自身，矛盾时抛 ValueError，不访问文件。"""
        if self.root_handoff_after_input_rejection and not self.root_handoff:
            raise ValueError("输入被拒后收尾需要启用根运行收尾")
        if self.closeout_advice_target is not None and self.root_handoff:
            raise ValueError("可编辑收尾提醒与只读根交接不能同时启用")
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("压缩目标比例必须小于触发比例")
        return self


# 一次运行冻结的根项目约定；只保存相对来源，不记录失败路径或异常原文。
class ProjectInstructionSnapshot(DataModel):
    status: Literal["loaded", "missing", "disabled", "error"]  # 成功、缺失、关闭或明确读取错误。
    path: Literal["AGENTS.md"] = "AGENTS.md"  # 当前仅支持实际工作区根目录的固定文件。
    content: str | None = None  # 仅 loaded 保存完整原文；其余状态不伪造或截断内容。
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")  # 成功时记录原始文件字节摘要。
    size_bytes: int | None = Field(default=None, ge=0)  # 成功时为原始字节数；未读取完整文件时为 None。
    error_code: str | None = Field(default=None, pattern=r"^project_instructions_[a-z0-9_]+$")  # 失败时只接受项目指令受控前缀原因码。


    @model_validator(mode="after")
    def validate_snapshot(self) -> "ProjectInstructionSnapshot":
        """校验本实例状态与原始 UTF-8 正文、字节数和摘要一致，返回自身；矛盾时抛 ValueError。

        loaded 必须保存完整且匹配的正文/摘要/大小，不带错误码；error 必须有受控错误码且
        不保存部分正文；missing/disabled 不包含任何正文、摘要、大小或错误码。
        """
        if self.status == "loaded":
            if self.content is None or self.sha256 is None or self.size_bytes is None or self.error_code is not None:
                raise ValueError("已加载项目约定必须包含完整正文、摘要和字节数，且没有错误码")
            try:
                raw = self.content.encode("utf-8")
            except UnicodeError:
                raise ValueError("项目约定正文必须是有效 UTF-8") from None
            if len(raw) != self.size_bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
                raise ValueError("项目约定正文、字节数与摘要不一致")
        else:
            if self.content is not None or self.sha256 is not None or self.size_bytes is not None:
                raise ValueError("未加载项目约定不能携带部分正文、摘要或字节数")
            if (self.status == "error") != (self.error_code is not None):
                raise ValueError("只有项目约定错误状态必须携带受控错误码")
        return self


# 每次拟发送模型请求前的确定性大小检查；被拒绝的下一轮也有记录。
class ContextCheck(DataModel):
    model_turn: int = Field(gt=0)  # 拟请求轮次；被拒绝时不计入实际 model_turns。
    input_bytes: int = Field(ge=0)  # 固定 JSON 编码后的 UTF-8 字节数，包含消息和工具定义。
    max_input_bytes: int = Field(gt=0)  # 本次运行的整体输入预算快照，单位字节。
    message_count: int = Field(ge=0)  # 拟发送消息数量，包含项目约定消息与完整工具结果。
    allowed: bool  # 同时满足字节限制和已声明的估算 token 输入限制。
    raw_input_bytes: int | None = Field(default=None, ge=0)  # 压缩前大小；旧记录未知。
    estimated_input_tokens: int | None = Field(default=None, ge=0)  # 本地估算，不冒充精确 tokenizer。
    max_input_tokens: int | None = Field(default=None, gt=0)  # 窗口减输出和安全余量后的上限。
    purpose: Literal["execution", "summary"] = "execution"  # 正常执行或额外摘要请求。
    # 普通工具轮与紧凑收尾可能检查同一个拟发送轮号；历史记录和摘要阶段无此值。
    phase: Literal["execution", "handoff"] | None = None


    @model_validator(mode="after")
    def validate_budget_decision(self) -> "ContextCheck":
        """校验本实例allowed与字节和可选token输入预算一致，返回自身；矛盾时抛ValueError。

        字节恰好等于上限可发送；声明token上限时还须有估算值且不超限。
        不信任受损事件中独立保存的布尔值，旧记录缺省token上限时沿用字节判断。
        """
        tokens_allowed = self.max_input_tokens is None or (
            self.estimated_input_tokens is not None and self.estimated_input_tokens <= self.max_input_tokens
        )
        if self.allowed != (self.input_bytes <= self.max_input_bytes and tokens_allowed):
            raise ValueError("上下文预算结论与输入字节数不一致")
        return self


# 结构化摘要描述已有工作；用户原文和验收事实另行保留。
class WorkSummary(DataModel):
    goal: str = Field(min_length=1)  # 历史片段处理的目标。
    decisions: list[str]  # 已有决策及理由。
    changes: list[str]  # 已修改文件或行为。
    evidence: list[str]  # 已观察证据，历史测试不能当作本轮通过。
    open_issues: list[str]  # 未解决问题和失败。
    next_steps: list[str]  # 待办，不代表额外授权。


# 输入投影变换的持久证据；范围为本运行完整消息的零基半开区间。
class ContextAction(DataModel):
    kind: Literal["offload", "mask", "summary", "summary_failed"]  # 外置、遮蔽、摘要或失败。
    model_turn: int = Field(gt=0)  # 此变换服务的拟请求轮次。
    before_bytes: int = Field(ge=0)  # 整体输入变换前大小。
    after_bytes: int = Field(ge=0)  # 变换后大小；失败不提交替换。
    source_start: int = Field(ge=0)  # 起始消息索引，包含。
    source_end: int = Field(gt=0)  # 结束消息索引，不包含。
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 原始归档内容摘要。
    reference: str  # 当前运行或已核验祖先的受控回读引用。
    summary: WorkSummary | None = None  # 仅成功摘要携带内容。
    reason: str | None = None  # 稳定失败原因。

    @model_validator(mode="after")
    def validate_action(self) -> "ContextAction":
        """核对范围、摘要状态及真实缩小；返回自身，矛盾时抛 ValueError，不写记录。"""
        if self.source_end <= self.source_start or (self.kind == "summary") != (self.summary is not None):
            raise ValueError("上下文变换范围或摘要状态无效")
        if self.kind != "summary_failed" and self.after_bytes >= self.before_bytes:
            raise ValueError("成功压缩必须实际缩小输入")
        return self


# 宿主发现的技能目录项；无效项保留普通字符串以便页面解释禁用原因。
class SkillOption(DataModel):
    """管理页及任务选择器共享的技能目录元数据。"""

    name: str  # 有效技能的选择编号；目录级故障可为空。
    description: str  # 技能用途；损坏目录无法解析时为空。
    source: str  # 宿主固定的 SKILL.md 或故障根目录绝对路径。
    available: bool = True  # 仅有效且无歧义的技能可选择。
    error_code: str | None = None  # 禁用时的稳定原因码。
    error_message: str | None = None  # 禁用时可展示的受控中文说明。
    id: str = ""  # 管理入口的稳定标识，同名来源各自独立。
    managed: bool = False  # True 表示由应用保存的导入副本。
    enabled: bool = True  # 持久启停状态，与格式校验是否通过分别记录。
    revision: str = ""  # 管理操作提交时核对的版本，空值表示不可管理。
    manual_only: bool = False  # 技能声明仅能由用户选中。


# 一轮实际加载的完整技能快照；后续修改源文件不会改变此记录。
class SkillSnapshot(DataModel):
    """固定技能正文和附属文件版本，供执行与历史回看。"""

    name: str  # 符合目录命名规范的技能编号。
    description: str  # 本次加载时的用途描述。
    source: str  # 本次读取的外部路径或受管来源标识。
    content: str  # 包含 YAML frontmatter 的完整 UTF-8 原文，不截断。
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 原始文件字节的 SHA-256 指纹。
    size_bytes: int = Field(ge=0)  # 原始 UTF-8 文件的字节数。
    activation: Literal["manual", "automatic"] = "manual"  # 正文进入上下文的触发方式。
    version: str | None = None  # 包内容或外部文件身份指纹，与正文 SHA-256 分开。
    resources: dict[str, str] = Field(default_factory=dict)  # 允许读取的相对路径与固定版本。

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SkillSnapshot":
        """核对本实例完整正文的字节数与摘要并返回自身；不一致抛 ValueError，不读取源文件。"""
        raw = self.content.encode("utf-8")
        if len(raw) != self.size_bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("技能正文与保存的字节指纹不一致")
        return self


class SkillResourceSnapshot(DataModel):
    """本轮实际读取的技能资料正文及指纹。"""

    name: str  # 已加载的所属技能名称。
    path: str  # 相对技能根目录的公开路径。
    content: str  # 已按 UTF-8 读取的完整正文。
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 正文原字节的指纹。
    size_bytes: int = Field(ge=0)  # 正文原字节数，参与技能资料总量检查。

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SkillResourceSnapshot":
        """按本实例的正文复核字节数与指纹，不一致时抛 ValueError。"""
        raw = self.content.encode("utf-8")
        if len(raw) != self.size_bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("技能资料与保存的字节指纹不一致")
        return self


# 选中技能无法加载时保存的受控错误，不包含异常正文或部分文件内容。
class SkillFailure(DataModel):
    name: str | None = None  # 可定位时为选中技能名，根目录故障可为空。
    code: str  # 稳定失败原因，供页面及运行结果识别。
    message: str  # 可以直接展示的中文说明。


# API 从事件派生的上下文视图；旧运行没有对应事件时整体视图可为 None。
class ContextView(DataModel):
    memory: dict[str, JsonValue] | None = None  # 本轮固定记忆及实际回读；None 表示尚未记录，不读取当前数据库补造。
    instruction: ProjectInstructionSnapshot  # 本次运行固定的项目约定快照。
    skills: list[SkillSnapshot] = Field(default_factory=list)  # 本轮真正加载的完整技能快照，旧记录默认为空。
    skill_resources: list[SkillResourceSnapshot] = Field(default_factory=list)  # 实际读取的资料快照，按首次读取顺序保存。
    skill_error: SkillFailure | None = None  # 加载失败时保存原因；没有选择或加载成功时为空。
    last_check: ContextCheck | None = None  # 最后一次检查；指令失败或尚未检查时为 None。
    serialization: Literal["json_utf8_v1"] = "json_utf8_v1"  # 明确计量版本，不等同 HTTP 封装或 token 数。
    actions: list[ContextAction] = Field(default_factory=list)  # 最近50条，完整记录保留在事件中。
    action_count: int = Field(default=0, ge=0)  # 全部变换数量，包含失败。
    summary_calls: int = Field(default=0, ge=0)  # 已开始摘要调用数，包含失败和取消。


# 这里只声明允许识别的名称；当前真正实现和可分发的工具由工具注册层决定。
from agent_harness.mcp.models import McpToolSelection

ToolName = Literal["read_file", "search_files", "write_file", "edit_file", "run_command"]


# 记忆的公开选择；use 与 capture 独立，关闭读取不等于停止提出候选。
class MemorySelection(DataModel):
    use: bool = True  # 使用当前项目已确认记忆，首次无条目时正常继续。
    capture: bool = True  # 允许执行阶段提出候选，候选须人工确认后跨会话生效。


# 宿主记忆预算；大小均为 UTF-8 字节，不增加整体输入或工具额度。
class MemoryPolicy(MemorySelection):
    max_input_bytes: int = Field(default=16384, ge=512, le=65536)  # 自动注入的总字节上限。
    max_entries: int = Field(default=6, ge=0, le=20)  # 自动正文条数，0 表示只提供搜索入口。
    history: bool = True  # 宿主是否开放项目历史检索；use=False 时无效。
    topics: bool = True  # 宿主是否开放主题索引；use=False 时无效。


# 可序列化的运行设置：模型、工具、系统提示词和限制；不保存凭据值。
class ModelSelection(DataModel):
    """固定的一组供应商与模型标识，用于主运行和独立角色。"""

    provider: ModelProvider  # 负责请求和密钥分区的已登记服务商。
    requested_model: ModelId  # 该服务商登记的具体模型。

# 每轮固定的模型、执行策略与已选择的服务商定义。
class GenerationOptions(DataModel):
    """显式模型采样参数；None 表示不向兼容端点发送该字段。"""

    thinking: Literal["enabled", "disabled"] | None = Field(default=None, description="是否启用供应商思考模式；None使用服务端默认。")
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(default=None, description="思考强度，None不覆盖供应商默认。")
    top_p: float | None = Field(default=None, gt=0, le=1, description="核采样概率阈值，None不显式发送。")

    def request_fields(self) -> dict:
        """返回可直接加入请求的非空参数；无参数，不包含模型名称或凭据。"""
        fields = self.model_dump(exclude_none=True)
        if self.thinking is not None:
            fields["thinking"] = {"type": self.thinking}
        return fields


class RunConfig(DataModel):
    """宿主运行策略及模型目录。首次配置前允许模型为空，执行前必须完成选择。"""

    provider: ModelProvider | None = None  # 无可用定义时为空，否则使用已选或首个启用服务商。
    requested_model: ModelId | None = None  # 为空时使用所选服务商的默认模型。
    generation: GenerationOptions = Field(default_factory=GenerationOptions)  # 运行时冻结的思考与采样参数。
    custom_providers: dict[ModelProvider, CustomProvider] = Field(default_factory=dict, max_length=16)  # 宿主可登记的端点，运行清单只保存本轮选择。
    mcp_tools: list[McpToolSelection] = Field(default_factory=list, max_length=16)  # 本轮明确选择的外部工具及定义指纹；空列表不连接服务。
    hooks: HookPolicy = Field(default_factory=HookPolicy)  # 宿主规则及扩展开销上限，页面不能覆盖。
    permission_policy: PermissionPolicy = Field(default_factory=PermissionPolicy)  # 宿主权限上限，网页不能覆盖。
    permission_mode: PermissionMode = "workspace_edit"  # 会话研究、工作区编辑或严格审批模式。
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)  # 每轮冻结使用/学习选择及宿主注入限额。
    planning_policy: Literal["auto", "required"] = "auto"  # 宿主策略：auto由Agent判断；required在新任务开始即只读，网页不能覆盖。
    role_models: dict[ModelRole, ModelSelection] = Field(default_factory=dict)  # 探索/审阅覆盖；未登记角色继承父运行。
    tools: list[ToolName] = Field(
        default_factory=lambda: ["read_file"],
        min_length=1,
    )
    system_prompt: str = Field(
        default="你是一个工具型任务助手。根据工具结果继续操作，并如实说明完成情况。",
        min_length=1,
    )
    skill_roots: list[Path] = Field(default_factory=list, max_length=8)  # 宿主登记的技能集合目录，浏览器不能覆盖。
    skills: list[str] = Field(default_factory=list, max_length=8)  # 本轮按名称选择的技能，空列表表示不加载。
    skill_auto: bool = False  # 交互式主运行是否允许模型按需启用登记技能。
    # 每个配置创建独立的限制对象，避免不同运行共享可变设置。
    limits: RunLimits = Field(default_factory=RunLimits)
    # 每次运行独立的指令加载与整体模型输入预算策略。
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    auto_repair: bool = False  # 明确开启后，公开项目测试失败最多追加两次修复；仍共用本轮全部预算。

    @model_validator(mode="before")
    @classmethod
    def resolve_custom_defaults(cls, values: object) -> object:
        """按宿主目录补全主模型和角色默认值。目录为空时保留未配置状态。"""
        if not isinstance(values, dict) or not isinstance(providers := values.get("custom_providers"), dict):
            return values

        def resolve(selection: object) -> object:
            """在 selection 省略模型时返回补全的副本，保持调用者原字典。"""
            if not isinstance(selection, dict) or selection.get("requested_model") is not None or not isinstance(provider := selection.get("provider"), str):
                return selection
            definition = providers.get(provider)
            if isinstance(definition, CustomProvider):
                return {**selection, "requested_model": definition.default_model}
            if isinstance(definition, dict) and "default_model" in definition:
                return {**selection, "requested_model": definition["default_model"]}
            return selection

        if values.get("provider") is None:
            first = next((identifier for identifier, definition in providers.items()
                          if (definition.enabled if isinstance(definition, CustomProvider)
                              else isinstance(definition, dict) and definition.get("enabled", True) is True)), None)
            if first is not None:
                values = {**values, "provider": first}
        result = resolve(values)
        if isinstance(roles := values.get("role_models"), dict):
            return {**result, "role_models": {role: resolve(selection) for role, selection in roles.items()}}
        return result

    @model_validator(mode="after")
    def validate_config(self) -> "RunConfig":
        """校验模型、窗口及工具/技能选择，返回自身；重复或非法选择抛 ValueError。"""
        if not self.system_prompt.strip():
            raise ValueError("系统提示词不能为空白")
        if (self.provider is None) != (self.requested_model is None):
            raise ValueError("请选择已登记的服务商和模型")
        selections = [(self.provider, self.requested_model), *((item.provider, item.requested_model) for item in self.role_models.values())]
        for provider, model in selections:
            if provider is not None:
                definition = self.custom_providers.get(provider)
                if definition is None or model not in definition.model_ids:
                    raise ValueError("所选服务商或模型尚未登记")
        window = self.context.model_context_tokens
        if window is not None and window <= self.limits.model_max_output_tokens + self.context.token_safety_margin:
            raise ValueError("模型窗口必须大于输出预留与安全余量之和")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("工具名称不能重复")
        if len(self.mcp_tools) != len({item.name for item in self.mcp_tools}):
            raise ValueError("外部工具不能重复选择")
        if len(self.skills) != len(set(self.skills)):
            raise ValueError("技能名称不能重复")
        for name in self.skills:
            if len(name) > 64 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None:
                raise ValueError("技能名称必须为不超过64字符的小写字母、数字和单连字符")
        if len(self.skill_roots) != len(set(self.skill_roots)):
            raise ValueError("技能目录不能重复")
        return self

    def selected_providers(self) -> set[str]:
        """返回主模型与已选角色实际引用的服务商标识。"""
        providers = {item.provider for item in self.role_models.values()}
        if self.provider is not None:
            providers.add(self.provider)
        return providers

    def freeze_models(self) -> "RunConfig":
        """返回只保留当前选择的服务商快照，供运行清单和历史展示使用。"""
        return self.model_copy(update={"custom_providers": {
            provider: self.custom_providers[provider]
            for provider in sorted(self.selected_providers() & self.custom_providers.keys())
        }})

    def model_versions(self) -> dict[str, str]:
        """返回当前主模型和角色所用的自定义端点版本，不含凭据。"""
        return {provider: self.custom_providers[provider].version
                for provider in sorted(self.selected_providers() & self.custom_providers.keys())}

    @property
    def api_key_env(self) -> str | None:
        """返回当前供应商的环境变量名。桌面自定义密钥与免密端点可以为空。"""
        definition = self.custom_providers.get(self.provider)
        return definition.api_key_env if definition is not None else None

    @property
    def requires_model_key(self) -> bool:
        """判断当前请求是否需要提供供应商 Bearer 凭据。"""
        definition = self.custom_providers.get(self.provider)
        return definition is not None and definition.auth == "bearer"


# 宿主预先指定的验收程序及其工作目录、超时；描述入口，不负责执行验收。
class VerifierSpec(DataModel):
    kind: Literal["independent", "project_tests"] = "independent"  # 独立检查不反馈模型；project_tests表示用户明确公开的项目测试。
    argv: list[str] = Field(min_length=1)  # 宿主固定程序和参数，启动预检后 argv[0] 为绝对可执行入口。
    cwd: Path | None = None  # 验收进程目录；清单默认取清单所在目录，程序调用 None 时取宿主当前目录。
    timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)  # 验收自身上限（秒），仍受总运行截止时间约束。
    python_paths: list[Path] = Field(default_factory=list)  # 验收程序私有依赖目录，位于验收 cwd 内；不交给模型命令，共享项目源码另由执行快照提供。

    @model_validator(mode="after")
    def validate_program(self) -> "VerifierSpec":
        """校验非空程序及显式 cwd 内的绝对 Python 搜索目录；合法时返回自身，否则抛 ValueError。"""
        if not self.argv[0].strip():
            raise ValueError("验收程序不能为空白")
        if self.python_paths and self.cwd is None:
            raise ValueError("Python 搜索目录需要显式验收工作目录")
        for path in self.python_paths:
            if not path.is_absolute() or "\x00" in str(path) or os.pathsep in str(path):
                raise ValueError("Python 搜索目录必须是绝对路径")
            if self.cwd is not None and not path.resolve().is_relative_to(self.cwd.resolve()):
                raise ValueError("Python 搜索目录不能越出验收工作目录")
        return self


# 一次任务的输入：任务文本、绝对工作目录和可选宿主验收配置。
class PythonExecution(DataModel):
    """当轮工作区的 Python 执行快照；只校验路径结构，读取历史时不访问当前文件系统。"""

    workspace: Path  # 该轮实际工作区，作为共享源码目录的固定边界，与命令 cwd 分开。
    executable: Path | None  # 当轮项目配置/PATH 解析的 Python；None 表示未找到，非 Python 任务仍可执行。
    python_paths: list[Path] = Field(min_length=1)  # 依次查找的工作区源码目录；首项必须是工作区根目录。

    @model_validator(mode="after")
    def validate_paths(self) -> "PythonExecution":
        """校验绝对路径、工作区归属及目录顺序；返回自身，不读取磁盘或推测旧记录环境。"""
        if not self.workspace.is_absolute() or ".." in self.workspace.parts:
            raise ValueError("Python 执行工作区必须是规范绝对路径")
        if self.executable is not None and (not self.executable.is_absolute() or "\x00" in str(self.executable)):
            raise ValueError("Python 解释器必须是绝对路径")
        if self.python_paths[0] != self.workspace or len(set(self.python_paths)) != len(self.python_paths):
            raise ValueError("Python 源码目录必须以工作区根目录开始且不能重复")
        for path in self.python_paths:
            if (not path.is_absolute() or ".." in path.parts or "\x00" in str(path)
                    or os.pathsep in str(path) or not path.is_relative_to(self.workspace)):
                raise ValueError("Python 源码目录必须位于该轮工作区内")
        return self


# 限制浏览器每轮引用数量、单项与合计的 UTF-8 字节数；模型仍按实际输入计量。
MAX_FILE_REFERENCES = 8
MAX_REFERENCE_BYTES = 32 * 1024
MAX_REFERENCE_TOTAL_BYTES = 64 * 1024


class ProjectFileRange(DataModel):
    """从实际工作区或选定 Git 提交引用一个 UTF-8 文件，行号从 1 开始且包含两端。"""

    path: str = Field(min_length=1, max_length=4096)  # 以斜杠分隔的工作区相对文件名。
    start_line: int | None = Field(default=None, ge=1)  # 两端均为空表示引用整个文件。
    end_line: int | None = Field(default=None, ge=1)  # 有起始行时必须指定不早于起始行的结束行。

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """验证 value 为规范项目相对文件名，返回原字符串；非法路径在读取前拒绝。"""
        parts = value.split("/")
        if any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
            raise ValueError("请选择项目内的普通文件")
        if "\\" in value or ":" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("文件名包含不可用字符")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "ProjectFileRange":
        """整文件不设行号，行段必须成对且有序；成功返回自身。"""
        if self.start_line is None and self.end_line is None:
            return self
        if self.start_line is None or self.end_line is None or self.start_line > self.end_line:
            raise ValueError("请指定有效的起始行和结束行")
        return self


class ProjectFileReference(ProjectFileRange):
    """用户已核对的引用选择，正文由宿主读取，浏览器仅提交范围与版本。"""

    version: str = Field(pattern=r"^[a-f0-9]{64}$")  # 绑定来源、范围和实际文件内容的 SHA-256。


class ProjectFileSnapshot(ProjectFileReference):
    """保存给本轮模型的原始 UTF-8 文本及其来源，历史预览读取同一份快照。"""

    content: str = Field(max_length=MAX_REFERENCE_BYTES)  # 整文件或指定行段正文，保留原换行。
    commit: str | None = Field(default=None, pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")  # 首轮隔离任务的固定提交。

    def reference(self) -> ProjectFileReference:
        """返回绑定范围的引用摘要；对话列表及排队核对均只使用这些字段。"""
        return ProjectFileReference(path=self.path, start_line=self.start_line, end_line=self.end_line, version=self.version)

    @model_validator(mode="after")
    def validate_size(self) -> "ProjectFileSnapshot":
        """检查正文按 UTF-8 编码后的大小；超限拒绝，成功返回自身。"""
        if len(self.content.encode("utf-8")) > MAX_REFERENCE_BYTES:
            raise ValueError("项目文件引用超出大小限制")
        return self


class TaskSpec(DataModel):
    """本轮用户要求、运行目录和固定的项目文件引用。"""

    prompt: str = Field(min_length=1)  # 用户输入的任务正文，用于会话标题及当前要求。
    workspace: Path  # 本轮实际执行的绝对目录。
    python_execution: PythonExecution | None = None  # 接纳运行时固定；None 表示尚未准备，读取时不推断当前环境。
    verifier: VerifierSpec | None = None  # None 表示本轮未登记独立验收。
    file_references: list[ProjectFileSnapshot] = Field(default_factory=list, max_length=MAX_FILE_REFERENCES)  # 仅本轮显式选择的资料。

    def user_content(self) -> str:
        """返回交给模型的用户正文；引用作为独立 JSON 数据段附加，展示标题仍使用原要求。"""
        if not self.file_references:
            return self.prompt
        values = [item.model_dump(exclude={"version"}, exclude_none=True) for item in self.file_references]
        return self.prompt + "\n\n【用户引用的项目文件】\n" + json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    @model_validator(mode="after")
    def validate_task(self) -> "TaskSpec":
        """拒绝空白任务，并要求启动边界已将工作目录解析为绝对路径。"""
        if not self.prompt.strip():
            raise ValueError("任务描述不能为空白")
        if not self.workspace.is_absolute():
            raise ValueError("工作目录必须在启动时解析为绝对路径")
        if self.python_execution is not None and self.python_execution.workspace != self.workspace:
            raise ValueError("Python 执行快照必须对应任务实际工作区")
        return self


# 网页启动时固定的宿主登记来源；后续轮次不能由浏览器改成任意目录或验收程序。
class RunOrigin(DataModel):
    kind: Literal["workspace", "project"]  # 普通文件任务或 Git 项目。
    preset_id: str = Field(min_length=1, max_length=64)  # 启动时的宿主登记编号。
    registered_path: Path  # 会话固定的来源目录；原目录执行时也等于实际工作区。
    execution_mode: Literal["local", "isolated"]  # 创建时明确固定；isolated仅表示主动创建的Git工作树。
    workspace_identity: tuple[int, int] | None = None  # 原目录的设备/对象编号，隔离模式为空；续轮拒绝目录被替换。
    verifier_id: str | None = None  # 普通任务选择的宿主验收编号；项目任务使用项目测试。

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "RunOrigin":
        """核对来源与执行方式并返回自身；普通目录隔离记录抛 ValueError，不访问文件。"""
        if self.execution_mode == "isolated" and self.kind != "project":
            raise ValueError("只有Git项目可以使用独立工作树")
        return self


# 后续运行固定的父历史来源；实际消息由父事件重建，并在调用模型前重新核验。
class HistorySource(DataModel):
    last_seq: int = Field(gt=0)  # 接纳追加时父事件的最后序号。
    messages_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 去掉旧宿主前缀后的完整消息摘要。
    interrupted: bool = False  # 宿主明确接纳中断后继续；旧记录默认False，网页不能设置来源。


def new_step_id() -> str:
    """生成宿主计划步骤的稳定字符串编号；返回 UUID 文本，不访问文件。"""
    return str(uuid4())


class PlanStep(DataModel):
    """计划内稳定编号的执行步骤；进度更新不能修改已经批准的步骤正文。"""

    id: str = Field(min_length=1, max_length=100)  # 宿主创建时生成并持久保存；读取缺失编号须拒绝，不能临时生成。
    text: str = Field(min_length=1, max_length=4000)  # 可审阅的具体工作与产出。
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"  # AgentTask 的只读进度投影，不能由规划工具独立更新。


class PlanRevision(DataModel):
    """绑定实际工作区的不可变方案版本；只有宿主处理用户决定时才能批准。"""

    version: int = Field(ge=1)  # 同一会话从1递增，旧版不能授权新版。
    title: str = Field(min_length=1, max_length=200)  # 方案名称。
    summary: str = Field(min_length=1, max_length=8000)  # 目标、架构与设计理由。
    scope: list[str] = Field(min_length=1, max_length=50)  # 本轮影响范围与重要边界。
    steps: list[PlanStep] = Field(min_length=1, max_length=30)  # 正文固定，状态可由模型维护。
    verification: list[str] = Field(min_length=1, max_length=30)  # 前后端联动与结果检查方式。
    workspace: str  # 提交时实际目录，批准与执行必须保持一致。
    workspace_identity: tuple[int, int]  # 目录对象身份，拒绝同路径被其他目录替换。
    status: Literal["pending", "approved", "rejected", "superseded"] = "pending"  # 用户决定与新版替代分别记录。


class UserInteraction(DataModel):
    """需要用户处理的单个持久请求；计划批准与普通问题使用不同语义。"""

    id: UUID = Field(strict=False)  # 宿主生成，客户端只能回应当前请求。
    kind: Literal["question", "plan_approval"]  # 回答问题不会自动获得执行权限。
    question: str = Field(min_length=1, max_length=8000)  # 问题或待批准说明。
    options: list[str] = Field(default_factory=list, max_length=6)  # 建议答案；仍允许自由输入。
    plan_version: int | None = Field(default=None, ge=1)  # 仅批准请求绑定具体计划版本。


class HumanDecision(DataModel):
    """浏览器明确提交的回答或计划决定；此结构不注册为模型工具。"""

    interaction_id: UUID = Field(strict=False)  # 必须匹配最新待处理请求。
    action: Literal["answer", "approve", "revise", "reject", "cancel"]  # 回答、批准、修改、拒绝或取消问题等待。
    plan_version: int | None = Field(default=None, ge=1)  # 计划操作必须携带当前版本。


class DecisionRecord(DataModel):
    """固定用户处理请求的事实；后续运行保留来源，模型文字不能伪造该记录。"""

    interaction_id: UUID = Field(strict=False)  # 原待处理请求编号。
    action: Literal["answer", "approve", "revise", "reject", "cancel"]  # 用户实际选择，取消不产生答案。
    plan_version: int | None = None  # 普通问题没有计划版本。
    text: str  # 用户补充或回答；不作为新工具权限指令。


class PlanningState(DataModel):
    """跨轮传递的规划状态；每轮输入和事件各自固定，不改写历史证据。"""

    phase: Literal["execution", "planning"] = "execution"  # 自动入口允许简单任务执行；进入规划后须批准才能解锁。
    reason: str | None = None  # 用户或Agent进入规划的原因。
    plans: list[PlanRevision] = Field(default_factory=list, max_length=30)  # 保留历史版本和各自决定。
    pending: UserInteraction | None = None  # None表示没有待回答请求，不等于已经批准。
    decisions: list[DecisionRecord] = Field(default_factory=list, max_length=100)  # 有界保留本会话用户决定。

    @model_validator(mode="after")
    def validate_state(self) -> "PlanningState":
        """核对本实例版本、待批准请求与权限状态，返回自身；矛盾抛ValueError，不访问磁盘。"""
        if [item.version for item in self.plans] != list(range(1, len(self.plans) + 1)):
            raise ValueError("计划版本必须连续且唯一")
        if any(item.status in {"pending", "approved"} for item in self.plans[:-1]):
            raise ValueError("旧版本不能保持待批准或执行授权")
        if self.phase == "execution" and self.plans and self.plans[-1].status != "approved":
            raise ValueError("存在计划时执行必须对应当前批准版本")
        if self.pending is not None:
            if self.pending.kind == "plan_approval":
                if (self.phase != "planning" or not self.plans or self.plans[-1].status != "pending"
                        or self.pending.plan_version != self.plans[-1].version):
                    raise ValueError("待批准请求必须绑定规划状态的当前方案")
            elif self.pending.plan_version is not None:
                raise ValueError("普通问题不能绑定计划批准版本")
        return self


class DelegationOrigin(DataModel):
    """子执行的宿主归属；与多轮会话的前轮关系分别保存。"""

    parent_run_id: UUID  # 唯一委派者；子执行不独立进入全局运行队列。
    team_id: str | None = None  # 宿主绑定的持久团队编号；普通委派为空。
    team_member_id: str | None = None  # 稳定成员身份，跨审批和多个执行段保持不变。
    role: Literal["explore", "review", "develop"]  # 探索、审阅只读；开发必须使用独立工作树。
    task_id: UUID | None = None  # 开发必须关联当前父会话工作项；只读时仅关联。
    write_scope: list[str] = Field(default_factory=list)  # 开发可写相对路径前缀；空列表禁止写入。
    previous_parent_id: UUID | None = None  # 审批接续时原子日志的父编号；首次为空。
    previous_run_id: UUID | None = None  # 接续前段子执行；只读继承历史，不重放工具。


class SubagentFinding(DataModel):
    """子模型的研究判断；分类不授予修改范围，证据编号由宿主核验。"""

    category: Literal["required", "optional", "observation"]  # 范围内必要修复、可选建议或事实观察。
    title: str = Field(min_length=1, max_length=160)  # 简短结论，不能只写空白。
    detail: str = Field(min_length=1, max_length=600)  # 影响与依据，不复制完整文件或扩大父任务要求。
    evidence_call_ids: list[str] = Field(max_length=2)  # 本成员可信接续链内唯一成功回执编号；只读仅接受读取，开发可引用写入/命令；required至少一个。


class SubagentEvidence(DataModel):
    """宿主从角色允许的实际成功工具回执提取的证据入口；正文始终保存在原子轨迹。"""

    run_id: UUID | None = None  # 真实证据所属子执行段；宿主提取，禁止引用负责人或其他成员。
    tool_call_id: str = Field(min_length=1, max_length=200)  # 当前可信接续链内唯一的调用编号；同名歧义不能作为证据。
    tool: str  # 角色允许的实际成功工具名，不由模型报告提供。
    path: str | None = None  # 文件或目录的实际相对路径；搜索/记忆等入口可能为空。
    sha256: str | None = None  # 工具已核验的完整文件指纹；无法获得时为空，不冒充完整读取。
    truncated: bool = False  # 原工具是否截断，报告不能把有限结果当作完整扫描。


class SubagentReport(DataModel):
    """有界、可验证来源的统一交接报告；部分完成与执行状态分别表达。"""

    summary: str = Field(min_length=1, max_length=1200)  # 面向父Agent的短结论，不能代替项目验收。
    findings: list[SubagentFinding] = Field(default_factory=list, max_length=8)  # 必要修复、观察及可选建议分别表达。
    limitations: list[str] = Field(default_factory=list, max_length=8)  # 模型明确说明的范围、未读取部分和不确定性。
    pending: list[str] = Field(default_factory=list, max_length=8)  # 尚未完成事项，不自动批准后续执行。
    evidence: list[SubagentEvidence] = Field(default_factory=list, max_length=24)  # 宿主核验的回执入口，引用的证据优先保留。
    partial: bool = True  # 只有执行正常且结构、证据及模型完成声明均有效时才为False。
    reason: str | None = None  # 部分完成的稳定原因；完整报告为空。


class SubagentRecord(DataModel):
    """父运行持有的有界委派快照；完整输入和工具结果保存在子日志。"""

    run_id: UUID  # 子日志编号，同时是父运行内的委派编号。
    title: str = Field(min_length=1, max_length=120)  # 委派标题，用于页面定位。
    objective: str = Field(min_length=1, max_length=8000)  # 主 Agent 明确提供的执行目标，不继承全部聊天历史。
    team_id: str | None = None  # 宿主绑定的持久团队编号；普通委派为空。
    team_member_id: str | None = None  # 稳定成员身份，跨审批和多个执行段保持不变。
    role: Literal["explore", "review", "develop"]  # 探索、审阅或隔离开发职责。
    model: ModelSelection  # 接纳委派时固定的供应商与请求模型。
    provider_name: str | None = None  # 历史展示的自定义服务商名称，内置服务商使用固定名称。
    response_model: str | None = None  # 最近成功响应的实际模型；尚未获得响应时为空。
    task_id: UUID | None = None  # 可选同会话任务编号，不表示任务已完成。
    status: Literal["running", "waiting_input", "completed", "failed", "cancelled", "timed_out", "limit_exceeded", "interrupted"] = "running"  # 可信执行状态；缺终态且父任务已停止时为 interrupted。
    cleanup_status: Literal["not_needed", "complete", "unknown", "incomplete"] = "not_needed"  # 子命令最坏清理状态，父结果聚合不重复计量。
    log_parent_id: UUID | None = None  # 子日志实际所属父运行；跨审批保留原记录时不改变。
    workspace: str | None = None  # 开发子项独立工作树；只读子项为空。
    write_scope: list[str] = Field(default_factory=list)  # 冻结的文件写入范围，不等于命令沙箱。
    changed_paths: list[str] = Field(default_factory=list)  # 成果相对委派起点的实际变更路径。
    scope_violations: list[str] = Field(default_factory=list)  # 超范围成果路径；非空禁止采纳。
    pending_action: dict | None = None  # 子项待批准原动作，由父会话统一处理。
    snapshot_status: Literal["pending", "ready", "error"] = "pending"  # 开发成果完整性，不代替测试结论。
    stop_reason: str | None = None  # 宿主生成的结束原因，未结束时为空。
    report: SubagentReport | None = None  # 统一结构化交接报告；活动期间为空，失败也保留部分证据。
    skill_names: list[str] = Field(default_factory=list, max_length=8)  # 仅明确委派的父级固定技能快照，默认不继承技能。
    parent_reserve: dict[str, float] = Field(default_factory=dict)  # 分配时为父保留的模型、工具次数和秒数；不是额外额度。
    model_turns: int = Field(default=0, ge=0)  # 子执行次数，已经计入父运行总数，不能再相加。
    tool_calls: int = Field(default=0, ge=0)  # 子工具次数，已经计入父运行总数。
    http_attempts: int = Field(default=0, ge=0)  # 子模型实际 HTTP 尝试，注入客户端不提供时为零。
    usage: dict[str, int] = Field(default_factory=dict)  # 子执行已知 token 用量，同样已计入父运行。
    usage_complete: bool = False  # False 表示存在未知用量，不据此估算完整费用。
    elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)  # 子执行单调耗时，单位秒。
    event_count: int = Field(default=1, ge=1)  # 已提交的子日志事件数，便于页面增量读取。
    max_model_turns: int = Field(ge=1, le=12)  # 接纳时固定的子轮数上限，不超过父级剩余值。
    max_tool_calls: int = Field(ge=1, le=30)  # 接纳时固定的子工具上限，不超过父级剩余值。
    timeout_seconds: float = Field(gt=0, le=180, allow_inf_nan=False)  # 接纳时固定的子时限，秒；父任务截止时间始终优先。


class ExecutionResume(RunBudget):
    """宿主核验后的安全暂停接续；新执行段不自动重放旧工具，也不重置预算。"""

    source_run_id: UUID  # 生成本检查点的直接父运行，不允许任意历史来源。
    workspace_identity: list[int]  # 安全停止时的设备与目录标识，替换目录后禁止恢复。


class CrashResume(ExecutionResume):
    """宿主从中断证据生成的接续输入，继承原预算并为尾部未配对的工具补充核对结果。"""

    tool_results: dict[str, dict]  # 仅包含直接父段最后一批缺回执的调用，按原工具编号索引。


class AutomationOrigin(DataModel):
    """宿主写入的自动化来源；HTTP运行请求不能构造，用于追踪及逐动作撤销核验。"""

    rule_id: str  # 稳定规则编号。
    revision: int  # 接纳时的定义版本，规则编辑后旧版本不能启动或执行新动作。
    fire_id: str  # 唯一触发记录，同一触发最多接纳一个作业。
    grant_generation: int  # 授权代次；撤销或重新授权后旧运行不继承新许可。


class RunManifest(DataModel):
    """运行开始时固定的任务、来源、配置、计划与当前用户决定；不保存密钥。"""
    # 数据格式版本用于读取兼容；runtime_version 另外标识生成记录的程序版本。
    schema_version: Literal[1] = 1
    run_id: UUID
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
    runtime_version: str = "0.1.0"
    task: TaskSpec
    config: RunConfig
    session_id: UUID | None = None  # 会话根编号；旧记录/CLI 缺省时以 run_id 作为单轮会话。
    parent_run_id: UUID | None = None  # 首轮为空，追加轮指向前一可信运行。
    turn_index: int = Field(default=1, ge=1)  # 用户请求轮次，从1开始，不等于模型轮次。
    origin: RunOrigin | None = None  # 新网页运行的宿主来源；旧记录可能未记录。
    history_source: HistorySource | None = None  # 首轮为空，追加轮固定父消息来源。
    memory_scope_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")  # 宿主固定目录身份；无身份的记录不作为跨会话证据。
    planning: PlanningState = Field(default_factory=PlanningState)  # 宿主固定的模式、计划和用户决定；网页不能直接传入该状态。
    human_decision: HumanDecision | None = None  # 仅本轮明确处理的决定；历史拒绝不阻止后续重新规划。
    delegation: DelegationOrigin | None = None  # 非空表示父运行内的委派子执行，不是会话续轮。
    permission_resume: PermissionResume | None = None  # 宿主绑定的原动作与累计预算；页面不得直接构造。
    execution_resume: ExecutionResume | None = None  # 仅安全暂停恢复时由宿主固定，不能与动作批准混用。
    crash_recovery: bool = False  # Web 宿主认领的手动隔离运行使用崩溃恢复记录。
    crash_resume: CrashResume | None = None  # 用户明确接续后由宿主核对并固定，不能由普通续轮提交。
    automation: AutomationOrigin | None = None  # 自动化首轮和人工接续保留同一来源，普通运行为空。

    @property
    def resumed_budget(self) -> RunBudget | None:
        """返回需要继承的原段预算，普通续轮或拒绝原动作时返回 None。"""
        return self.crash_resume or self.execution_resume or (
            self.permission_resume if self.permission_resume and self.permission_resume.decision == "approve" else None
        )


# 累计已知的输入、输出和总 token 数；数据是否完整由 RunResult 单独标记。
class TokenUsage(DataModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


# 已登记产物的相对位置；location 区分工作区文件和本次运行记录。
class ArtifactRef(DataModel):
    location: Literal["workspace", "run"]
    path: str = Field(min_length=1)
    kind: Literal["file", "stdout", "stderr"] = "file"

    @model_validator(mode="after")
    def validate_relative_path(self) -> "ArtifactRef":
        """产物只能使用相对路径引用，拒绝绝对路径、父级穿越和空字节。"""
        path = Path(self.path)
        if path.anchor or PureWindowsPath(self.path).drive or ".." in PurePosixPath(self.path.replace("\\", "/")).parts or path == Path(".") or "\x00" in self.path:
            raise ValueError("产物路径必须是安全相对路径")
        return self


# 独立验收的真实执行结果；未运行验收时 RunResult.verification 为 None。
class VerificationResult(DataModel):
    code_before: str | None = None  # 检查开始的Git规范源码指纹；普通目录或无法完整核对时为None。
    code_after: str | None = None  # 检查结束的同口径指纹；与开始不同则原通过结论不适用于结束代码。
    status: Literal["passed", "failed", "error"]  # 内容通过、内容不符、验收运行异常。
    reason: str  # 宿主生成的稳定原因码，不使用模型声明或异常正文。
    exit_code: int | None = None  # 验收子进程退出码；未获得实际退出码时为 None。
    stdout: str = ""  # 受字符预算限制的验收标准输出预览。
    stderr: str = ""  # 受字符预算限制的错误输出预览，不代表内容断言一定失败。
    timed_out: bool = False  # 验收自身或覆盖验收的总运行预算是否耗尽。
    cancelled: bool = False  # 用户或上游明确取消验收；与超时原因分开记录。
    output_limit_exceeded: bool = False  # 原始输出超过合计字节预算，不能判通过。
    cleanup_status: Literal["not_needed", "complete", "incomplete", "unknown"] = "not_needed"  # 本次验收进程的清理结论。
    elapsed_ms: int = Field(default=0, ge=0)  # 验收开始到进程收尾的单调时钟耗时，毫秒。
    truncated: bool = False  # 预览或原始保存存在截断；原始超限另有独立标记。
    artifacts: list[ArtifactRef] = Field(default_factory=list)  # 本次验收的 stdout/stderr 运行内引用。


class VerificationAttempt(DataModel):
    """一次实际检查的固定证据；初次为0，修复后的检查与对应修复次数一致。"""

    repair_attempt: int = Field(ge=0, le=2)  # 0为原始交付，1/2为相应修复后的代码。
    result: VerificationResult  # 该次进程退出、输出和清理事实，后续成功不能覆盖早先失败。


# 一次运行的当前或最终结果，分别记录执行、验收、清理状态以及回答与计数。
class RunResult(DataModel):
    hooks: dict = Field(default_factory=dict)  # 固定 Hook 定义、调用记录及累计开销；不回查当前审核状态。
    permissions: PermissionState = Field(default_factory=PermissionState)  # 当轮固定授权、待审批动作及此前预算。
    subagent_report: SubagentReport | None = None  # 子运行结束时核验并固定的报告；根运行为空。
    subagents: list[SubagentRecord] = Field(default_factory=list)  # 该轮委派快照，旧轮不查询新轮状态。
    team: dict | None = None  # 当轮固定的团队事实；历史不使用后续轮的最新状态。
    tasks: dict = Field(default_factory=dict)  # 当轮固定的工作项快照；历史读取不回查最新清单。
    schema_version: Literal[1] = 1
    run_id: UUID
    # 执行结束与验收通过分别记录：completed 本身不代表产物已经符合要求。
    execution_status: Literal[
        "created", "running", "completed", "failed", "cancelled", "timed_out", "limit_exceeded", "waiting_input"
    ] = "created"
    validation_status: Literal["unverified", "passed", "failed", "error"] = "unverified"
    verification: VerificationResult | None = None  # 当前代码的检查；修复开始后清空，不能用旧检查证明新代码。
    verification_attempts: list[VerificationAttempt] = Field(default_factory=list)  # 按实际执行顺序保留全部检查，不包含未启动的检查。
    repair_attempts: int = Field(default=0, ge=0, le=2)  # 已开始的自动修复次数，与模型轮次及用户追加轮次分别记录。
    cleanup_status: Literal["not_needed", "complete", "incomplete", "unknown"] = "not_needed"
    stop_reason: str | None = None
    planning: PlanningState = Field(default_factory=PlanningState)  # 本轮结束时的规划事实，等待不计为开发完成。
    # 统计已发起的逻辑轮次、HTTP 尝试及接纳处理的工具调用，不把重试重复算轮次。
    model_turns: int = Field(default=0, ge=0)
    http_attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    final_answer: str | None = None
    response_model: str | None = None  # 当前运行最近一个执行请求实际返回的模型。
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    artifact_status: Literal["pending", "complete", "incomplete"] = "pending"  # 文件收集独立状态；不改变执行或验收结论。
    artifact_warnings: list[str] = Field(default_factory=list)  # 采集超限、文件变化或证据保存失败的受控说明。
    usage: TokenUsage = Field(default_factory=TokenUsage)
    # 缺少用量或发生重试时，只展示已知部分，不能据此推算精确账单。
    usage_complete: bool = False
    active_duration_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)  # 本次运行执行时长，单位秒；旧记录缺省为零。

    @model_validator(mode="after")
    def validate_result(self) -> "RunResult":
        """检查本实例状态约束并返回自身；矛盾时抛 ValueError，不写文件。

        非 unverified 状态要求执行 completed；非空 verification 必须与顶层状态一致。
        旧记录缺省 verification 时仍可读取，未运行验收时该字段保持 None。
        """
        if self.validation_status != "unverified" and self.execution_status != "completed":
            raise ValueError("执行完成后才能记录验收结果")
        if self.verification is not None and self.verification.status != self.validation_status:
            raise ValueError("验收详情与顶层验收状态不一致")
        return self


# 日志中的单条事实：用运行 ID、连续序号、UTC 时间、累计耗时和载荷描述动作。
class RunEvent(DataModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    seq: int = Field(gt=0)  # 同一运行从 1 连续递增，读取时据此识别缺失或乱序事件。
    time_utc: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_ms: int = Field(ge=0)  # 使用单调时钟计算，系统时间校正不会使耗时倒退。
    type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    payload: dict[str, JsonValue] = Field(default_factory=dict)
