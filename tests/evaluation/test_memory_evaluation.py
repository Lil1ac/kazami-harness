"""用冻结开发题验证四组记忆消融、外部评分与实际证据链。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

from agent_harness.config import load_config
from agent_harness.evaluation.engine import evaluate, read_report
from agent_harness.evaluation.memory_cases import MEMORY_STRATEGIES
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.storage.runs import RunStore
from tests.web.test_planning_api import call, reply


class MemoryEvalModel:
    """固定模型根据收到的证据选择产物；只验证工程接线和评分器，不测量模型能力。"""

    async def complete(self, messages, tools):
        """以 messages 中的实际学习依据决定整理候选或文件工具，返回带可核对用量的响应。"""
        if messages[0]["content"].startswith("你是项目记忆整理器"):
            evidence = json.loads(messages[1]["content"])["historical_evidence"]
            changes = []
            for row in evidence:
                if row["kind"] == "user" and "review-lane" in row["text"]:
                    changes.append({"action": "add", "topic": "演练告警", "reason": "用户已经确认路由",
                                    "evidence_ids": [row["id"]], "content": {"type": "rule", "title": "demo_route 路由",
                                    "details": {"topic": "演练路由", "behavior": "demo_route 返回 review-lane"}}})
                if row["kind"] == "user" and "45 秒" in row["text"]:
                    changes.append({"action": "add", "topic": "演练告警", "reason": "用户已经确认时长",
                                    "evidence_ids": [row["id"]], "content": {"type": "rule", "title": "demo_ttl 时长",
                                    "details": {"topic": "演练时长", "behavior": "demo_ttl 返回 45 秒"}}})
            result = reply(text=json.dumps({"changes": changes}, ensure_ascii=False))
        else:
            prompt = next(message["content"] for message in reversed(messages) if message["role"] == "user")
            contents = "\n".join(message.get("content") or "" for message in messages)
            has_search = any(message["role"] == "tool" for message in messages)
            if has_search and any(
                item.get("function", {}).get("name") == "write_file"
                for message in messages for item in message.get("tool_calls", [])
            ):
                result = reply()
            elif "draft_amount" in prompt and "draft_total" in contents:
                result = reply(call("write_file", path="solution.py", overwrite=True,
                    content='def draft_amount(cents):\n    """以分为输入，返回演练金额对象。"""\n    return {"draft_total": f"{cents/100:.1f}"}\n'))
            elif "demo_route" in prompt and "review-lane" in contents:
                result = reply(call("write_file", path="solution.py", overwrite=True,
                    content='def demo_route():\n    """返回已确认的演练路由。"""\n    return "review-lane"\n'))
            elif "demo_ttl" in prompt and "45 秒" in contents:
                result = reply(call("write_file", path="solution.py", overwrite=True,
                    content='def demo_ttl():\n    """返回已确认的演练持续秒数。"""\n    return 45\n'))
            elif not has_search and "历史证据可用" in contents:
                term = "demo_route" if "demo_route" in prompt else "demo_ttl" if "demo_ttl" in prompt else "debug_folder"
                result = reply(call("memory_search", query=term, collection="history", limit=4))
            elif "debug_folder" in prompt:
                assert "isolated-demo" not in contents
                result = reply(call("write_file", path="result.json", content='{"status":"needs_confirmation"}'))
            else:
                result = reply(text="项目约定未找到。")
        return replace(result, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def test_memory_suite_runs_four_groups_and_records_consolidation(tmp_path: Path):
    """用真实 Runner 和子进程评分验证 4×4 开发对照及独立整理账本。"""
    suite = Path(__file__).resolve().parents[2] / "evals" / "memory-v1" / "suite.json"
    experiment = Experiment(kind="memory", groups=preset_groups(list(MEMORY_STRATEGIES)))
    config = load_config(suite.with_name("deepseek.toml"))
    root = tmp_path / "runs"
    report = asyncio.run(evaluate(suite, config, experiment, root, tmp_path / "work",
                                  "offline-memory-evaluation", client_factory=MemoryEvalModel))
    assert report["state"] == "completed", report["stop_reason"]
    assert report["mode"] == "scripted" and len(report["trials"]) == 16
    assert len(report["consolidations"]) == 3
    groups = {group["strategy"]: group for group in report["summary"]["groups"]}
    assert groups["memory_current"]["successes"] == 2
    assert groups["memory_history"]["successes"] == 4
    assert groups["memory_full"]["successes"] == 4
    assert groups["memory_off"]["successes"] == 1
    assert groups["memory_full"]["memory"]["consolidation_tokens"] == 45
    assert groups["memory_history"]["memory"]["all_evidence_exposed"] == 3
    assert all(not row["memory"]["forbidden_hits"] for row in report["trials"])
    assert read_report(root, report["id"])["summary"] == report["summary"]
    # 核对真实试次清单，避免向模型声明当前组不能执行的检索路径。
    expected = {"memory_current": {"notes"}, "memory_history": {"notes", "history"},
                "memory_full": {"notes", "history", "topics"}, "memory_off": set()}
    for row in report["trials"]:
        history = RunStore.read(root, row["run_id"])
        started = next(event for event in history.events if event.type == "run_started")
        tools = {item["function"]["name"]: item["function"]["parameters"] for item in started.payload["tools"]}
        collection = tools.get("memory_search", {}).get("properties", {}).get("collection")
        exposed = set(collection["enum"]) if collection else {"notes"} if "memory_search" in tools else set()
        assert exposed == expected[row["strategy"]]
        assert ("evidence_id" in tools.get("memory_get", {}).get("properties", {})) == ("history" in exposed)
