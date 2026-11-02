"""增量组装 Chat Completions 响应；展示更新与完整工具协议验证分开。"""

from collections.abc import Callable
import json
import time

import httpx

from .base import ModelDelta, ModelError


class StreamAssembler:
    """聚合单候选 SSE、工具参数和用量，并以有界频率发布可持久化的展示增量。"""

    def __init__(self, observer: Callable[[ModelDelta], None] | None) -> None:
        """observer 接收真实增量；None 只组装结果。回调错误向上传播，禁止重试副作用。"""
        self.observer = observer  # 可选宿主观察器，不承担工具分发。
        self.payload: dict = {"choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": None}]}  # 最终统一响应结构。
        self.calls: dict[int, dict] = {}  # 按供应商 index 累计的工具，最终按 index 排序。
        self.pending: dict[tuple[str, int | None], ModelDelta] = {}  # 每通道待发布片段；首片立即发布，随后合并。
        self.last_publish = 0.0  # 单调秒，限制连续磁盘提交与前端更新频率。
        self.received_content = False  # 收到文字或工具后禁止透明重试，以免重复输出或重复动作。
        self.finished = False  # 供应商已经给出可信 finish_reason，仍等待 [DONE]。

    def flush(self) -> None:
        """按接收顺序发布待处理片段并清空；无参数、无返回值，可能传播观察器存储错误。"""
        pending, self.pending = self.pending, {}
        if self.observer is not None:
            for delta in pending.values():
                self.observer(delta)
        self.last_publish = time.monotonic()

    def publish(self, delta: ModelDelta) -> None:
        """合并 delta 到同一内容通道；最多约每 80ms 或累计 2KiB 发布，首片立即可见。"""
        self.received_content = True
        key = (delta.channel, delta.tool_index)
        if self.pending and key not in self.pending:
            self.flush()  # 通道边界先落盘，保证文字与工具的顺序。
        previous = self.pending.get(key)
        self.pending[key] = ModelDelta(delta.channel, (previous.text if previous else "") + delta.text,
                                       delta.tool_index, delta.tool_id, delta.tool_name)
        if time.monotonic() - self.last_publish >= 0.08 or len(self.pending[key].text) >= 2048:
            self.flush()

    def accept(self, chunk: object) -> None:
        """校验并累计一个 SSE JSON chunk；非法结构抛受控 ModelError，返回 None。"""
        if not isinstance(chunk, dict) or "error" in chunk:
            raise ModelError("model_protocol_error", "模型流格式无效，未执行本轮工具。")
        for key in ("model", "id", "system_fingerprint"):
            value = chunk.get(key)
            if value is not None:
                if not isinstance(value, str) or (key in self.payload and self.payload[key] != value):
                    raise ModelError("model_protocol_error", "模型流标识不一致，未执行本轮工具。")
                self.payload[key] = value
        if chunk.get("usage") is not None:
            self.payload["usage"] = chunk["usage"]
        choices = chunk.get("choices")
        if choices == []:  # 某些端点使用独立的结束用量帧。
            return
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelError("model_protocol_error", "模型流候选无效，未执行本轮工具。")
        choice = choices[0]
        if choice.get("index", 0) != 0 or self.finished:
            raise ModelError("model_protocol_error", "模型流顺序无效，未执行本轮工具。")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("role", "assistant") != "assistant":
            raise ModelError("model_protocol_error", "模型流内容无效，未执行本轮工具。")
        message = self.payload["choices"][0]["message"]
        for field, channel in (("content", "text"), ("reasoning_content", "reasoning")):
            value = delta.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ModelError("model_protocol_error", "模型文字片段无效，未执行本轮工具。")
                message[field] = (message.get(field) or "") + value
                if value:
                    self.publish(ModelDelta(channel, value))
        raw_calls = delta.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelError("model_protocol_error", "工具流格式无效，未执行本轮工具。")
        for raw in raw_calls:
            if not isinstance(raw, dict) or type(raw.get("index")) is not int or raw["index"] < 0 or raw["index"] > 1023:
                raise ModelError("model_protocol_error", "工具流索引无效，未执行本轮工具。")
            index = raw["index"]
            call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if raw.get("type", "function") != "function" or not isinstance(raw.get("function", {}), dict):
                raise ModelError("model_protocol_error", "工具流类型无效，未执行本轮工具。")
            function = raw.get("function", {})
            for source, target, key in ((raw, call, "id"), (function, call["function"], "name"), (function, call["function"], "arguments")):
                value = source.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ModelError("model_protocol_error", "工具流字段无效，未执行本轮工具。")
                    target[key] += value
            self.publish(ModelDelta("tool_arguments", function.get("arguments") or "", index, call["id"] or None, call["function"]["name"] or None))
        reason = choice.get("finish_reason")
        if reason is not None:
            self.payload["choices"][0]["finish_reason"] = reason
            self.finished = True

    async def read(self, response: httpx.Response) -> dict:
        """从 response 读取 data-only SSE，返回完整供应商结构；断流保留已展示前缀并报错。"""
        done = False
        try:
            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:") or len(line) > 1_048_576:
                    raise ModelError("model_protocol_error", "模型 SSE 帧无效，未执行本轮工具。")
                value = line[5:].strip()
                if value == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(value)
                except (ValueError, UnicodeError):
                    raise ModelError("model_protocol_error", "模型流 JSON 无效，未执行本轮工具。") from None
                self.accept(chunk)
        finally:
            self.flush()  # 取消或故障也持久化最后收到的可读前缀，不能冒充完整回答。
        if not done or not self.finished:
            raise ModelError("model_stream_interrupted", "模型输出连接中断，已保留收到的内容，未执行未完成工具。")
        if self.calls:
            if sorted(self.calls) != list(range(len(self.calls))):
                raise ModelError("model_protocol_error", "工具流索引不连续，未执行本轮工具。")
            self.payload["choices"][0]["message"]["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        return self.payload
