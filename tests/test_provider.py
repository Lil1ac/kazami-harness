"""离线验证供应商协议、可重试边界与单轮超时，不访问真实模型。"""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from agent_harness.schemas import RunConfig, RunLimits
from agent_harness.providers import DeepSeekClient, ModelError, ToolCall


SECRET = "test-only-api-key-never-record-this"
MESSAGES = [{"role": "user", "content": "读取日志"}]
TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}]


def envelope(message=None, finish_reason="stop", **extra):
    """构造单候选模型响应，允许覆盖消息、结束原因和元数据以测试协议边界。"""
    payload = {
        "id": "response-123",
        "model": "actual-response-model",
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": message or {"role": "assistant", "content": "已完成"},
        }],
    }
    payload.update(extra)
    return payload


def tool_call(identifier="call-1", name="read_file", arguments='{"path":"demo.log"}'):
    """构造供应商格式的函数调用，保留调用 ID、工具名和原始参数字符串。"""
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def invoke(handler, config=None, on_attempt=None):
    """使用离线 HTTP 处理器同步完成一次真实适配器请求，并可观察重试次数。"""
    async def operation():
        """在异步上下文中打开客户端并发送消息，退出时关闭 HTTP 资源。"""
        async with DeepSeekClient(
            SECRET, config or RunConfig(),
            transport=httpx.MockTransport(handler), on_attempt=on_attempt,
        ) as client:
            return await client.complete(deepcopy(MESSAGES), deepcopy(TOOLS))
    return asyncio.run(operation())


def test_request_and_response_metadata_are_preserved():
    """验证请求参数与认证构造正确，响应标识、实际模型和已知用量得到保留。"""
    captured = []
    def handler(request):
        """捕获真实构造的请求，返回含用量和指纹的响应以检查字段筛选。"""
        captured.append(request)
        return httpx.Response(200, json=envelope(
            system_fingerprint="fingerprint-1",
            usage={
                "prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22,
                "prompt_cache_hit_tokens": 2, "unknown_cost": SECRET,
            },
        ))
    config = RunConfig(requested_model="requested-model", limits=RunLimits(model_max_output_tokens=123))
    result = invoke(handler, config=config)
    request = captured[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert body == {
        "model": "requested-model", "messages": MESSAGES, "tools": TOOLS,
        "stream": False, "thinking": {"type": "disabled"}, "max_tokens": 123,
    }
    assert result.response_model == "actual-response-model"
    assert result.response_id == "response-123"
    assert result.system_fingerprint == "fingerprint-1"
    assert result.usage == {
        "prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22,
        "prompt_cache_hit_tokens": 2,
    }
    assert result.tool_calls == []


def test_multiple_calls_keep_arguments_and_reasoning_for_followup():
    """验证多工具调用和 reasoning_content 完整保留，后续请求可回传原消息。"""
    message = {
        "role": "assistant", "content": None, "reasoning_content": "下一步读取两个文件",
        "tool_calls": [tool_call(), tool_call("call-2", arguments="this-is-not-json")],
    }
    attempts = []
    requests = []
    def handler(request):
        """保存每次请求内容，并持续返回包含多工具调用的固定响应。"""
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(message, "tool_calls"))
    async def operation():
        """连续请求两轮，将首轮完整 assistant 消息放入第二轮历史以验证回传。"""
        async with DeepSeekClient(
            SECRET, RunConfig(), transport=httpx.MockTransport(handler), on_attempt=attempts.append,
        ) as client:
            first = await client.complete(deepcopy(MESSAGES), deepcopy(TOOLS))
            await client.complete([*MESSAGES, first.message], deepcopy(TOOLS))
            return first
    result = asyncio.run(operation())
    assert result.message == message
    assert requests[1]["messages"][-1] == message
    assert result.tool_calls == [
        ToolCall("call-1", "read_file", '{"path":"demo.log"}'),
        ToolCall("call-2", "read_file", "this-is-not-json"),
    ]
    assert attempts == [1, 1]


