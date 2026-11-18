"""在三份新仓库快照上做两轮开关记忆对照，并按真实工具回执度量资料查找。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

from agent_harness.evaluation.budget import USD_PICOS, validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.memory.types import MemoryContent
from agent_harness.model_selection import valid_model_key
from agent_harness.storage.runs import RunStore


HERE = Path(__file__).resolve().parent  # 真实公开源码快照和新题入口。
OUTPUT = HERE.parent.parent / "output" / HERE.name  # 原始运行证据目录。
ROUNDS = (1, 2)  # 每个题目每轮独立准备两组。
MAX_CALLS = 48 * 12  # 冻结的模型请求次数边界。
PROJECTS = {"config": "config", "importer": "importer", "reporting": "reporting"}  # 允许的三个公开源目录。

sys.path.insert(0, str(HERE.parent / "memory-followup-20261117"))
from run_followup import RequestLedger, memory_run, schedule_for, scored
sys.path.pop(0)

BASE_CONFIG = memory_run.config  # 沿用原有模型、提示和安全工具。
BASE_TRANSPORT = memory_run.EvidenceTransport  # 沿用经过验证的官方 HTTPS 传输和旧值观测。


def repository_files(root: Path) -> tuple[dict[str, str], dict]:
    """保留仓库原始 UTF-8 文本和层级，记录二进制素材与全部文本版本指纹。"""
    texts, omitted = {}, []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("公开源码快照中存在符号链接或连接点")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if set(relative.parts) & {".git", "__pycache__", ".venv", ".pytest_cache"}:
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError
        except UnicodeError:
            omitted.append(relative.as_posix())
            continue
        texts[relative.as_posix()] = text
    docs = [name for name in texts if Path(name).suffix.lower() in {".md", ".rst", ".txt"}]
    if not docs or not any(name.endswith(".py") for name in texts):
        raise ValueError(f"源码快照缺少原生资料或 Python 源码：{root}")
    inventory = {"root": str(root), "utf8_files": len(texts), "text_documents": len(docs),
                 "omitted_binary": omitted, "sha256": memory_run.fingerprint(texts)}
    return texts, inventory


def load_suite() -> tuple[list[dict], dict, str]:
    """验证题单、初始真实源码、两组可见证据与四类干扰，计算完整协议指纹。"""
    from config_cases import CASES as CONFIG
    from import_cases import CASES as IMPORT
    from report_cases import CASES as REPORT

    groups = {"config": CONFIG, "importer": IMPORT, "reporting": REPORT}
    if any(len(group) != 4 for group in groups.values()):
        raise ValueError("每个仓库必须有四道新题")
    cases, inventories = [], {}
    base = {}
    for project, group in groups.items():
        roots = {Path(case["source_root"]).resolve(strict=True) for case in group}
        allowed = (HERE / "sources" / PROJECTS[project]).resolve(strict=True)
        if len(roots) != 1:
            raise ValueError("同一项目需要同一份固定源码")
        root = roots.pop()
        if not root.is_relative_to(allowed):
            raise ValueError(f"禁止从未登记目录加载评测源码：{project}")
        base[project], inventories[project] = repository_files(root)
    for index in range(4):
        for project, group in groups.items():
            source = group[index]
            case = dict(source, source_root=str(Path(source["source_root"]).resolve(strict=True)),
                        files=base[project] | source["files"])
            if case["project"] != project or case["scenario"] not in {"locate", "knowledge", "safety"}:
                raise ValueError("题目项目或类别不符")
            case["kind"] = "stale" if case["scenario"] == "safety" else "reuse"
            current = case["files"] | case.get("updates", {})
            if case["target"] not in current:
                raise ValueError(f"缺待修改文件：{case['id']}")
            anchors = case["evidence_anchors"]
            if not anchors or not set(anchors) <= set(case["evidence_paths"]):
                raise ValueError(f"缺少可复查的证据观察条件：{case['id']}")
            for path, anchor in anchors.items():
                memory_run.safe_path(HERE, path)
                if not anchor or anchor not in current.get(path, ""):
                    raise ValueError(f"答题侧原文无法找到指定证据：{case['id']} {path}")
            foreign = {note["episode"] for note in case["notes"] if note.get("foreign")}
            for i, episode in enumerate(case["episodes"]):
                materials = case.get("foreign_files", {}) if i in foreign else case["files"]
                if (not episode["read"] or case["target"] in episode["read"] or
                        not set(episode["read"]) <= materials.keys()):
                    raise ValueError(f"学习来源与实际证据不一致：{case['id']}")
            for note in case["notes"]:
                MemoryContent.model_validate(note["content"])
            if case["kind"] == "stale" and (not case.get("stale_values") or not case.get("misuse")):
                raise ValueError(f"失效题缺少行为探针：{case['id']}")
            cases.append(case)
    if len({case["id"] for case in cases}) != 12 or Counter(case["scenario"] for case in cases) != {"locate": 4, "knowledge": 4, "safety": 4}:
        raise ValueError("题号或题型配额不符")
    code = [Path(__file__), Path(memory_run.__file__)]
    protocol = memory_run.fingerprint([cases, inventories, [sha256(path.read_bytes()).hexdigest() for path in code]])
    return cases, inventories, protocol


def config(arm: str, *, learning: bool = False):
    """为真实仓库任务提高已批准的时限，记忆之外的设置在两组完全一致。"""
    initial = BASE_CONFIG(arm, learning=learning)
    limits = initial.limits.model_copy(update={"max_model_turns": 12, "max_tool_calls": 30, "run_timeout_seconds": 180})
    return initial.model_copy(update={"limits": limits})


def visible_text(data: object) -> str:
    """展开实际可见的结构化记忆正文，保留转义前的文本以匹配原文锚点。"""
    if isinstance(data, dict):
        return "\n".join(visible_text(value) for value in data.values())
    if isinstance(data, list):
        return "\n".join(visible_text(value) for value in data)
    if not isinstance(data, str):
        return ""
    if data.startswith(("{", "[")):
        try:
            decoded = json.loads(data)
        except ValueError:
            return data
        if isinstance(decoded, (dict, list)):
            return data + "\n" + visible_text(decoded)
    return data


class EvidenceAudit:
    """只从实际交给模型的内容中确认资料被定位或取得，正文不另行写盘。"""

    def __init__(self, case: dict) -> None:
        """固定预注册的来源路径与关键正文，以及仅用于定位的路径指示。"""
        self.case = case  # 原冻结题与评分共享的原文标志。
        self.locations = set(case["evidence_paths"])  # 正确资料的相对路径。
        self.anchors = dict(case["evidence_anchors"])  # 逐文件唯一的关键原文片段。
        self.requests: list[dict] = []  # 只保存路由、步数、计数和是否实际收到。
        self.original_lines: set[tuple[str, int, str]] = set()  # 跨请求去重搜索返回的原文行。

    def _consider(self, request: dict) -> None:
        """从序列化的模型消息识别第一条路径提示、第一条实质证据与查找步数。"""
        paths, contents = {}, {}
        steps = 0
        names = {call["id"]: call["function"]["name"] for message in request["messages"]
                 for call in message.get("tool_calls") or []}
        for message in request["messages"]:
            text = message.get("content")
            if not isinstance(text, str):
                continue
            if message["role"] == "user" and "<project_memory>" in text:
                projection = text.split("<project_memory>", 1)[1].split("</project_memory>", 1)[0]
                projection = "\n".join(visible_text(line) for line in projection.splitlines())
                for path in self.locations:
                    if path in projection:
                        paths.setdefault(path, 0)
                    marker = self.anchors.get(path)
                    if marker and marker in projection:
                        paths.setdefault(path, 0)
                        contents.setdefault(path, 0)
                continue
            if message["role"] != "tool":
                continue
            steps += 1
            name = names.get(message.get("tool_call_id"))
            result = json.loads(text)
            if not result.get("ok"):
                continue
            data = result.get("data") or {}
            if name == "read_file":
                path = data.get("path")
                if path in self.locations:
                    paths.setdefault(path, steps)
                if path in self.anchors and self.anchors[path] in data.get("content", ""):
                    contents.setdefault(path, steps)
            elif name == "list_files":
                for entry in data.get("entries", []):
                    path = entry["path"]
                    if path in self.locations:
                        paths.setdefault(path, steps)
            elif name == "search_files":
                for entry in data.get("matches", []):
                    path, line = entry["path"], entry.get("line", "")
                    if path not in self.locations:
                        continue
                    paths.setdefault(path, steps)
                    self.original_lines.add((path, entry.get("line_number", 0), line))
                    if path in self.anchors and self.anchors[path] in line:
                        contents.setdefault(path, steps)
            elif name in memory_run.MEMORY_TOOLS:
                text = visible_text(data)
                for path in self.locations:
                    if path in text:
                        paths.setdefault(path, steps)
                    marker = self.anchors.get(path)
                    if marker and marker in text:
                        paths.setdefault(path, steps)
                        contents.setdefault(path, steps)
        self.requests.append({"locate_steps": min(paths.values(), default=None),
                              "evidence_steps": min(contents.values(), default=None),
                              "paths": sorted(paths), "evidence_paths": sorted(contents)})


async def run_slot(case: dict, arm: str, batch: Path, key: str, guard: RequestLedger | None) -> dict:
    """调用原 Runner 并留存列目录、搜索、原文获取和独立评分的实际计数。"""
    audit = EvidenceAudit(case)

    class TraceTransport(BASE_TRANSPORT):
        """对真实 HTTPS 请求观测已送达的路径和原文，保留正常传输功能。"""

        async def handle_async_request(self, request):
            """观察实际请求正文及回执状态，失败时沿用计费门禁。"""
            audit._consider(json.loads(request.content))
            response = await super().handle_async_request(request)
            audit.requests[-1]["status"] = response.status_code
            return response

    with patch.object(memory_run, "config", config), patch.object(memory_run, "EvidenceTransport", TraceTransport):
        row = await memory_run.trial(case, arm, batch, key=key, guard=guard, real=guard is not None)
    history = RunStore.read(batch / "runs", row["run_id"])
    successes = [request for request in audit.requests if request.get("status", 500) < 400]
    listing = sum(e.type == "tool_result" and e.payload.get("name") == "list_files" for e in history.events)
    row.update(scenario=case["scenario"], list_calls=listing, find_calls=listing + row["search_calls"],
               locate_steps=min((x["locate_steps"] for x in successes if x["locate_steps"] is not None), default=None),
               evidence_steps=min((x["evidence_steps"] for x in successes if x["evidence_steps"] is not None), default=None),
               search_source_lines=len(audit.original_lines),
               search_source_bytes=sum(len(text.encode("utf-8")) for _, _, text in audit.original_lines))
    slot = batch / "slots" / case["id"] / arm
    atomic_json(slot / "lookup.json", {"requests": audit.requests, "search_source_lines": row["search_source_lines"],
                                       "search_source_bytes": row["search_source_bytes"]})
    return row


def arm_metrics(rows: list[dict]) -> dict:
    """以可比较的同题子集为分母汇总质量、查找、回读及 token。"""
    counts = ("list_calls", "search_calls", "find_calls", "repeated_reads", "repeated_bytes",
              "tool_calls", "memory_calls", "search_source_bytes", "search_source_lines", "http_attempts")
    result = {name: sum(row[name] for row in rows) for name in counts}
    n = len(rows)
    result.update(n=n, mean_tools=result["tool_calls"] / n if n else None,
                  patch=sum(row["patch_passed"] is True for row in rows), full=sum(row["delivery"] is True for row in rows),
                  old_exposed=sum(row["old_exposed"] for row in rows),
                  misuse=sum(row["misuse"] is True for row in rows),
                  assessable=sum(row["misuse"] is not None for row in rows),
                  safe_delivery=sum(row["delivery"] is True and row["misuse"] is False for row in rows),
                  usage={key: sum(row["usage"][key] for row in rows)
                         for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
    for metric in ("locate_steps", "evidence_steps"):
        values = [row[metric] for row in rows if row[metric] is not None]
        result[metric] = {"observed": len(values), "mean": sum(values) / len(values) if values else None}
    return result


def summary_for(rows: list[dict]) -> dict:
    """只在两臂均有可计量结果的同题上比较，另给同题双方完整交付的复用子集。"""
    paired = defaultdict(dict)
    for row in rows:
        key = row["case_id"]
        if row["arm"] in paired[key]:
            raise ValueError("同轮内有重复试次")
        paired[key][row["arm"]] = row
    valid = [pair for pair in paired.values() if set(pair) == {"A", "B"} and all(map(scored, pair.values()))]
    reuse = [pair for pair in valid if pair["A"]["kind"] == "reuse"]
    ranges = {"all": valid, "reuse": reuse,
              "completed_reuse": [pair for pair in reuse if all(row["delivery"] for row in pair.values())],
              **{label: [pair for pair in valid if pair["A"]["scenario"] == label]
                 for label in ("locate", "knowledge", "safety")}}
    return {"valid_pairs": len(valid), "trials": len(rows),
            **{key: {arm: arm_metrics([pair[arm] for pair in pairs]) for arm in ("A", "B")}
               for key, pairs in ranges.items()}}


def mean_text(value: float | None) -> str:
    """显示两位小数；无实际记录时保留破折号。"""
    return "—" if value is None else f"{value:.2f}"


def render(report: dict) -> str:
    """将两轮原始结果可复查地呈现在 Markdown 中。"""
    rows = report["trials"]
    budget = report["budget"]
    out = ["# AgentHarness 多目录仓库记忆对照", "",
           "A 关闭项目记忆，B 开启现有项目记忆。仓库快照、题面、评分、模型和运行额度一致。", "",
           f"状态 `{report['state']}`。记录 {len(rows)}/48 次，真实请求 {budget['calls_admitted']}，"
           f"已知用量 {budget['known_tokens']:,} token，估算费用 ${budget['known_usd_picos']/USD_PICOS:.6f}。",
           "", "## 两轮配对摘要", "",
           "| 轮 | 题型 | 组 | 配对题 | 文件搜索 | 列目录 | 重读 | 重读字节 | 搜索返回原文 B | 平均工具 | 首获正文平均步数 / 样本 | 正确 | 完整 |",
           "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    if report["stop_reason"]:
        out.insert(5, f"停批原因 `{report['stop_reason']}`。")
    for repeat, block in report["rounds"].items():
        for key, title in (("all", "全部"), ("reuse", "复用"), ("completed_reuse", "双方完整复用"),
                           ("locate", "定位"), ("knowledge", "知识"), ("safety", "失效")):
            for arm, m in block[key].items():
                n = m["n"]
                if n:
                    e = m["evidence_steps"]
                    out.append(f"| {repeat} | {title} | {arm} | {n} | {m['search_calls']} | {m['list_calls']} | "
                               f"{m['repeated_reads']} | {m['repeated_bytes']} | {m['search_source_bytes']} | {mean_text(m['mean_tools'])} | "
                               f"{mean_text(e['mean'])} / {e['observed']} | {m['patch']}/{n} | {m['full']}/{n} |")
    out += ["", "## 失效与干扰", "",
            "| 轮 | 组 | 旧值进入实际输入 | 误用 / 可判定 | 安全完整交付 |",
            "|---|---|---:|---:|---:|"]
    for repeat, block in report["rounds"].items():
        for arm, row in block["safety"].items():
            if row["n"]:
                out.append(f"| {repeat} | {arm} | {row['old_exposed']} | {row['misuse']}/{row['assessable']} | {row['safe_delivery']}/{row['n']} |")
    out += ["", "## 逐题结果", "",
            "| 轮 | 题目 | 组 | 搜索 | 列目录 | 重读 | 全部工具 | 首获正文步数 | 正确 / 完整 | 终态 | Run |",
            "|---|---|---|---:|---:|---:|---:|---:|---|---|---|"]
    marks = {True: "是", False: "否", None: "—"}
    for row in rows:
        steps = row["evidence_steps"] if row["evidence_steps"] is not None else "—"
        out.append(f"| {row['round']} | {row['title']} | {row['arm']} | {row['search_calls']} | {row['list_calls']} | "
                   f"{row['repeated_reads']} | {row['tool_calls']} | {steps} | "
                   f"{marks[row['patch_passed']]} / {marks[row['delivery']]} | {row['stop_reason']} | {row['run_id']} |")
    return "\n".join(out) + "\n"


async def main() -> None:
    """按批准范围先测试基线和参考，再跑两轮真实对照。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="运行四十八个正式槽位。")
    parser.add_argument("--key-from-stdin", action="store_true", help="从匿名管道接收官方凭据。")
    flags = parser.parse_args()
    key = sys.stdin.readline(2048).strip() if flags.key_from_stdin else "offline"
    if flags.real and (not flags.key_from_stdin or not valid_model_key(key)):
        raise SystemExit("缺少官方凭据")
    cases, inventories, protocol = load_suite()
    if flags.real:
        receipt = json.loads((OUTPUT / "preflight-latest.json").read_bytes())
        if receipt["state"] != "passed" or receipt["protocol"] != protocol:
            raise SystemExit("同一源码和题单尚未通过预检")
        validate_billing_config(config("A"))
        validate_billing_config(config("B"))
    branch = "paid" if flags.real else "preflight"
    batch = OUTPUT / branch / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    sessions = {repeat: schedule_for(cases, repeat) for repeat in ROUNDS}
    rows: list[dict] = []
    report = {"protocol": protocol, "repositories": inventories, "state": "preparing", "stop_reason": None,
              "active": None, "trials": rows, "scorer_image": memory_run.SCORER_IMAGE,
              "schedule": [{"round": r, "case_id": case["id"], "arm": arm} for r, slots in sessions.items()
                           for case, arm in slots]}

    def save() -> None:
        """原子落盘报告、准备或活动试次和逐请求账本。"""
        report["rounds"] = {str(r): summary_for([row for row in rows if row["round"] == r]) for r in ROUNDS}
        atomic_json(batch / "batch.json", report)

    print(f"Batch: {batch}", flush=True)
    save()
    atomic_json(batch / "frozen-suite.json", {"protocol": protocol, "cases": cases, "repositories": inventories})
    seen = {}
    for r in ROUNDS if flags.real else (1,):
        local = batch / f"round-{r}"
        with patch.object(memory_run, "config", config):
            seeds = await memory_run.prepare(cases, sessions[r], local)
        for seed in seeds:
            value = seen.setdefault(seed["case_id"], seed["fixture_sha256"])
            if value != seed["fixture_sha256"]:
                raise ValueError(f"两轮学习历史或答题文件不一致：{seed['case_id']}")
        atomic_json(local / "seeds.json", {"preparations": seeds})
        print(f"Prepared round {r}: {len(seeds)} slots.", flush=True)
    if not flags.real:
        scores = [await asyncio.to_thread(memory_run.scoring_preflight, case, batch) for case in cases]
        controls = [await run_slot(case, arm, batch / "round-1", key, None)
                    for case, arm in sessions[1] if case["id"] in {row["id"] for row in cases[:3]}]
        if any(row["http_attempts"] or row["patch_passed"] is not False for row in controls):
            raise ValueError("独立 Runner 的基线预检失败")
        report.update(state="passed", scores=scores, controls=controls)
        save()
        atomic_json(OUTPUT / "preflight-latest.json", {"state": "passed", "protocol": protocol, "batch": str(batch), "external_http": 0})
        print("Preflight passed 12/12; external requests 0.", flush=True)
        return
    report.update(state="running", preflight=receipt["batch"])
    guard = RequestLedger(report, save)
    guard.state.update(max_calls=MAX_CALLS, max_known_tokens=MAX_CALLS * 1_000_000)
    save()
    for r, slots in sessions.items():
        for case, arm in slots:
            if reason := guard.blocking_reason():
                report.update(state="stopped", stop_reason=reason)
                break
            report["active"] = {"round": r, "case_id": case["id"], "arm": arm}
            save()
            try:
                row = await run_slot(case, arm, batch / f"round-{r}", key, guard)
            except Exception as error:
                report.update(state="stopped", stop_reason=type(error).__name__)
                save()
                raise
            row["round"] = r
            rows.append(row)
            report["active"] = None
            print(f"Round {r} {case['id']} {arm} search={row['search_calls']} list={row['list_calls']} "
                  f"read={row['repeated_reads']} tools={row['tool_calls']} score={row['patch_passed']} "
                  f"full={row['delivery']} stop={row['stop_reason']}", flush=True)
            if guard.stop_reason or row["patch_passed"] is None or row["cleanup_status"] not in {"complete", "not_needed"}:
                report.update(state="stopped", stop_reason=guard.stop_reason or "evaluation_infrastructure")
                break
            save()
        if report["state"] == "stopped":
            break
    if report["state"] == "running":
        report["state"] = "complete"
    save()
    (batch / "report.md").write_text(render(report), encoding="utf-8")
    print(f"{report['state']} {len(rows)}/48", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
