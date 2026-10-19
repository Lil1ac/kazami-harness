"""运行循环依赖的最小模型接口，不向上层暴露 HTTPX 响应。"""

from dataclasses import dataclass
from typing import Protocol


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

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和可展示说明，供 Runner 记录失败原因。"""
        super().__init__(message)
        self.code = code
        self.message = message


class ModelClient(Protocol):
    """Agent 循环依赖的最小异步模型协议，使供应商实现可替换、可离线测试。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """完成一个模型轮次；内部 HTTP 重试仍属于同一轮。"""
        ...
