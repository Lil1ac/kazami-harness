"""阿里云百炼北京地域的 Qwen 文本与工具调用适配器。"""

from agent_harness.providers.chat import ChatCompletionsClient


class QwenClient(ChatCompletionsClient):
    """使用百炼北京地域的兼容接口，复用公共流式协议。"""

    endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"  # 百炼北京地域的官方公共入口。
    options = {"enable_thinking": False}  # 首版使用支持工具调用的非思考文本模型。

    def _request_messages(self, messages: list[dict]) -> list[dict]:
        """按百炼工具示例将只有工具的 assistant 空正文编码为空串，返回请求副本。

        messages 为已校验的对话历史；只转换待发送消息，保留落盘历史与工具编号。
        """
        return [
            {**message, "content": ""} if message.get("role") == "assistant" and message.get("tool_calls") and message.get("content") is None else message
            for message in messages
        ]
