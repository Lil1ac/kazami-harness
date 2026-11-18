"""用固定工具动作离线贯穿真实 Agent Loop，单独标记脚本来源，不计入模型试次。"""

import argparse
import asyncio
import base64
import json
from pathlib import Path

from agent_harness.evaluation.engine import evaluate
from agent_harness.evaluation.reproduction import patch_paths
from agent_harness.providers.base import ModelResponse, ToolCall
from verify import DATA, DJANGO, PROBE, PYTHON, RECORDS, experiment, read, save, settings


class ScriptedRepair:
    """用已知正确修复验证复现、选择交付和正常收尾的接口契约。"""

    def __init__(self):
        """将仅用于离线回归的探针和补丁编码为容器工具参数，不使用模型服务。"""
        patch = read(RECORDS / (DJANGO + ".json"))["patch"]
        probe64 = base64.b64encode(PROBE.read_bytes()).decode()
        patch64 = base64.b64encode(patch.encode()).decode()
        self.actions = iter([
            ["bash", "-c", 'echo "$1" | base64 -d >/tmp/harness-repro.py', "probe", probe64],
            ["harness-reproduce"],
            ["bash", "-c", 'echo "$1" | base64 -d | git apply -', "edit", patch64],
            ["harness-deliver", *patch_paths(patch)],
        ])  # 每一项走真实工具参数校验和回执，最后一次必须在干净副本重跑。
        self.turn = 0  # 本脚本响应序号，与付费调用统计无关。

    async def complete(self, messages, tools):
        """核对 messages 里上一步工具回执，按 tools 中声明的接口返回下一步或最终答复。"""
        if messages[-1]["role"] == "tool":
            receipt = json.loads(messages[-1]["content"])
            assert receipt["ok"], receipt
        cmd = next(self.actions, None)
        self.turn += 1
        calls = []
        message = {"role": "assistant", "content": "已完成离线脚本交付。"}
        if cmd:
            call = ToolCall(call_id=f"local-{self.turn}", name="run_command", arguments_json=json.dumps({"argv": cmd}))
            calls.append(call)
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}]}
        return ModelResponse(message=message, tool_calls=calls, response_model="scripted",
                             usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                             finish_reason="tool_calls" if calls else "stop")


async def main(data: Path = DATA):
    """在 data 的冻结预检上运行离线脚本，核对交付和评分 SHA，默认使用首轮目录。"""
    cfg = experiment().model_copy(update={"max_cost_cents": None})
    result = await evaluate(data / "suite.json", settings(), cfg, data / "runs", data / "w", "scripted",
                            client_factory=ScriptedRepair, swe_python=PYTHON,
                            required_preflight=read(data / "offline.json")["preflight"])
    assert result["mode"] == "scripted" and len(result["trials"]) == 1, result.get("preflight_error")
    row = result["trials"][0]
    assert row["success"] and row["entry"]["status"] == "verified" and row["grade"]["status"] == "passed", (
        row["stop_reason"], row["entry"], row["grade"]["status"])
    assert row["patch_sha256"] == row["entry"]["candidate"]["patch_sha256"]
    save(data / "scripted.json", {"report": result["id"], "external_requests": 0, "status": "passed"})
    print(result["id"], row["entry"]["status"], row["grade"]["status"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DATA, help="本轮零模型验收目录")
    asyncio.run(main(parser.parse_args().data_dir.resolve()))
