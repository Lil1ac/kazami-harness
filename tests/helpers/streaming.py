"""离线模型测试共用的 SSE 编码，确保所有 HTTP 链路使用真正的流协议。"""

from copy import deepcopy
import json as jsonlib

import httpx


def model_stream_response(status_code: int, *, json: object) -> httpx.Response:
    """把测试的完整 json 响应编码为一个结束 chunk 加 DONE，返回 status_code 的 SSE 响应。

    保留故意损坏的字段以测试协议拒绝；工具补齐流协议必须的 index。不访问网络。
    """
    chunk = deepcopy(json)
    if isinstance(chunk, dict) and isinstance(chunk.get("choices"), list):
        for choice in chunk["choices"]:
            if not isinstance(choice, dict):
                continue
            choice["delta"] = choice.pop("message", None)
            choice.setdefault("index", 0)
            delta = choice["delta"]
            if isinstance(delta, dict) and isinstance(delta.get("tool_calls"), list):
                for index, call in enumerate(delta["tool_calls"]):
                    if isinstance(call, dict):
                        call["index"] = index
    content = "data: " + jsonlib.dumps(chunk, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
    return httpx.Response(status_code, content=content.encode("utf-8"), headers={"content-type": "text/event-stream"})