@pytest.mark.parametrize("message,finish_reason", [
    ({"role": "user", "content": "wrong role"}, "stop"),
    ({"role": "assistant", "content": " "}, "stop"),
    ({"role": "assistant", "content": None}, "stop"),
    ({"role": "assistant", "content": []}, "stop"),
    ({"role": "assistant", "content": "ok", "reasoning_content": {}}, "stop"),
    ({"role": "assistant", "tool_calls": [tool_call(), tool_call()]}, "tool_calls"),
    ({"role": "assistant", "tool_calls": [tool_call("")]}, "tool_calls"),
    ({"role": "assistant", "tool_calls": [tool_call(name="")]}, "tool_calls"),
    ({"role": "assistant", "tool_calls": [tool_call(arguments={})]}, "tool_calls"),
    ({"role": "assistant", "tool_calls": [tool_call()]}, "stop"),
    ({"role": "assistant", "tool_calls": []}, "tool_calls"),
    ({"role": "assistant", "tool_calls": "bad"}, "tool_calls"),
    ({"role": "assistant", "content": "ok"}, {}),
    ({"role": "assistant", "content": "ok"}, "unknown"),
])
def test_invalid_protocol_is_not_retried(message, finish_reason):
    """参数化验证消息角色、调用 ID、参数类型和结束原因等协议错误立即失败。"""
    attempts = []
    def handler(request):
        """返回当前参数化用例指定的非法响应，供协议校验测试使用。"""
        return httpx.Response(200, json=envelope(message, finish_reason))
    with pytest.raises(ModelError) as error:
        invoke(handler, on_attempt=attempts.append)
    assert error.value.code == "model_protocol_error"
    assert attempts == [1]


@pytest.mark.parametrize("finish_reason,code", [
    ("length", "model_output_truncated"),
    ("content_filter", "model_content_filtered"),
])
def test_incomplete_response_never_returns_executable_tools(finish_reason, code):
    """验证截断或被过滤的响应直接报错，即使带有工具调用也不交付执行。"""
    with pytest.raises(ModelError) as error:
        invoke(lambda request: httpx.Response(200, json=envelope(
            {"role": "assistant", "tool_calls": [tool_call()]}, finish_reason,
        )))
    assert error.value.code == code


@pytest.mark.parametrize("status,code", [
    (400, "model_bad_request"), (401, "model_authentication"),
    (403, "model_authentication"), (422, "model_bad_request"),
    (501, "model_unavailable"), (302, "model_protocol_error"),
])
def test_non_retryable_status_never_exposes_body_or_follows_redirect(status, code):
    """验证不可重试状态只尝试一次，错误不含正文，且不跟随重定向。"""
    attempts = []
    seen_urls = []
    def handler(request):
        """记录请求地址并返回含敏感正文和外部跳转地址的响应。"""
        seen_urls.append(str(request.url))
        return httpx.Response(status, text=SECRET, headers={"Location": "https://untrusted.example"})
    with pytest.raises(ModelError) as error:
        invoke(handler, on_attempt=attempts.append)
    assert error.value.code == code
    assert SECRET not in str(error.value)
    assert attempts == [1]
    assert seen_urls == ["https://api.deepseek.com/chat/completions"]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_has_bounded_attempts(status):
    """验证临时服务错误或限流只重试到配置上限，失败说明不含响应正文。"""
    attempts = []
    with pytest.raises(ModelError) as error:
        invoke(
            lambda request: httpx.Response(status, text=SECRET),
            config=RunConfig(limits=RunLimits(max_http_attempts_per_turn=2)),
            on_attempt=attempts.append,
        )
    assert error.value.code in {"model_rate_limited", "model_unavailable"}
    assert SECRET not in str(error.value)
    assert attempts == [1, 2]


def test_transient_status_then_success_keeps_single_model_turn():
    """验证同一逻辑请求先失败后成功时在适配器内重试，并返回正常回答。"""
    attempts = []
    def handler(request):
        """首次模拟 503，后续返回成功响应，供故障恢复测试使用。"""
        if len(attempts) == 1:
            return httpx.Response(503, text=SECRET)
        return httpx.Response(200, json=envelope())
    result = invoke(handler, on_attempt=attempts.append)
    assert result.message["content"] == "已完成"
    assert attempts == [1, 2]


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_transient_transport_failure_is_retried_and_sanitized(exception_type):
    """验证连接错误和读取超时有限重试，最终错误不泄露底层异常正文。"""
    attempts = []
    def handler(request):
        """抛出当前用例指定的网络异常，并在正文中放入敏感标记以检查脱敏。"""
        raise exception_type(SECRET, request=request)
    with pytest.raises(ModelError) as error:
        invoke(
            handler, config=RunConfig(limits=RunLimits(max_http_attempts_per_turn=2)),
            on_attempt=attempts.append,
        )
    assert error.value.code == "model_transport"
    assert SECRET not in str(error.value)
    assert attempts == [1, 2]


