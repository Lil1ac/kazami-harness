"""DeepSeek Chat Completions 的异步非流式适配器。"""

import asyncio
from copy import deepcopy
from types import TracebackType
from typing import Callable

import httpx

from agent_harness.schemas import RunConfig

from .base import ModelError, ModelResponse, ToolCall


_ENDPOINT = "https://api.deepseek.com/chat/completions"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
)
_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


def _protocol_error() -> ModelError:
    """生成不包含响应正文的协议错误，供各个响应校验分支统一使用。"""
    return ModelError("model_protocol_error", "模型响应格式无效，未执行本轮工具。")


def _optional_text(payload: dict, key: str) -> str | None:
    """读取可缺省的文本字段；字段存在且非空值时必须是字符串。"""
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise _protocol_error()
    return value


def _usage(payload: object) -> dict | None:
    """筛选并校验已知 token 用量字段；缺失时返回 None，不把未知用量当作零。"""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise _protocol_error()
    # 只保留可解释的 token 计数，未知供应商字段不进入公共契约。
    usage: dict = {}
    for field in _TOKEN_FIELDS:
        if field in payload:
            value = payload[field]
            if type(value) is not int or value < 0:
                raise _protocol_error()
            usage[field] = value
    if "completion_tokens_details" in payload:
        details = payload["completion_tokens_details"]
        if not isinstance(details, dict):
            raise _protocol_error()
        if "reasoning_tokens" in details:
            value = details["reasoning_tokens"]
            if type(value) is not int or value < 0:
                raise _protocol_error()
            usage["completion_tokens_details"] = {"reasoning_tokens": value}
    return usage


def _parse_response(payload: object) -> ModelResponse:
    """将单候选供应商响应校验为 ModelResponse，拒绝截断或无法可靠配对的调用。

    工具 ID 必须非空且不重复，结束原因必须与消息内容一致。
    本层保留原始工具参数字符串和必要 assistant 字段，参数是否合法由工具层判断。
    """
    if not isinstance(payload, dict):
        raise _protocol_error()
    model = payload.get("model")
    choices = payload.get("choices")
    if not isinstance(model, str) or not model.strip():
        raise _protocol_error()
    if not isinstance(choices, list) or len(choices) != 1:
        raise _protocol_error()
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _protocol_error()
    finish_reason = choice.get("finish_reason")
    # 即便截断响应带有看似完整的工具，也不能执行可能不完整的动作。
    if finish_reason == "length":
        raise ModelError("model_output_truncated", "模型输出达到长度限制，未执行本轮工具。")
    if finish_reason == "content_filter":
        raise ModelError("model_content_filtered", "模型输出被服务端过滤，未执行本轮工具。")
    if not isinstance(finish_reason, str) or finish_reason not in {"stop", "tool_calls"}:
        raise _protocol_error()
    raw_message = choice.get("message")
    if not isinstance(raw_message, dict) or raw_message.get("role") != "assistant":
        raise _protocol_error()
    content = _optional_text(raw_message, "content")
    _optional_text(raw_message, "reasoning_content")
    raw_calls = raw_message.get("tool_calls")
    if raw_calls is not None and not isinstance(raw_calls, list):
        raise _protocol_error()
    calls: list[ToolCall] = []
    identifiers: set[str] = set()
    for raw_call in raw_calls or []:
        if not isinstance(raw_call, dict) or raw_call.get("type") != "function":
            raise _protocol_error()
        call_id = raw_call.get("id")
        function = raw_call.get("function")
        if not isinstance(call_id, str) or not call_id.strip() or call_id in identifiers:
            raise _protocol_error()
        if not isinstance(function, dict):
            raise _protocol_error()
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
            raise _protocol_error()
        identifiers.add(call_id)
        calls.append(ToolCall(call_id, name, arguments))
    if calls:
        if finish_reason != "tool_calls":
            raise _protocol_error()
    elif finish_reason != "stop" or not isinstance(content, str) or not content.strip():
        raise _protocol_error()
    # 多轮工具协议需要原样回传 reasoning_content 与工具调用，不能只留下 content。
    message = {"role": "assistant", "content": content}
    for key in ("reasoning_content", "tool_calls"):
        if key in raw_message:
            message[key] = deepcopy(raw_message[key])
    return ModelResponse(
        message=message,
        tool_calls=calls,
        finish_reason=finish_reason,
        response_model=model,
        response_id=_optional_text(payload, "id"),
        system_fingerprint=_optional_text(payload, "system_fingerprint"),
        usage=_usage(payload.get("usage")),
    )


