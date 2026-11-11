"""验证真正的分块 HTTP 流、首次文字时机、工具参数组装与断流重试边界。"""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from agent_harness.providers.base import ModelDelta, ModelError
from agent_harness.providers.chat import ChatCompletionsClient
from tests.helpers.models import configured_run


def frame(delta: dict, finish: str | None = None, **metadata: object) -> bytes:
    """编码 delta 为单候选 SSE 帧；finish 为结束原因，None 表示继续，返回 UTF-8 字节。"""
    value = {"id": "stream-a", "model": "offline", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **metadata}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode("utf-8")


class PacketStream(httpx.AsyncByteStream):
    """模拟跨网络包的 UTF-8/SSE，按约定在指定片段后等待消费确认或注入断网。"""

    def __init__(self, packets: list[bytes], *, gate: asyncio.Event | None = None, disconnect: bool = False) -> None:
        """保存 packets 原始包；gate 非空时首包后等待观察器确认，disconnect 为 True 时末尾断流。"""
        self.packets = packets  # 每次迭代交付的原始网络包，可以截在中文字符中间。
        self.gate = gate  # 首片消费信号；None 不等待。
        self.disconnect = disconnect  # 是否在输出前缀后抛网络错误，默认正常结束。

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """按顺序交付包并让出调度，返回异步字节流；注入错误不含可展示服务端内容。"""
        for index, packet in enumerate(self.packets):
            yield packet
            if index == 0 and self.gate is not None:
                await asyncio.wait_for(self.gate.wait(), 1)
            await asyncio.sleep(0)
        if self.disconnect:
            raise httpx.ReadError("sensitive transport detail")


def test_first_text_is_observed_before_response_finishes() -> None:
    """观察器必须先消费首字才能继续交付结束帧，证明没有等完整回答后伪装流式。"""
    async def operation() -> None:
        """在真实客户端中用消费信号解锁流末尾，核对完整正文和已知用量。"""
        gate = asyncio.Event()
        deltas: list[ModelDelta] = []

        def observe(delta: ModelDelta) -> None:
            """记录真实增量；首字到达立即通知网络替身继续。"""
            deltas.append(delta)
            gate.set()

        def handler(request: httpx.Request) -> httpx.Response:
            """返回门控 SSE；请求必须明确开启流式。"""
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(200, stream=PacketStream([frame({"content": "首字"}), frame({"content": "与结尾"}, "stop", usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}), b"data: [DONE]\n\n"], gate=gate))

        async with ChatCompletionsClient("offline-key", configured_run(), on_delta=observe, transport=httpx.MockTransport(handler)) as client:
            result = await client.complete([{"role": "user", "content": "测试"}], [])
        assert result.message["content"] == "首字与结尾"
        assert "".join(item.text for item in deltas) == result.message["content"]
        assert deltas[0].text == "首字"
        assert result.usage["total_tokens"] == 5

    asyncio.run(operation())


def test_fragmented_utf8_and_interleaved_tools_are_assembled() -> None:
    """中文跨字节边界与交错工具索引都能组装，参数完整后才交回可执行工具。"""
    body = b"".join([
        frame({"role": "assistant", "content": "先读取。"}),
        frame({"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}}]}),
        frame({"tool_calls": [{"index": 1, "id": "b", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"二.py"}'}}]}),
        frame({"tool_calls": [{"index": 0, "function": {"arguments": '"一.py"}'}}]}, "tool_calls"),
        b"data: [DONE]\n\n",
    ])
    deltas: list[ModelDelta] = []

    async def operation():
        """使用七字节包消费实际解码器，返回完整响应与工具参数。"""
        def handler(request: httpx.Request) -> httpx.Response:
            """为请求提供打散到 UTF-8 字符内部的包，不访问网络。"""
            return httpx.Response(200, stream=PacketStream([body[index:index + 7] for index in range(0, len(body), 7)]))
        async with ChatCompletionsClient("offline-key", configured_run(), on_delta=deltas.append, transport=httpx.MockTransport(handler)) as client:
            return await client.complete([{"role": "user", "content": "测试"}], [])

    result = asyncio.run(operation())
    assert [json.loads(call.arguments_json)["path"] for call in result.tool_calls] == ["一.py", "二.py"]
    assert {item.tool_index for item in deltas if item.channel == "tool_arguments"} == {0, 1}
    assert deltas[0].text == "先读取。"


@pytest.mark.parametrize("disconnect", [False, True])
def test_partial_stream_never_retries_or_returns_executable_tools(disconnect: bool) -> None:
    """已输出后缺少 DONE 或发生断网都保留前缀，不重试，不返回工具。"""
    deltas: list[ModelDelta] = []
    attempts: list[int] = []

    async def operation() -> None:
        """消费一个不完整流，确认错误稳定且不泄露底层网络异常。"""
        def handler(request: httpx.Request) -> httpx.Response:
            """交付正文和未闭合工具参数，随后按当前用例结束或断开。"""
            return httpx.Response(200, stream=PacketStream([frame({"content": "已收到的前缀"}), frame({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "write_file", "arguments": '{"path":'}}]})], disconnect=disconnect))
        async with ChatCompletionsClient("offline-key", configured_run(), on_attempt=attempts.append, on_delta=deltas.append, transport=httpx.MockTransport(handler)) as client:
            await client.complete([{"role": "user", "content": "测试"}], [])

    with pytest.raises(ModelError) as error:
        asyncio.run(operation())
    assert error.value.code == "model_stream_interrupted"
    assert attempts == [1]
    assert deltas[0].text == "已收到的前缀"
    assert "sensitive" not in str(error.value)
