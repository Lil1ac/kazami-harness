"""预检并冻结 12 个独立的新题输入。前序工具回执来自本机真实读取和测试。"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_harness.context.compression import encode
from agent_harness.schemas import ContextPolicy
from agent_harness.tools.files import ReadFileArguments, read_file

from log_tasks import CASES as LOG_CASES
from replay import _input_content, replay_tools


HERE = Path(__file__).resolve().parent  # 只在当前评测目录使用题集模块。
OUTPUT = HERE.parent.parent / "output" / HERE.name  # 生成数据集中到工作区输出目录。
POLICY = ContextPolicy(compression="hybrid", budget_projection=False, root_handoff=False)  # 锁定产品 256 KiB 等默认值。
SYSTEM = "你是一名仓库维护助手。遵循当前用户要求，并核对早期约定、最新代码和工具结果。"  # 两组共用前缀。

# 在看到压缩结果前固定每题目标原始输入量和读页规格。4 个低压、4 个临界、4 个超限。
LAYOUT = {
    "log": ((170, 5900), (226, 6100), (280, 9700), (295, 6600)),
    "cross_file": ((175, 9600), (186, 6700), (236, 9900), (285, 6300)),
    "multiturn": ((182, 6200), (223, 6300), (241, 9800), (290, 10500)),
}


def write_json(path: Path, data: object) -> None:
    """将 data 保存为 UTF-8 JSON，供校验、复跑和人工检查。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preflight_logs(directory: Path) -> dict:
    """运行四题的基线和参考解，验证预期断言，并记录用于历史的实际基线日志。"""
    directory.mkdir(parents=True, exist_ok=True)
    rows = {}
    for case in LOG_CASES:
        identifier = case["id"]
        row = {}
        for variant, expected in (("baseline", 1), ("reference", 0)):
            environment = os.environ.copy()
            environment.update(AH_REPLAY_LOG_CASE=identifier, AH_REPLAY_LOG_VARIANT=variant,
                               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONIOENCODING="utf-8",
                               PYTHONDONTWRITEBYTECODE="1")
            # 前序只使用用例名称与状态，回溯中显示的独立断言和评分代码不进入输入。
            command = [sys.executable, "-m", "pytest", "-vv", "--tb=no", "--color=no", "-p", "no:cacheprovider",
                       str(HERE / "log_tasks.py")]
            finished = subprocess.run(command, cwd=directory, env=environment, capture_output=True, timeout=45)
            stdout, stderr = (part.decode("utf-8", errors="strict") for part in (finished.stdout, finished.stderr))
            (directory / f"{identifier}-{variant}.log").write_text(stdout + stderr, encoding="utf-8")
            markers = ("1 failed", "24 passed", "test_new_contract FAILED") if expected else ("25 passed",)
            if finished.returncode != expected or not all(marker in stdout for marker in markers):
                raise ValueError(f"预检结果与预期不同：{identifier} {variant}。请检查 {directory}")
            row[variant] = {"exit_code": finished.returncode, "stdout": stdout, "stderr": stderr}
        rows[identifier] = row
    write_json(directory / "preflight.json", rows)
    return rows


def append_tool(messages: list[dict], name: str, parameters: dict, result: dict) -> int:
    """追加一个脚本化真实工具调用及对应回执，返回回执在消息序列中的零基下标。"""
    identifier = f"checkpoint-call-{len(messages):03}"
    messages.append({
        "role": "assistant", "content": None, "tool_calls": [
            {"id": identifier, "type": "function", "function": {"name": name, "arguments": encode(parameters)}}
        ],
    })
    messages.append({"role": "tool", "tool_call_id": identifier, "content": encode(result)})
    return len(messages) - 1


def page(workspace: Path, path: str, start: int, size: int) -> tuple[dict, dict]:
    """以实际 AgentHarness 文件工具读取 path 的一个字节范围，返回请求参数与成功回执。"""
    parameters = {"path": path, "offset": start, "max_bytes": size}
    data = read_file(workspace, OUTPUT / "runs", ReadFileArguments(**parameters), 12000, 1024 * 1024)
    return parameters, {"ok": True, "data": data, "error": None, "truncated": not data["eof"]}


def measure(messages: list[dict], tools: list[dict]) -> int:
    """按 ContextBuilder 的 JSON 格式计量同一模型输入，单位为 UTF-8 字节。"""
    return len(_input_content(messages, tools))


def pressure_band(size: int) -> str:
    """将原始输入 size 按同一硬上限和触发比例划为三个预注册档位。"""
    if size > POLICY.max_input_bytes:
        return "over"
    if size > POLICY.max_input_bytes * POLICY.trigger_ratio:
        return "near"
    return "low"


