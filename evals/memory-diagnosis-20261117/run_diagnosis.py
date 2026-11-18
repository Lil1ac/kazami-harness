"""用四种受控配置诊断复用题中的证据重读，各组均开启项目记忆。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

from agent_harness.evaluation.budget import validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.memory.service import MemoryController
from agent_harness.model_selection import valid_model_key

from presentation import present_verified


HERE = Path(__file__).resolve().parent  # 诊断入口和仅评测使用的呈现候选。
OUTPUT = HERE.parent.parent / "output" / HERE.name  # 诊断证据与已完成的追加评测独立。
SOURCE = OUTPUT.parent / "memory-followup-20261117/paid/20261117-144735-379385/frozen-cases.json"  # 已完成批次的原题。
CASE_IDS = (
    "followup-rule-half-open-window", "followup-rule-shared-place",
    "reuse-fact-transfer-scan-codes", "reuse-fact-digest-template-grid",
)  # 预先声明的两道规则和两道事实题。
VARIANTS = {
    1: {"neutral_prompt": False, "verified_notes": False},
    2: {"neutral_prompt": True, "verified_notes": False},
    3: {"neutral_prompt": False, "verified_notes": True},
    4: {"neutral_prompt": True, "verified_notes": True},
}  # 两个独立开关，记忆功能始终开启。
ORDERS = ((1, 2, 4, 3), (2, 3, 1, 4), (3, 4, 2, 1), (4, 1, 3, 2))  # 每种配置均占一次各顺位。
REMINDER = "，按需核对当前项目规则"  # 中性提示只移除这一处。
MAX_CALLS = 16 * 8  # 原有每试次八请求的四题四组上限。

sys.path.insert(0, str(HERE.parent / "memory-followup-20261117"))
from run_followup import RequestLedger, memory_run
sys.path.pop(0)

BASE_CONFIG = memory_run.config  # 保留既有真实 Runner 的模型和预算配置。
BASE_INPUT_TEXT = MemoryController.input_text  # 已审计的产品投影，候选变体在其基础上叠加。
BASE_PROBE = memory_run.SeedReader  # 只在确定性预检中生成非联网完成回执。
BASE_TRANSPORT = memory_run.EvidenceTransport  # 真实请求沿用原校验端点与 TLS 信任库。


def frozen_cases() -> tuple[list[dict], str]:
    """核对上一批完整冻结内容，再按指定顺序取出四道原题。"""
    document = json.loads(SOURCE.read_bytes())
    if memory_run.fingerprint(document["cases"]) != document["case_fingerprint"]:
        raise ValueError("原冻结题单发生变化")
    lookup = {case["id"]: case for case in document["cases"]}
    selected = [lookup[case_id] for case_id in CASE_IDS]
    if [case["scenario"] for case in selected] != ["rule", "rule", "fact", "fact"]:
        raise ValueError("诊断题型与批准范围不一致")
    return selected, document["case_fingerprint"]


def schedule_of(cases: list[dict]) -> list[tuple[dict, int]]:
    """按均衡顺序生成每题四个独立槽位。"""
    return [(case, variant) for case, variants in zip(cases, ORDERS, strict=True) for variant in variants]


def note_projection(controller: MemoryController) -> str:
    """取产品生成的输入并增加候选核验说明，仅在当前诊断槽位使用。"""
    return present_verified(controller, BASE_INPUT_TEXT(controller))


def configuration(arm: str, *, learning: bool = False):
    """生成中性提示配置，其余设置与现有评测完全一致。"""
    original = BASE_CONFIG(arm, learning=learning)
    if original.system_prompt.count(REMINDER) != 1:
        raise ValueError("公共提示中的核对措辞发生了变化")
    return original.model_copy(update={"system_prompt": original.system_prompt.replace(REMINDER, "", 1)})


class InputAudit:
    """验证真正交给模型客户端的请求是否符合冻结的两个因素。"""

    def __init__(self, case: dict, variant: int) -> None:
        """固定题目、配置序号和每次请求的证据标志。"""
        self.case = case  # 用原冻结记忆标题在自动注入中核对相应条目。
        self.variant = variant  # 四个获批配置中的一个。
        self.observations: list[dict] = []  # 仅保存必要的核验信息，不额外保留整份请求。

    def read(self, messages: list[dict], tools: list[dict]) -> None:
        """核验公共指令、记忆条目、待解决任务与工具，首个请求必须通过。"""
        factor = VARIANTS[self.variant]
        system = next(message.get("content") for message in messages if message["role"] == "system")
        prompt_matches = (REMINDER in system) != factor["neutral_prompt"]
        names = {item["function"]["name"] for item in tools}
        expected_tools = set(memory_run.TOOLS + memory_run.MEMORY_TOOLS)
        last_task = [message["content"] for message in messages
                     if message["role"] == "user" and message.get("content") == self.case["prompt"]]
        memory_texts = [message["content"].split("<project_memory>", 1)[1].split("</project_memory>", 1)[0]
                        for message in messages if message["role"] == "user"
                        and isinstance(message.get("content"), str) and "<project_memory>" in message["content"]]
        projected = []
        for text in memory_texts:
            for line in text.strip().splitlines():
                if line.startswith("[{"):
                    projected.extend(json.loads(line))
        title = self.case["notes"][0]["content"]["title"]
        candidates = [note for note in projected if note.get("title") == title]
        marker = [note.get("verification") for note in candidates]
        verified = bool(marker and all(isinstance(value, dict) and value for value in marker))
        if not self.observations:
            if (not prompt_matches or names != expected_tools or len(last_task) != 1
                    or not candidates or verified != factor["verified_notes"]):
                raise ValueError(f"首条模型输入与冻结配置不同：{self.case['id']} 配置{self.variant}")
            content = self.case["notes"][0]["content"]
            value = content["details"]["behavior" if content["type"] == "rule" else "value"]
            if value not in candidates[0]["usage_text"]:
                raise ValueError(f"首条模型输入没有完整记忆：{self.case['id']}")
        self.observations.append({"prompt_matches": prompt_matches, "task_present": bool(last_task),
                                  "note_present": bool(candidates), "verification_present": verified,
                                  "tools_match": names == expected_tools, "note_verification": marker})


async def run_slot(case: dict, variant: int, batch: Path, *, key: str, ledger: RequestLedger | None) -> dict:
    """运行一个冻结槽位并以本地模型替身或真实 HTTP 验证输入因素。"""
    audit = InputAudit(case, variant)

    class InspectTransport(BASE_TRANSPORT):
        """沿用生产发单，仅插入实际序列化请求的诊断。"""

        async def handle_async_request(self, request):
            """在请求发送前对消息和工具执行精确核验。"""
            data = json.loads(request.content)
            audit.read(data["messages"], data["tools"])
            return await super().handle_async_request(request)

    class InspectOffline(BASE_PROBE):
        """不走网络，通过固定响应测试同一个 Runner 组装的模型输入。"""

        async def complete(self, messages: list[dict], tools: list[dict]):
            """核验输入后交还零模型的确定性答复。"""
            audit.read(messages, tools)
            return await super().complete(messages, tools)

    scope = batch / f"variant-{variant}"
    with ExitStack() as fixes:
        if VARIANTS[variant]["neutral_prompt"]:
            fixes.enter_context(patch.object(memory_run, "config", configuration))
        if VARIANTS[variant]["verified_notes"]:
            fixes.enter_context(patch.object(MemoryController, "input_text", note_projection))
        if ledger is not None:
            fixes.enter_context(patch.object(memory_run, "EvidenceTransport", InspectTransport))
        else:
            fixes.enter_context(patch.object(memory_run, "SeedReader", InspectOffline))
        row = await memory_run.trial(case, "B", scope, key=key, guard=ledger, real=ledger is not None)

    slot = scope / "slots" / case["id"] / "B"
    atomic_json(slot / "diagnosis-input.json", {"variant": variant, "requests": audit.observations})
    verified = bool(audit.observations) and all(value["prompt_matches"] and value["tools_match"] for value in audit.observations)
    if ledger and len(audit.observations) != row["http_attempts"]:
        verified = False
    row.update(variant=variant, scenario=case["scenario"], injected_input_verified=verified)
    return row


async def main() -> None:
    """先核定四种输入和独立评分，通过后按同一冻结题单完成十六次诊断。"""
    from report import render, summarize

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="运行已批准的十六次真实诊断。")
    parser.add_argument("--key-from-stdin", action="store_true", help="通过匿名管道接收官方凭据。")
    flags = parser.parse_args()
    key = sys.stdin.readline(2048).strip() if flags.key_from_stdin else "offline"
    if flags.real and (not flags.key_from_stdin or not valid_model_key(key)):
        raise SystemExit("缺少官方 API 凭据")
    cases, source_fingerprint = frozen_cases()
    logic = [Path(__file__), HERE / "presentation.py", Path(memory_run.__file__)]
    protocol = memory_run.fingerprint([cases, VARIANTS, ORDERS, [sha256(path.read_bytes()).hexdigest() for path in logic]])
    if flags.real:
        previous = json.loads((OUTPUT / "preflight-latest.json").read_bytes())
        if previous["state"] != "passed" or previous["protocol"] != protocol:
            raise SystemExit("该冻结配置尚未通过预检")
        validate_billing_config(BASE_CONFIG("B"))
    branch = "paid" if flags.real else "preflight"
    batch = OUTPUT / branch / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    schedule = schedule_of(cases)
    rows: list[dict] = []
    report = {"state": "preparing", "stop_reason": None, "active": None, "trials": rows, "protocol": protocol,
              "source_fingerprint": source_fingerprint, "variants": VARIANTS,
              "schedule": [{key: case[key] for key in ("title", "scenario")} | {"case_id": case["id"], "variant": variant}
                           for case, variant in schedule]}

    def save() -> None:
        """原子持久化报告，保留每次请求账本和完整度。"""
        report["summary"] = summarize(rows)
        atomic_json(batch / "batch.json", report)

    atomic_json(batch / "frozen-cases.json", {"cases": cases, "protocol": protocol, "source_fingerprint": source_fingerprint})
    save()
    print(f"Batch: {batch}", flush=True)
    old_seeds = json.loads((SOURCE.parent / "round-1/seeds.json").read_bytes())["preparations"]
    matched = {seed["case_id"]: seed["fixture_sha256"] for seed in old_seeds
               if seed["case_id"] in CASE_IDS and seed["arm"] == "B"}
    if set(matched) != set(CASE_IDS):
        raise ValueError("原批次缺少四道冻结题的学习证据")
    for variant in VARIANTS:
        seeds = await memory_run.prepare(cases, [(case, "B") for case in cases], batch / f"variant-{variant}")
        for seed in seeds:
            previous_seed = matched.setdefault(seed["case_id"], seed["fixture_sha256"])
            if previous_seed != seed["fixture_sha256"]:
                raise ValueError(f"某配置的学习材料发生变化：{seed['case_id']}")
        atomic_json(batch / f"variant-{variant}" / "seeds.json", {"preparations": seeds})
    if not flags.real:
        scores = [await asyncio.to_thread(memory_run.scoring_preflight, case, batch) for case in cases]
        controls = [await run_slot(case, variant, batch, key=key, ledger=None) for case, variant in schedule]
        if any(not row["injected_input_verified"] or row["patch_passed"] is not False or row["http_attempts"] for row in controls):
            raise ValueError("诊断输入或零模型基线验证失败")
        report.update(state="passed", controls=controls, scores=scores)
        save()
        atomic_json(OUTPUT / "preflight-latest.json", {"state": "passed", "protocol": protocol, "batch": str(batch),
                                                      "verified_variants": len(controls), "external_http": 0})
        print("Preflight passed 4 cases and all 16 request shapes; external requests 0.", flush=True)
        return

    report.update(state="running", preflight=previous["batch"])
    ledger = RequestLedger(report, save)
    ledger.state.update(max_calls=MAX_CALLS, max_known_tokens=MAX_CALLS * 1_000_000)
    save()
    for case, variant in schedule:
        if reason := ledger.blocking_reason():
            report.update(state="stopped", stop_reason=reason)
            break
        report["active"] = {"case_id": case["id"], "variant": variant}
        save()
        try:
            row = await run_slot(case, variant, batch, key=key, ledger=ledger)
        except Exception as error:
            report.update(state="stopped", stop_reason=type(error).__name__)
            save()
            raise
        rows.append(row)
        report["active"] = None
        print(f"{case['id']} v{variant} read={row['repeated_reads']} tools={row['tool_calls']} "
              f"score={row['patch_passed']} delivery={row['delivery']} stop={row['stop_reason']}", flush=True)
        if ledger.stop_reason or row["patch_passed"] is None or row["cleanup_status"] not in {"complete", "not_needed"} or not row["injected_input_verified"]:
            report.update(state="stopped", stop_reason=ledger.stop_reason or "evaluation_infrastructure")
            break
        save()
    if report["state"] == "running":
        report["state"] = "complete"
    save()
    (batch / "report.md").write_text(render(report), encoding="utf-8")
    print(f"{report['state']} {len(rows)}/16", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
