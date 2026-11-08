"""使用合成消息探测已登记的自定义模型，不读取项目或执行工具。"""

import asyncio
import json

from fastapi import FastAPI
from agent_harness.model_selection import ModelProvider, ModelVersion
from agent_harness.providers.base import ModelError
from agent_harness.providers.factory import create_model_client
from agent_harness.schemas import DataModel, RunConfig
from agent_harness.web.service import WebRunManager, WebServiceError

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "harness_probe",
        "description": "Return a fixed marker for a connectivity check.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
    },
}  # 只用来检查模型的工具调用格式，从不进入 Runner 工具分发。


class ModelProbeRequest(DataModel):
    """只接受用户当前看到的连接版本，探测内容由宿主固定。"""

    version: ModelVersion  # 页面已确认的连接版本。


def install_model_routes(app: FastAPI, manager: WebRunManager) -> None:
    """注册已登记自定义端点的探测入口，每个宿主同时只运行一次。"""
    lock = asyncio.Lock()

    @app.post("/api/model-providers/{provider}/probe")
    async def probe(provider: ModelProvider, body: ModelProbeRequest) -> dict:
        """用两次有界请求检查 provider 的正文流和工具流，返回受控状态。"""
        if lock.locked():
            raise WebServiceError(409, "model_probe_busy", "已有连接测试正在运行。")
        async with lock:
            manager.check_model_versions({provider: body.version})
            definition = manager.run_config.custom_providers[provider]
            config = RunConfig(provider=provider, custom_providers={provider: definition},
                               limits={"max_http_attempts_per_turn": 1, "model_turn_timeout_seconds": 20.0, "model_max_output_tokens": 128})
            status = {"connection": "not_tested", "authentication": "not_required" if not config.requires_model_key else "not_tested",
                      "stream": "not_tested", "tools": "not_tested", "error_code": None}
            try:
                async with create_model_client(config, manager.api_key(config), transport=manager._transport) as client:
                    response = await client.complete([{"role": "user", "content": "Reply with the word OK."}], [])
                    if response.tool_calls or not response.message["content"]:
                        raise ModelError("model_protocol_error", "连接测试未收到正文。")
                    status.update(connection="passed", stream="passed")
                    if config.requires_model_key:
                        status["authentication"] = "passed"
                    manager.check_model_versions({provider: body.version})
                    response = await client.complete(
                        [{"role": "user", "content": 'Call the harness_probe tool with value "ok".'}], [_PROBE_TOOL],
                    )
                    if len(response.tool_calls) == 1:
                        tool = response.tool_calls[0]
                        if tool.name == "harness_probe" and json.loads(tool.arguments_json) == {"value": "ok"}:
                            return {**status, "tools": "passed"}
                    return {**status, "tools": "failed", "error_code": "model_probe_tool_missing"}
            except ValueError:
                return {**status, "tools": "failed", "error_code": "model_protocol_error"}
            except ModelError as error:
                if status["stream"] == "passed":
                    status["tools"] = "failed"
                elif error.code == "model_credentials_invalid":
                    status["authentication"] = "failed"
                elif error.code == "model_authentication":
                    status.update(connection="passed", authentication="failed")
                elif error.code in {"model_transport", "model_tls_verification", "model_timeout"}:
                    status["connection"] = "failed"
                else:
                    status.update(connection="passed", stream="failed")
                return {**status, "error_code": error.code}
