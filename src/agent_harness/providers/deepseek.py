"""DeepSeek 官方 Chat Completions 协议的固定配置。"""

from agent_harness.providers.chat import ChatCompletionsClient


class DeepSeekClient(ChatCompletionsClient):
    """在 DeepSeek 官方端点使用工具调用与流式输出。"""

    endpoint = "https://api.deepseek.com/chat/completions"  # DeepSeek 官方认证主机。
    options = {"thinking": {"type": "disabled"}}  # 首版沿用非思考模式的工具行为。
