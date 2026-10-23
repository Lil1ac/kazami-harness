"""配置与运行记录的数据结构及校验规则；供应商消息由适配器保存。"""

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


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
    verifier_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    cleanup_grace_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    # 预览按字符限制；原始进程输出和文件写入按字节限制，不能混用单位。
    tool_output_preview_chars: int = Field(default=12000, gt=0)
    max_process_capture_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
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
    compression: Literal["off", "masking", "hybrid"] = "off"
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
        """检查目标比例小于触发比例；返回自身，矛盾时抛 ValueError，不访问文件。"""
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
    name: str  # 有效技能的选择编号；目录级故障可为空。
    description: str  # 技能用途；损坏目录无法解析时为空。
    source: str  # 宿主固定的 SKILL.md 或故障根目录绝对路径。
    available: bool = True  # 仅有效且无歧义的技能可选择。
    error_code: str | None = None  # 禁用时的稳定原因码。
    error_message: str | None = None  # 禁用时可展示的受控中文说明。


# 一轮实际加载的完整技能快照；后续修改源文件不会改变此记录。
class SkillSnapshot(DataModel):
    name: str  # 符合目录命名规范的技能编号。
    description: str  # 本次加载时的用途描述。
    source: str  # 本次读取的绝对文件路径，仅用于来源展示。
    content: str  # 包含 YAML frontmatter 的完整 UTF-8 原文，不截断。
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 原始文件字节的 SHA-256 指纹。
    size_bytes: int = Field(ge=0)  # 原始 UTF-8 文件的字节数。

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SkillSnapshot":
        """核对本实例完整正文的字节数与摘要并返回自身；不一致抛 ValueError，不读取源文件。"""
        raw = self.content.encode("utf-8")
        if len(raw) != self.size_bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("技能正文与保存的字节指纹不一致")
        return self


# 选中技能无法加载时保存的受控错误，不包含异常正文或部分文件内容。
class SkillFailure(DataModel):
    name: str | None = None  # 可定位时为选中技能名，根目录故障可为空。
    code: str  # 稳定失败原因，供页面及运行结果识别。
    message: str  # 可以直接展示的中文说明。


# API 从事件派生的上下文视图；旧运行没有对应事件时整体视图可为 None。
class ContextView(DataModel):
    instruction: ProjectInstructionSnapshot  # 本次运行固定的项目约定快照。
    skills: list[SkillSnapshot] = Field(default_factory=list)  # 本轮真正加载的完整技能快照，旧记录默认为空。
    skill_error: SkillFailure | None = None  # 加载失败时保存原因；没有选择或加载成功时为空。
    last_check: ContextCheck | None = None  # 最后一次检查；指令失败或尚未检查时为 None。
    serialization: Literal["json_utf8_v1"] = "json_utf8_v1"  # 明确计量版本，不等同 HTTP 封装或 token 数。
    actions: list[ContextAction] = Field(default_factory=list)  # 最近50条，完整记录保留在事件中。
    action_count: int = Field(default=0, ge=0)  # 全部变换数量，包含失败。
    summary_calls: int = Field(default=0, ge=0)  # 已开始摘要调用数，包含失败和取消。


# 这里只声明允许识别的名称；当前真正实现和可分发的工具由工具注册层决定。
ToolName = Literal["read_file", "search_files", "write_file", "run_command"]


