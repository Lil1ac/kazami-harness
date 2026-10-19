"""配置与运行记录的数据结构及校验规则；供应商消息由适配器保存。"""

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
    # 每个配置创建独立的限制对象，避免不同运行共享可变设置。
    limits: RunLimits = Field(default_factory=RunLimits)

    @model_validator(mode="after")
    def validate_config(self) -> "RunConfig":
        """在字段类型校验后拒绝空白模型名、空白提示词和重复工具名。"""
        if not self.requested_model.strip() or not self.system_prompt.strip():
            raise ValueError("模型名和系统提示词不能为空白")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("工具名称不能重复")
        return self


# 宿主预先指定的验收程序及其工作目录、超时；描述入口，不负责执行验收。
class VerifierSpec(DataModel):
    argv: list[str] = Field(min_length=1)
    cwd: Path | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_program(self) -> "VerifierSpec":
        """要求 argv 的第一个元素包含实际程序名，不能只由空白组成。"""
        if not self.argv[0].strip():
            raise ValueError("验收程序不能为空白")
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


# 运行创建时保存的登记快照，使历史记录保留当时的任务、配置和运行时版本。
class RunManifest(DataModel):
    # 数据格式版本用于读取兼容；runtime_version 另外标识生成记录的程序版本。
    schema_version: Literal[1] = 1
    run_id: UUID
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
    runtime_version: str = "0.1.0"
    task: TaskSpec
    config: RunConfig


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


# 一次运行的当前或最终结果，分别记录执行、验收、清理状态以及回答与计数。
class RunResult(DataModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    # 执行结束与验收通过分别记录：completed 本身不代表产物已经符合要求。
    execution_status: Literal[
        "created", "running", "completed", "failed", "cancelled", "timed_out", "limit_exceeded"
    ] = "created"
    validation_status: Literal["unverified", "passed", "failed", "error"] = "unverified"
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

    @model_validator(mode="after")
    def validate_result(self) -> "RunResult":
        """仅允许执行已完成的任务记录验收结果，防止准备或失败状态宣称验收通过。"""
        if self.validation_status != "unverified" and self.execution_status != "completed":
            raise ValueError("执行完成后才能记录验收结果")
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
