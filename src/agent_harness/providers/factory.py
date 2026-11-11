"""在宿主侧根据冻结配置选择模型客户端和凭据。"""

from collections.abc import Callable

import httpx

from agent_harness.providers.base import ModelDelta
from agent_harness.providers.chat import ChatCompletionsClient
from agent_harness.schemas import RunConfig

def create_model_client(
    config: RunConfig,
    api_key: str,
    *,
    on_attempt: Callable[[int], None] | None = None,
    on_delta: Callable[[ModelDelta], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatCompletionsClient:
    """用 config 中已登记的端点构造未打开的兼容客户端。

    api_key 仅用于该供应商认证；回调分别记录 HTTP 尝试和可展示增量。
    transport 为测试注入，None 使用真实 HTTPS。调用方负责异步关闭客户端。
    """
    return ChatCompletionsClient(api_key, config, on_attempt=on_attempt, on_delta=on_delta, transport=transport)