def _http_error(status_code: int) -> ModelError:
    """仅根据 HTTP 状态码生成受控错误，不把服务端正文或认证信息传给调用方。"""
    if status_code in {401, 403}:
        return ModelError("model_authentication", "模型服务拒绝认证，请检查环境变量中的 API 密钥。")
    if status_code == 429:
        return ModelError("model_rate_limited", "模型服务请求过于频繁，已用完本轮重试次数。")
    if 400 <= status_code < 500:
        return ModelError("model_bad_request", "模型服务拒绝了请求，请检查模型名与请求配置。")
    if status_code >= 500:
        return ModelError("model_unavailable", "模型服务暂时不可用，已停止本轮请求。")
    return _protocol_error()


class DeepSeekClient:
    """复用一个异步 HTTP 客户端完成 DeepSeek 请求，封装响应解析、有界重试与超时。"""

    def __init__(
        self,
        api_key: str,
        config: RunConfig,
        *,
        on_attempt: Callable[[int], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """校验密钥并保存配置副本；可注入尝试回调和测试传输层，此时尚未发请求。"""
        # 提前拒绝非法 header 字符，避免第三方异常包含原始凭据。
        if not isinstance(api_key, str) or not api_key or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
            raise ModelError("model_credentials_invalid", "API 密钥格式无效，请检查环境变量。")
        self._config = config.model_copy(deep=True)
        self._on_attempt = on_attempt
        self._entered = False
        # 固定官方地址、禁用环境代理与重定向，避免把 Authorization 发向其他主机。
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(config.limits.model_turn_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> "DeepSeekClient":
        """进入底层客户端的异步上下文，允许随后发送请求并复用连接池。"""
        await self._client.__aenter__()
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出异步上下文并关闭 HTTP 客户端资源，异常信息交由底层上下文处理。"""
        self._entered = False
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """发送一个非流式模型轮次，单轮预算同时覆盖请求、重试和退避等待。"""
        if not self._entered:
            raise ModelError("model_not_ready", "模型客户端必须在异步上下文中使用。")
        body = {
            "model": self._config.requested_model,
            "messages": messages,
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_tokens": self._config.limits.model_max_output_tokens,
        }
        if tools:
            body["tools"] = tools
        try:
            # 超时预算包含每次 HTTP 请求及退避，不能因重试把单轮时间扩大数倍。
            async with asyncio.timeout(self._config.limits.model_turn_timeout_seconds):
                return await self._complete_with_retries(body)
        except TimeoutError:
            raise ModelError("model_timeout", "模型轮次超时，已停止本轮请求。") from None

    async def _complete_with_retries(self, body: dict) -> ModelResponse:
        """在尝试次数内重发同一请求，仅重试选定的临时故障，成功后解析响应。"""
        attempts = self._config.limits.max_http_attempts_per_turn
        for attempt in range(1, attempts + 1):
            if self._on_attempt is not None:
                self._on_attempt(attempt)
            try:
                response = await self._client.post(_ENDPOINT, json=body)
            except _RETRYABLE_TRANSPORT_ERRORS:
                if attempt == attempts:
                    raise ModelError("model_transport", "模型服务连接失败，已用完本轮重试次数。") from None
            except httpx.RequestError:
                raise ModelError("model_transport", "模型请求传输失败，已停止本轮请求。") from None
            else:
                if response.status_code in _RETRYABLE_STATUSES and attempt < attempts:
                    pass
                elif response.status_code != 200:
                    raise _http_error(response.status_code)
                else:
                    try:
                        payload = response.json()
                    except (ValueError, UnicodeError):
                        raise _protocol_error() from None
                    return _parse_response(payload)
            # 固定上限防止服务端 Retry-After 或连续故障消耗无界等待时间。
            await asyncio.sleep(min(0.1 * (2 ** min(attempt - 1, 4)), 1.0))
        raise ModelError("model_unavailable", "模型服务暂时不可用，已停止本轮请求。")