def test_transport_protocol_error_is_not_retried():
    """验证远端协议错误立即转换为受控传输错误，不执行重试。"""
    attempts = []
    def handler(request):
        """模拟不可重试的远端协议异常，检查异常转换与尝试次数。"""
        raise httpx.RemoteProtocolError(SECRET, request=request)
    with pytest.raises(ModelError) as error:
        invoke(handler, on_attempt=attempts.append)
    assert error.value.code == "model_transport"
    assert SECRET not in str(error.value)
    assert attempts == [1]


def test_invalid_json_is_sanitized_and_not_retried():
    """验证成功状态码下的非法 JSON 被判为协议错误，不回显正文也不重试。"""
    attempts = []
    with pytest.raises(ModelError) as error:
        invoke(lambda request: httpx.Response(200, text=SECRET), on_attempt=attempts.append)
    assert error.value.code == "model_protocol_error"
    assert SECRET not in str(error.value)
    assert attempts == [1]


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": True}, {"total_tokens": -1}, {"completion_tokens": "2"},
    {"completion_tokens_details": {"reasoning_tokens": -2}}, [],
])
def test_usage_rejects_invalid_token_counts(usage):
    """验证 token 计数拒绝布尔值、负数、字符串和错误结构。"""
    with pytest.raises(ModelError) as error:
        invoke(lambda request: httpx.Response(200, json=envelope(usage=usage)))
    assert error.value.code == "model_protocol_error"


def test_missing_usage_is_unknown_instead_of_zero():
    """验证供应商未提供用量时保留 None，避免将未知计费误认为零。"""
    result = invoke(lambda request: httpx.Response(200, json=envelope()))
    assert result.usage is None


def test_single_turn_timeout_includes_http_request():
    """验证单轮墙钟预算能中断仍在等待响应的 HTTP 请求。"""
    attempts = []
    async def handler(request):
        """模拟耗时超过单轮预算的服务响应，让外层超时取消等待。"""
        await asyncio.sleep(1)
        return httpx.Response(200, json=envelope())
    with pytest.raises(ModelError) as error:
        invoke(handler, config=RunConfig(limits=RunLimits(model_turn_timeout_seconds=0.02)), on_attempt=attempts.append)
    assert error.value.code == "model_timeout"
    assert attempts == [1]


def test_single_turn_timeout_includes_retry_backoff():
    """验证重试前的退避等待也计入单轮预算，超时后不再发下一次请求。"""
    attempts = []
    with pytest.raises(ModelError) as error:
        invoke(
            lambda request: httpx.Response(429, text=SECRET),
            config=RunConfig(limits=RunLimits(model_turn_timeout_seconds=0.02)),
            on_attempt=attempts.append,
        )
    assert error.value.code == "model_timeout"
    assert attempts == [1]


def test_cancellation_propagates_without_retry_or_reclassification():
    """验证用户取消原样向上传播，不被误分类为网络失败或触发重试。"""
    attempts = []
    async def handler(request):
        """在请求中直接抛出取消信号，模拟上层中断网络等待。"""
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        invoke(handler, on_attempt=attempts.append)
    assert attempts == [1]


@pytest.mark.parametrize("invalid_key", ["", "contains space", "bad\nheader", "非ASCII-secret-marker"])
def test_invalid_credentials_never_reach_header_encoding(invalid_key):
    """验证空白、控制字符和非 ASCII 密钥在构造认证头之前即被安全拒绝。"""
    with pytest.raises(ModelError) as error:
        DeepSeekClient(invalid_key, RunConfig())
    assert error.value.code == "model_credentials_invalid"
    assert not invalid_key or invalid_key not in str(error.value)


def test_client_requires_async_context_and_can_omit_tools():
    """验证客户端必须先进入异步上下文，且无工具时请求不包含 tools 字段。"""
    captured = []
    def handler(request):
        """捕获无工具请求的实际 JSON，并返回正常响应供上下文测试使用。"""
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=envelope())
    async def operation():
        """先验证上下文外请求失败，再进入上下文发送一次无工具请求。"""
        client = DeepSeekClient(SECRET, RunConfig(), transport=httpx.MockTransport(handler))
        with pytest.raises(ModelError) as error:
            await client.complete(MESSAGES, [])
        assert error.value.code == "model_not_ready"
        async with client:
            await client.complete(MESSAGES, [])
    asyncio.run(operation())
    assert "tools" not in captured[0]
