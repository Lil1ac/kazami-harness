"""运行循环依赖的最小模型接口，不向上层暴露 HTTPX 响应。"""

from dataclasses import dataclass
from typing import Protocol, Literal


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型提出的单个工具调用，保留原始参数 JSON，交由工具层解码和校验。"""

    call_id: str
    name: str
    # 参数的 JSON 解码和工具输入校验属于工具分发层，模型层保留原始字符串。
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """一次逻辑模型请求的统一响应，保留可回传消息、工具调用及供应商元数据。"""

    message: dict
    tool_calls: list[ToolCall]
    finish_reason: str
    response_model: str
    response_id: str | None = None
    system_fingerprint: str | None = None
    usage: dict | None = None


class ModelError(Exception):
    """只传递稳定错误码与受控说明，避免凭据或服务端正文进入运行记录。"""

    def __init__(self, code: str, message: str, *, usage: dict | None = None, response_model: str | None = None,
                 transport_diagnostic: dict | None = None) -> None:
        """保存受控code/message与已校验usage；None表示用量未知，response_model为可选实际模型名。

        不保存响应正文；usage仅用于计量，不使被拒绝的内容或工具调用获得执行资格。
        transport_diagnostic 仅为宿主生成的阶段、异常类名及内容接收标记，None 表示非传输错误。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.usage = usage  # 供应商已返回且通过协议校验的计数；缺失为None。
        self.response_model = response_model  # 有效响应的实际模型名，传输失败时为None。
        self.usage_accounted = False  # 仅统一网关计入账本后置True，防止Runner覆盖已有完整性。
        self.transport_diagnostic = transport_diagnostic  # 不存异常正文、URL、请求头或凭据，不改变未知计量结论。


class ModelClient(Protocol):
    """Agent 循环依赖的最小异步模型协议，使供应商实现可替换、可离线测试。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """完成一个模型轮次；内部 HTTP 重试仍属于同一轮。"""
        ...


@dataclass(frozen=True, slots=True)
class ModelDelta:
    """已收到的模型增量；只用于展示，完整响应校验前不得触发工具执行。"""

    channel: Literal["text", "reasoning", "tool_arguments"]  # 正文、供应商推理内容或工具参数。
    text: str  # 本次新增文本，工具通道中为尚未完成的 JSON 片段。
    tool_index: int | None = None  # 工具在本次模型响应中的稳定位置；文字通道为空。
    tool_id: str | None = None  # 已收到的完整调用编号，尚未提供时为空。
    tool_name: str | None = None  # 当前累计工具名，尚未提供时为空。
