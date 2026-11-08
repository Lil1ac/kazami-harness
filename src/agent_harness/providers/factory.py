"""在宿主侧根据冻结配置选择模型客户端和凭据。"""

from collections.abc import Callable

import httpx

from agent_harness.providers.base import ModelDelta
from agent_harness.providers.chat import ChatCompletionsClient, CustomChatClient
from agent_harness.providers.deepseek import DeepSeekClient
from agent_harness.providers.qwen import QwenClient
from agent_harness.schemas import RunConfig

_CLIENTS = {"deepseek": DeepSeekClient, "qwen": QwenClient}  # 供应商与固定官方端点一一绑定。


def create_model_client(
    config: RunConfig,
    api_key: str,
    *,
    on_attempt: Callable[[int], None] | None = None,
    on_delta: Callable[[ModelDelta], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatCompletionsClient:
    """按 config 选择适配器并返回未打开的客户端。

    api_key 仅用于该供应商认证；回调分别记录 HTTP 尝试和可展示增量。
    transport 为测试注入，None 使用真实 HTTPS。调用方负责异步关闭客户端。
    """
    adapter = _CLIENTS.get(config.provider, CustomChatClient)
    return adapter(api_key, config, on_attempt=on_attempt, on_delta=on_delta, transport=transport)