# 可序列化的运行设置：模型、工具、系统提示词和限制；不保存凭据值。
class RunConfig(DataModel):
    provider: Literal["deepseek"] = "deepseek"
    requested_model: str = Field(default="deepseek-flash", min_length=1)
    # 只记录密钥来源名称；凭据值从不进入可序列化配置。
    api_key_env: Literal["DEEPSEEK_API_KEY"] = "DEEPSEEK_API_KEY"
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
    # 每个配置创建独立的限制对象，避免不同运行共享可变设置。
    limits: RunLimits = Field(default_factory=RunLimits)
    # 每次运行独立的指令加载与整体模型输入预算策略。
    context: ContextPolicy = Field(default_factory=ContextPolicy)

    @model_validator(mode="after")
    def validate_config(self) -> "RunConfig":
        """校验模型、窗口及工具/技能选择，返回自身；重复或非法选择抛 ValueError。"""
        if not self.requested_model.strip() or not self.system_prompt.strip():
            raise ValueError("模型名和系统提示词不能为空白")
        window = self.context.model_context_tokens
        if window is not None and window <= self.limits.model_max_output_tokens + self.context.token_safety_margin:
            raise ValueError("模型窗口必须大于输出预留与安全余量之和")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("工具名称不能重复")
        if len(self.skills) != len(set(self.skills)):
            raise ValueError("技能名称不能重复")
        for name in self.skills:
            if len(name) > 64 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None:
                raise ValueError("技能名称必须为不超过64字符的小写字母、数字和单连字符")
        if len(self.skill_roots) != len(set(self.skill_roots)):
            raise ValueError("技能目录不能重复")
        return self


# 宿主预先指定的验收程序及其工作目录、超时；描述入口，不负责执行验收。
class VerifierSpec(DataModel):
    argv: list[str] = Field(min_length=1)  # 宿主固定程序和参数，启动预检后 argv[0] 为绝对可执行入口。
    cwd: Path | None = None  # 验收进程目录；清单默认取清单所在目录，程序调用 None 时取宿主当前目录。
    timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)  # 验收自身上限（秒），仍受总运行截止时间约束。
    python_paths: list[Path] = Field(default_factory=list)  # 宿主显式登记的绝对 Python 搜索目录，必须位于 cwd 内；空列表不注入搜索路径。

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
class TaskSpec(DataModel):
    prompt: str = Field(min_length=1)
    workspace: Path
    verifier: VerifierSpec | None = None
    expected_outputs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_task(self) -> "TaskSpec":
        """拒绝空白任务，并要求启动边界已将工作目录解析为绝对路径。"""
        if not self.prompt.strip():
            raise ValueError("任务描述不能为空白")
        if not self.workspace.is_absolute():
            raise ValueError("工作目录必须在启动时解析为绝对路径")
        return self


# 网页启动时固定的宿主登记来源；后续轮次不能由浏览器改成任意目录或验收程序。
class RunOrigin(DataModel):
    kind: Literal["workspace", "project"]  # 普通文件任务或 Git 项目。
    preset_id: str = Field(min_length=1, max_length=64)  # 启动时的宿主登记编号。
    registered_path: Path  # 宿主登记根目录；与每轮独立实际工作目录分别保存。
    verifier_id: str | None = None  # 普通任务选择的宿主验收编号；项目任务使用项目测试。


# 后续运行固定的父历史来源；实际消息由父事件重建，并在调用模型前重新核验。
class HistorySource(DataModel):
    last_seq: int = Field(gt=0)  # 接纳追加时父事件的最后序号。
    messages_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")  # 去掉旧宿主前缀后的完整消息摘要。
    interrupted: bool = False  # 宿主明确接纳中断后继续；旧记录默认False，网页不能设置来源。


# 运行创建时保存的登记快照，使历史记录保留当时的任务、配置和运行时版本。
class RunManifest(DataModel):
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


# 一次运行的当前或最终结果，分别记录执行、验收、清理状态以及回答与计数。
class RunResult(DataModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    # 执行结束与验收通过分别记录：completed 本身不代表产物已经符合要求。
    execution_status: Literal[
        "created", "running", "completed", "failed", "cancelled", "timed_out", "limit_exceeded"
    ] = "created"
    validation_status: Literal["unverified", "passed", "failed", "error"] = "unverified"
    verification: VerificationResult | None = None  # None 表示未运行或旧记录没有详情；非空时 status 必须与 validation_status 相同。
    cleanup_status: Literal["not_needed", "complete", "incomplete", "unknown"] = "not_needed"
    stop_reason: str | None = None
    # 统计已发起的逻辑轮次、HTTP 尝试及接纳处理的工具调用，不把重试重复算轮次。
    model_turns: int = Field(default=0, ge=0)
    http_attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    final_answer: str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
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