def freeze_case(case: dict, target_kib: int, page_size: int, tools: list[dict], log: dict | None) -> dict:
    """生成一题的新检查点，仅按原始输入压力停止收集，不读取压缩结果。"""
    workspace = Path(case["workspace"]).resolve(strict=True)
    evidence_path = case["evidence_file"]
    evidence_bytes = (workspace / evidence_path).read_bytes()
    anchor = case["anchor"]
    anchor_bytes = anchor.encode("utf-8")
    if evidence_bytes.count(anchor_bytes) != 1:
        raise ValueError(f"关键证据不唯一或不存在：{case['id']} {evidence_path}")
    anchor_at = evidence_bytes.index(anchor_bytes)
    start = max(0, min(anchor_at - page_size // 2, len(evidence_bytes) - page_size))
    # 只把页起点校到 UTF-8 字符边界，原始回执仍来自文件工具。
    while start and evidence_bytes[start] & 0xC0 == 0x80:
        start -= 1
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": case["early"]}]
    parameters, result = page(workspace, evidence_path, start, page_size)
    if anchor not in result["data"]["content"]:
        raise ValueError(f"证据未落在实际可见的文件页：{case['id']}")
    index = append_tool(messages, "read_file", parameters, result)
    files = {evidence_path: result["data"]["sha256"]}
    if log is not None:
        command = {"argv": ["python", "-m", "pytest", "-vv", "--tb=no", "--color=no",
                            "-p", "no:cacheprovider", str(HERE / "log_tasks.py")]}
        data = {"exit_code": log["exit_code"], "stdout": log["stdout"], "stderr": log["stderr"]}
        append_tool(messages, "run_command", command, {"ok": False, "data": data, "error": None, "truncated": False})
    messages.append({"role": "user", "content": "再查相关实现和回归覆盖，保留当前观察，到我给出最后要求后再修改。"})
    final = {"role": "user", "content": case["final"]}
    target = target_kib * 1024
    size = measure([*messages, final], tools)
    for path in dict.fromkeys(case["history_files"]):
        if path == evidence_path or size >= target:
            continue
        offset = 0
        while offset is not None and size < target:
            parameters, result = page(workspace, path, offset, page_size)
            data = result["data"]
            if data["bytes_read"] <= 0:
                break
            files[path] = data["sha256"]
            append_tool(messages, "read_file", parameters, result)
            offset = data["next_offset"]
            size = measure([*messages, final], tools)
    if size < target or pressure_band(size) != pressure_band(target) or size >= target + page_size * 2:
        raise ValueError(f"真实文件页无法形成预定检查点：{case['id']}，{size} 字节，目标 {target} 字节")
    messages.append(final)
    return {
        "id": case["id"], "title": case["title"], "source": case["source"], "category": case["category"],
        "band": pressure_band(size), "workspace": str(workspace), "page_size": page_size,
        "planned_target_kib": target_kib, "input_bytes": size, "source_files": files, "messages": messages,
        "tools": tools, "checkpoint_sha256": sha256(_input_content(messages, tools)).hexdigest(),
        "expected_evidence": [{"message_index": index, "contains": anchor, "path": evidence_path}],
    }


def main() -> None:
    """可单独做日志预检，或在各题预检完成后生成全套 12 个冻结检查点。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-only", action="store_true", help="先独立验证日志诊断题。")
    options = parser.parse_args()
    logs = preflight_logs(OUTPUT / "log-preflight")
    if options.logs_only:
        return

    from cross_file.cases import CASES as CROSS_FILE_CASES
    from multiturn.cases import CASES as MULTITURN_CASES
    tools = replay_tools(POLICY)
    groups = {"log": LOG_CASES, "cross_file": CROSS_FILE_CASES, "multiturn": MULTITURN_CASES}
    checkpoints = [
        freeze_case(case, target_kib, page_size, tools, logs.get(case["id"], {}).get("baseline"))
        for group, cases in groups.items()
        for case, (target_kib, page_size) in zip(cases, LAYOUT[group], strict=True)
    ]
    if Counter(item["band"] for item in checkpoints) != {"low": 4, "near": 4, "over": 4}:
        raise ValueError("检查点的压力分层必须各有四题。")
    if len({item["id"] for item in checkpoints}) != 12:
        raise ValueError("正式评测必须包含十二个不同题号。")
    for item in checkpoints:
        write_json(OUTPUT / "checkpoints" / (item["id"] + ".json"), item)
    write_json(OUTPUT / "manifest.json", {
        "policy": POLICY.model_dump(mode="json"), "source": "新编维护需求 + 已安装的实际开源发行包文件",
        "prelude": "脚本化调用产品文件读取工具，日志题含本地 pytest 实际失败回执",
        "cases": [{key: item[key] for key in ("id", "category", "band", "input_bytes", "checkpoint_sha256")} for item in checkpoints],
    })


if __name__ == "__main__":
    main()
