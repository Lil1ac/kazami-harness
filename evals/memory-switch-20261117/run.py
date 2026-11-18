"""冻结新题，真实记录学习读取，再比较同题开关长期记忆的结果。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import ssl
import subprocess
import sys
from uuid import uuid4

import httpx

from agent_harness.config import prepare_task
from agent_harness.evaluation.budget import BatchBudget, MODEL_NAME, USD_PICOS, validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.models import Experiment
from agent_harness.memory.evidence import evidence_metadata, project_events
from agent_harness.memory.service import MemoryChange, MemoryController, MemoryError, MemoryGet, MemorySearch, MemoryService, MemoryStore
from agent_harness.memory.types import MemoryContent
from agent_harness.model_selection import CustomProvider, valid_model_key
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runtime.runner import run_task
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.storage.runs import RunStore
from agent_harness.tools.files import ReadFileArguments
from agent_harness.tools.registry import ToolExecutor


HERE = Path(__file__).resolve().parent  # 新题和调度器目录。
OUTPUT = HERE.parent.parent / "output" / HERE.name  # 批次记录集中保存，保留各阶段原件。
TOOLS = ("list_files", "search_files", "read_file", "edit_file", "write_file")  # 两组基础工具相同。
MEMORY_TOOLS = ("memory_search", "memory_get")  # 仅 B 组可用的只读工具。
MODEL_PROVIDER = "custom-memory-switch"  # 本轮独立模型目录标识。
SCORER_IMAGE = "sha256:501605cc419443e770dcd7da28ae65cd060bd70f68f8c1bc36f89042d6adf596"  # 固定本机评分镜像。
EXPIRED_AT = "2000-01-01T00:00:00+00:00"  # 到期题的稳定失效边界。


def fingerprint(value: object) -> str:
    """为 JSON 证据生成稳定 SHA-256，忽略字典录入顺序。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    """将冻结题的 POSIX 相对文件名转换为工作区路径，拒绝任何父级或驱动器越界。"""
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or PureWindowsPath(relative).drive or "\\" in relative:
        raise ValueError(f"题目文件路径不合法：{relative}")
    return root.joinpath(*path.parts)


def write_files(root: Path, entries: dict[str, str]) -> None:
    """将冻结文件的原始 UTF-8 字节写入指定项目，保留行尾和目录结构。"""
    for relative, value in entries.items():
        path = safe_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode("utf-8"))


def load_cases() -> list[dict]:
    """载入新题并验证项目和类型配额、学习/交付文件关系，以及实际题面。"""
    from control_cases import CASES as CONTROLS
    from reuse_cases import CASES as REUSE
    from stale_cases import CASES as STALE

    # 三种题型交替进入批次，避免费用或宿主异常把剩余计划全留给一种题型。
    remaining = [list(REUSE), list(STALE), list(CONTROLS)]
    cases = [group.pop(0) for _ in range(6) for group in remaining if group]
    if Counter(case["kind"] for case in cases) != {"reuse": 6, "stale": 4, "control": 2}:
        raise ValueError("题型配额不符")
    if Counter(case["project"] for case in cases) != {"parcel": 4, "config": 4, "digest": 4}:
        raise ValueError("每个项目应有四道独立任务")
    if len({case["id"] for case in cases}) != 12:
        raise ValueError("出现重复题号")
    for case in cases:
        current = case["files"] | case.get("updates", {})
        foreign_episodes = {note["episode"] for note in case["notes"] if note.get("foreign")}
        if case["target"] not in current:
            raise ValueError(f"评分目标必须在基线中有固定初始版本：{case['id']}")
        for index, episode in enumerate(case["episodes"]):
            if case["target"] in episode["read"] or not episode["read"]:
                raise ValueError(f"学习材料必须独立于交付文件：{case['id']}")
            files = case.get("foreign_files", {}) if index in foreign_episodes else case["files"]
            if not set(episode["read"]) <= files.keys():
                raise ValueError(f"学习材料文件缺失：{case['id']}")
        for note in case["notes"]:
            MemoryContent.model_validate(note["content"])
            if not 0 <= note["episode"] < len(case["episodes"]):
                raise ValueError(f"学习证据索引不合法：{case['id']}")
        if case["kind"] != "reuse" and (not case.get("stale_values") or not case.get("misuse")):
            raise ValueError(f"需要预注册错误值与实际产物检查：{case['id']}")
    return cases


def config(arm: str, *, learning: bool = False) -> RunConfig:
    """冻结模型与工具预算；学习使用离线客户端，答题阶段只改变记忆读取状态。"""
    enabled = arm == "B" and not learning
    provider = CustomProvider(name="DeepSeek Flash", base_url="https://api.deepseek.com", default_model=MODEL_NAME)
    return RunConfig(
        provider=MODEL_PROVIDER, requested_model=MODEL_NAME, custom_providers={MODEL_PROVIDER: provider},
        system_prompt="你是项目维护助手。请直接用当前工作区的文件工具完成最新用户任务，按需核对当前项目规则。"
        "优先做小而可验证的修改，评分由宿主独立执行。产物完成后简短答复。",
        generation={"thinking": "disabled"}, tools=[name for name in TOOLS if name != "list_files"],
        context={"compression": "off", "max_input_bytes": 262144, "budget_projection": False,
                 "root_handoff": False, "load_project_instructions": False},
        memory={"use": enabled, "capture": learning, "history": enabled, "topics": enabled},
        permission_policy={"command": "deny", "external": "deny"}, auto_repair=False,
        limits={"max_model_turns": 8, "max_tool_calls": 20, "run_timeout_seconds": 120,
                "model_max_output_tokens": 4096, "max_http_attempts_per_turn": 1},
    ).freeze_models()


class EvalTools(ToolExecutor):
    """按实验臂暴露固定的文件工具和只读记忆能力。"""

    def _enabled_names(self) -> list[str]:
        """返回当前可见也可执行的工具，交接阶段不追加其他能力。"""
        if self.handoff:
            return []
        return list(TOOLS + MEMORY_TOOLS if self.config.memory.use else TOOLS)


class SeedReader:
    """用确定性消息触发真实读取，为项目生成可核验的学习回执。"""

    def __init__(self, paths: list[str]) -> None:
        """固定本次学习请求的文件路径。空集合只产生占位的无网络预检答复。"""
        self.paths = list(paths)  # 首轮批量读取，随后正常收尾。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """提供一次真实工具调用的批次和零 token 的固定终态，不使用网络。"""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self.paths:
            calls = [ToolCall("seed-" + str(index), "read_file", json.dumps({"path": path, "max_bytes": 8192}))
                     for index, path in enumerate(self.paths)]
            self.paths.clear()
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}
                for call in calls
            ]}
            return ModelResponse(message, calls, "tool_calls", "offline-seed", usage=usage)
        return ModelResponse({"role": "assistant", "content": "学习记录生成完毕。"}, [], "stop", "offline-seed", usage=usage)


def file_reads(events: list) -> list[dict]:
    """从真实的成功文件读取回执提取同版本字节范围，略过空页。"""
    reads = []
    for event in events:
        if event.type != "tool_result" or event.payload.get("name") != "read_file":
            continue
        result = event.payload["result"]
        data = result.get("data") or {}
        if result.get("ok") and data.get("bytes_read", 0) > 0 and data.get("sha256"):
            reads.append({key: data[key] for key in ("path", "sha256", "offset", "bytes_read")})
    return reads


def repeated(learning: list[dict], current: list[dict], target: str) -> tuple[int, int]:
    """以同路径同版本的学习区间并集计算答题重读调用和重叠字节。"""
    ranges = defaultdict(list)
    for data in learning:
        lo = data["offset"]
        if data["path"] != target:
            ranges[(data["path"], data["sha256"])].append((lo, lo + data["bytes_read"]))
    unions = {}
    for key, spans in ranges.items():
        union: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if union and start <= union[-1][1]:
                union[-1] = (union[-1][0], max(union[-1][1], end))
            else:
                union.append((start, end))
        unions[key] = union
    hits = overlap_bytes = 0
    for data in current:
        lo, hi = data["offset"], data["offset"] + data["bytes_read"]
        overlap = sum(max(0, min(hi, end) - max(lo, start))
                      for start, end in unions.get((data["path"], data["sha256"]), []))
        hits += int(overlap > 0)
        overlap_bytes += overlap
    return hits, overlap_bytes


def read_control(case: dict, root: Path, runs: Path, arm: str = "B") -> MemoryController:
    """用原产品控制器查看冻结作用域和有效条目，预检时忽略显示事件。"""
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt=case["prompt"], workspace=root), config=config(arm))
    return MemoryController(runs, manifest, lambda kind, payload: None)


def get_code(controller: MemoryController, spec: MemoryGet) -> str:
    """执行一条真实记忆回读，返回成功或产品的稳定拒绝代码。"""
    try:
        controller.execute("memory_get", spec)
    except MemoryError as error:
        return error.code
    return "ok"


async def seed(case: dict, slot: Path, runs: Path) -> tuple[Path, list[dict], dict]:
    """为一条试次生成隔离学习记录、已确认条目和失效探针，再返回答题工作区。"""
    workspace = slot / "workspace"
    workspace.mkdir(parents=True)
    write_files(workspace, case["files"])
    foreign_episodes = {note["episode"] for note in case["notes"] if note.get("foreign")}
    foreign = slot / "foreign"
    if foreign_episodes:
        foreign.mkdir()
        write_files(foreign, case.get("foreign_files", case["files"]))
    all_reads, episodes = [], []
    learning_config = config("A", learning=True)
    for index, episode in enumerate(case["episodes"]):
        location = foreign if index in foreign_episodes else workspace
        task = prepare_task(episode["prompt"], location, python_executable=sys.executable)
        manifest = RunManifest(run_id=uuid4(), task=task, config=learning_config)
        outcome = await run_task(manifest, runs, "offline", executor=EvalTools(location, runs, learning_config),
                                 model_client=SeedReader(episode["read"]))
        if outcome.result.execution_status != "completed" or outcome.result.http_attempts:
            raise ValueError(f"学习回放未得到零模型正常终态：{case['id']} #{index}")
        history = RunStore.read(runs, str(manifest.run_id))
        reads = file_reads(history.events)
        if {item["path"] for item in reads} != set(episode["read"]):
            raise ValueError(f"学习计划与真实读取不一致：{case['id']} #{index}")
        if location == workspace:
            all_reads.extend(reads)
        projected = project_events(history)
        refs = [value for value in projected if value["kind"] in {"observation", "user"}]
        if not any(value["kind"] == "observation" for value in refs):
            raise ValueError(f"学习事件没有可索引的文件证据：{case['id']} #{index}")
        episodes.append((location, refs, str(manifest.run_id)))

    registered = []
    store = MemoryStore(runs)
    markers = case.get("stale_values", [])
    for note in case["notes"]:
        location, refs, _ = episodes[note["episode"]]
        content = MemoryContent.model_validate(note["content"])
        if note["status"] == "expired":
            content = content.model_copy(update={"expires_at": None})
        service = MemoryService(store, location)
        provenance = {"kind": "evaluation_fixture", "refs": [evidence_metadata(item) for item in refs]}
        item = service.create(content, str(uuid4()), provenance=provenance)
        get_note = MemoryGet(memory_id=item["id"])
        owner = read_control(case, location, runs)
        for path in case["episodes"][note["episode"]]["read"]:
            owner.before_tool("read_file", ReadFileArguments(path=path), "execution")
        if get_code(owner, get_note) != "ok":
            raise ValueError(f"生效状态下的正对照回读失败：{case['id']}")
        old_refs = [entry["id"] for entry in refs if any(marker in entry["text"] for marker in markers)]
        for ref in old_refs:
            if get_code(owner, MemoryGet(evidence_id=ref)) != "ok":
                raise ValueError(f"所有者的真实历史证据无法回读：{case['id']}")
        registered.append((note, location, content, item, provenance, old_refs))

    write_files(workspace, case.get("updates", {}))
    for note, location, content, item, provenance, old_refs in registered:
        status = note["status"]
        service = MemoryService(store, location)
        if status == "expired":
            expired = content.model_copy(update={"expires_at": EXPIRED_AT})
            service.update(item["id"], MemoryChange(request_id=uuid4(), action="edit",
                           expected_revision=item["revision"], content=expired))
        elif status == "withdrawn":
            service.update(item["id"], MemoryChange(request_id=uuid4(), action="delete",
                           expected_revision=item["revision"]))
        elif status == "replaced":
            service.create(MemoryContent.model_validate(note["replacement"]), str(uuid4()), provenance=provenance,
                           replacements=[{"id": item["id"], "revision": item["revision"]}])

    primary = read_control(case, workspace, runs)
    probes = []
    for note, location, content, item, _, old_refs in registered:
        old_note = MemoryGet(memory_id=item["id"])
        status = note["status"]
        fact_changed = content.type == "fact" and bool(set(content.evidence) & case.get("updates", {}).keys())
        expected = "ok"
        if status in {"withdrawn", "replaced"} or location != workspace:
            expected = "memory_not_found"
        elif status == "expired" or fact_changed or content.applies_to.paths:
            expected = "memory_needs_review"
        actual = get_code(primary, old_note)
        selected = any(value["id"] == item["id"] for value in primary.selected)
        notes = primary.execute("memory_search", MemorySearch(query="", collection="notes"))["items"]
        visible = any(value["id"] == item["id"] for value in notes)
        if actual != expected or (expected != "ok" and (selected or visible)):
            raise ValueError(f"失效条目探针不符：{case['id']} {status} {expected} {actual}")
        checks = []
        history_page = primary.execute("memory_search", MemorySearch(query="", collection="history"))["items"]
        history_ids = {value["id"] for value in history_page}
        for ref in old_refs:
            history_wanted = "memory_not_found" if status == "withdrawn" or location != workspace else "ok"
            history_actual = get_code(primary, MemoryGet(evidence_id=ref))
            if history_actual != history_wanted or (history_wanted != "ok" and ref in history_ids):
                raise ValueError(f"旧历史的撤回、保留或隔离语义不符：{case['id']}")
            checks.append({"ref": ref, "expected": history_wanted, "actual": history_actual})
        probes.append({"status": status, "expected": expected, "actual": actual, "selected": selected,
                       "search_visible": visible, "history": checks})
    closed = read_control(case, workspace, runs, "A")
    if closed.input_text() or closed.history_cutoff:
        raise ValueError(f"关闭组意外读取记忆：{case['id']}")
    normalized = [{"actual": probe["actual"], "expected": probe["expected"],
                   "historical": [item["actual"] for item in probe["history"]]} for probe in probes]
    return workspace, all_reads, {"learning_runs": [entry[2] for entry in episodes], "probes": probes,
                                  "fixture_sha256": fingerprint([all_reads, normalized, case["files"] | case.get("updates", {})])}


class EvidenceTransport(httpx.AsyncHTTPTransport):
    """仅在已批准的实际 HTTPS 请求中记录陈旧材料是否进入模型输入。"""

    def __init__(self, markers: list[str]) -> None:
        """保留要核对的已冻结旧值。完整请求由原 Runner 按产品路径产生。"""
        # 自定义传输必须自己继承产品的系统信任库；外层 AsyncClient 的 verify 不会替换它。
        super().__init__(verify=ssl.create_default_context(), trust_env=False, retries=0)
        self.markers = markers  # 旧值或干扰值的精确字符串。
        self.attempts: list[dict] = []  # 仅保存布尔结果和调用次序。

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """仅发送官方 HTTPS 请求，按实际消息角色区分自动注入和记忆工具正文。"""
        if request.url.scheme != "https" or request.url.host != "api.deepseek.com" or request.url.path != "/chat/completions":
            raise ValueError("正式模型请求偏离批准的官方端点")
        messages = json.loads(request.content)["messages"]
        names = {call["id"]: call["function"]["name"]
                 for message in messages for call in message.get("tool_calls") or []}
        old_prompt = old_tool = False
        for message in messages:
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if message["role"] == "user" and "<project_memory>" in content:
                memory = content.split("<project_memory>", 1)[1].split("</project_memory>", 1)[0]
                old_prompt |= any(marker in memory for marker in self.markers)
            if message["role"] == "tool" and names.get(message.get("tool_call_id")) in MEMORY_TOOLS:
                old_tool |= any(marker in content for marker in self.markers)
        response = await super().handle_async_request(request)
        self.attempts.append({"old_auto_input": old_prompt, "old_tool_input": old_tool,
                              "status": response.status_code})
        return response


def grade(workspace: Path, evidence: Path, program: str, label: str) -> dict:
    """离线容器执行独立评分，返回确定退出码并保存两条标准流。"""
    evidence.mkdir(parents=True, exist_ok=True)
    script = evidence / (label + ".py")
    script.write_text(program, encoding="utf-8")
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "256m", "--cpus", "1", "--pids-limit", "48",
        "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m", "-v", f"{workspace}:/candidate:ro",
        "-v", f"{script}:/grader/run.py:ro", "--workdir", "/candidate", "--entrypoint", "python",
        SCORER_IMAGE, "-I", "-B", "-c",
        "import runpy,sys;sys.path.insert(0,'/candidate');runpy.run_path('/grader/run.py',run_name='__main__')",
    ]
    try:
        finished = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": type(error).__name__, "exit_code": None}
    (evidence / (label + ".stdout")).write_bytes(finished.stdout)
    (evidence / (label + ".stderr")).write_bytes(finished.stderr)
    return {"exit_code": finished.returncode, "error": None if finished.returncode in (0, 1) else "grader_process",
            "exception": b"Traceback" in finished.stderr}


async def trial(case: dict, arm: str, batch: Path, *, key: str, guard: BatchBudget | None, real: bool) -> dict:
    """启动一条独立答题 Run，评分实际产物，并统计真实文件重读和记忆行为。"""
    slot = batch / "slots" / case["id"] / arm
    runs = batch / "runs"
    workspace = slot / "workspace"
    learning_row = json.loads((slot / "seed.json").read_bytes())
    initial = safe_path(workspace, case["target"]).read_bytes()
    settings = config(arm)
    task = prepare_task(case["prompt"], workspace, python_executable=sys.executable)
    manifest = RunManifest(run_id=uuid4(), task=task, config=settings)
    atomic_json(slot / "started.json", {"run_id": str(manifest.run_id)})
    transport = EvidenceTransport(case.get("stale_values", [])) if real else None
    outcome = await run_task(manifest, runs, key, executor=EvalTools(workspace, runs, settings),
                             model_client=None if real else SeedReader([]), transport=transport, model_request_guard=guard)
    result = outcome.result
    history = RunStore.read(runs, str(manifest.run_id))
    folder = slot / "grade"
    score = grade(workspace, folder, case["score"], "independent")
    target = safe_path(workspace, case["target"])
    content = target.read_bytes() if target.is_file() else None
    changed = content is not None and content != initial
    if content is not None:
        (folder / "candidate.bin").write_bytes(content)
    misuse = None
    if case.get("misuse") and changed:
        outcome_misuse = grade(workspace, folder, case["misuse"], "misuse")
        if outcome_misuse["error"] is None and not outcome_misuse["exception"]:
            misuse = outcome_misuse["exit_code"] == 0
        atomic_json(folder / "misuse.json", outcome_misuse)
    patch = None if score["error"] else score["exit_code"] == 0
    delivery = None if patch is None else bool(patch and changed and result.execution_status == "completed"
        and bool(result.final_answer) and result.cleanup_status in {"complete", "not_needed"})
    atomic_json(folder / "score.json", score)
    reads, nbytes = repeated(learning_row["reads"], file_reads(history.events), case["target"])
    successful_memory = [event.payload["result"]["data"] for event in history.events
                         if event.type == "tool_result" and event.payload.get("name") in MEMORY_TOOLS
                         and event.payload["result"].get("ok")]
    markers = case.get("stale_values", [])
    retrieved = any(marker in json.dumps(data, ensure_ascii=False) for data in successful_memory for marker in markers)
    requests = transport.attempts if transport is not None else []
    exposed = any(row["old_auto_input"] or row["old_tool_input"] for row in requests if row["status"] < 400)
    if arm == "A" and (successful_memory or exposed):
        raise ValueError(f"关闭组出现记忆读取：{case['id']}")
    atomic_json(slot / "measurements.json", {"outbound_evidence": requests, "actual_reads": file_reads(history.events)})
    return {"case_id": case["id"], "title": case["title"], "kind": case["kind"], "project": case["project"], "arm": arm,
            "run_id": str(manifest.run_id), "execution_status": result.execution_status, "stop_reason": result.stop_reason,
            "patch_passed": patch, "delivery": delivery, "changed": changed, "misuse": misuse,
            "repeated_reads": reads, "repeated_bytes": nbytes, "old_retrieved": retrieved, "old_exposed": exposed,
            "search_calls": sum(e.type == "tool_result" and e.payload.get("name") == "search_files" for e in history.events),
            "tool_calls": result.tool_calls, "memory_calls": sum(e.type == "tool_result" and e.payload.get("name") in MEMORY_TOOLS for e in history.events),
            "usage": result.usage.model_dump(), "usage_complete": result.usage_complete,
            "http_attempts": result.http_attempts, "cleanup_status": result.cleanup_status,
            "fixture_sha256": learning_row["fixture_sha256"]}


async def prepare(cases: list[dict], schedule: list[tuple[dict, str]], batch: Path) -> list[dict]:
    """完成全部独立学习环境，任何题的两组素材指纹不一致都在发单前停止。"""
    rows, seen = [], {}
    for case, arm in schedule:
        slot = batch / "slots" / case["id"] / arm
        workspace, reads, receipt = await seed(case, slot, batch / "runs")
        previous = seen.setdefault(case["id"], receipt["fixture_sha256"])
        if previous != receipt["fixture_sha256"]:
            raise ValueError(f"同题学习素材或答题文件不一致：{case['id']}")
        entry = {"case_id": case["id"], "arm": arm, **receipt, "reads": reads}
        atomic_json(slot / "seed.json", entry)
        rows.append(entry)
    return rows


def scoring_preflight(case: dict, batch: Path) -> dict:
    """验证基线、参考解和参考解中是否含有已登记的旧值误用。"""
    folder = batch / "scoring-preflight" / case["id"]
    source = folder / "workspace"
    source.mkdir(parents=True)
    write_files(source, case["files"] | case.get("updates", {}))
    baseline = grade(source, folder, case["score"], "baseline")
    write_files(source, {case["target"]: case["reference"]})
    reference = grade(source, folder, case["score"], "reference")
    correct_uses = grade(source, folder, case["misuse"], "reference-misuse") if case.get("misuse") else None
    if (baseline["exit_code"] != 1 or reference["exit_code"] != 0 or
            (correct_uses and (correct_uses["exit_code"] != 1 or correct_uses["exception"]))):
        raise ValueError(f"评分正负对照失败：{case['id']}")
    return {"case_id": case["id"], "baseline": baseline, "reference": reference, "reference_misuse": correct_uses}


def previous_reservation(path: Path, case_fingerprint: str) -> dict:
    """保留同题单首次证书失败的原账本，并从可用额度扣除尚未结算的费用预留。"""
    path = path.resolve(strict=True)
    if not path.is_relative_to(OUTPUT / "paid") or path.name != "batch.json":
        raise ValueError("恢复范围须为本轮付费原始账本")
    predecessor = json.loads(path.read_bytes())
    budget = predecessor["budget"]
    requests = budget["requests"]
    if (predecessor["case_fingerprint"] != case_fingerprint or predecessor["state"] != "stopped"
            or len(requests) != 1 or requests[0]["error_code"] != "model_tls_verification"
            or requests[0]["status"] != "unknown" or budget["limit_usd_picos"] != 4 * USD_PICOS
            or budget["known_usd_picos"] or len(predecessor["trials"]) != 1):
        raise ValueError("该账本不符合首次 TLS 失败的恢复条件")
    unit = USD_PICOS // 100
    reserved_cents = (budget["reserved_usd_picos"] + unit - 1) // unit
    return {"previous_batch": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
            "held_cents": reserved_cents, "original_limit_cents": 400,
            "previous_outcome": "model_tls_verification"}


async def main() -> None:
    """运行完整零模型预检，或在预检题单指纹一致后启动 24 条付费试次。"""
    from report import render, summarize

    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--real", action="store_true", help="启动已批准的正式模型评测。")
    args.add_argument("--key-from-stdin", action="store_true", help="从受保护的匿名管道读取一次密钥。")
    args.add_argument("--recover-from", type=Path, help="保留首次证书校验失败账本，按剩余额度恢复。")
    flags = args.parse_args()
    key = sys.stdin.readline(2048).strip() if flags.key_from_stdin else "offline"
    if flags.real and (not flags.key_from_stdin or not valid_model_key(key)):
        raise SystemExit("正式评测缺少官方 API 凭据。")
    cases = load_cases()
    case_fingerprint = fingerprint(cases)
    recovery = previous_reservation(flags.recover_from, case_fingerprint) if flags.recover_from else None
    if flags.real:
        preflight = json.loads((OUTPUT / "preflight-latest.json").read_bytes())
        if preflight["state"] != "passed" or preflight["case_fingerprint"] != case_fingerprint:
            raise SystemExit("同一题单尚未通过预检。")
        validate_billing_config(config("A"))
        validate_billing_config(config("B"))
    batch = OUTPUT / ("paid" if flags.real else "preflight") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    schedule = [(case, arm) for index, case in enumerate(cases) for arm in ("AB" if index % 2 == 0 else "BA")]
    rows: list[dict] = []
    report = {"state": "preparing", "stop_reason": None, "case_fingerprint": case_fingerprint, "active": None,
              "schedule": [dict(case_id=case["id"], title=case["title"], kind=case["kind"], arm=arm) for case, arm in schedule],
              "trials": rows, "scorer_image": SCORER_IMAGE}

    def save() -> None:
        """原子持久化完整账本与当前活动试次，供批准后监控。"""
        report["summary"] = summarize(rows)
        atomic_json(batch / "batch.json", report)

    atomic_json(batch / "frozen-cases.json", {"case_fingerprint": case_fingerprint, "cases": cases})
    print(f"Batch: {batch}", flush=True)
    save()
    seeds = await prepare(cases, schedule, batch)
    atomic_json(batch / "seeds.json", {"preparations": seeds})
    if not flags.real:
        scores = [await asyncio.to_thread(scoring_preflight, case, batch) for case in cases]
        checks = [await trial(case, arm, batch, key=key, guard=None, real=False)
                  for case, arm in schedule if case["id"] == cases[0]["id"]]
        if any(row["http_attempts"] or row["patch_passed"] is not False for row in checks):
            raise ValueError("两组的零模型 Runner 入口预检失败")
        report.update(state="passed", scores=scores, controls=checks, seeds=len(seeds))
        save()
        atomic_json(OUTPUT / "preflight-latest.json", {"state": "passed", "case_fingerprint": case_fingerprint,
                    "batch": str(batch), "questions": len(cases), "external_http": 0})
        print("Preflight passed 12/12; external requests 0.", flush=True)
        return
    report.update(state="running", preflight=preflight["batch"], recovery=recovery)
    available_cents = 400 - (recovery["held_cents"] if recovery else 0)
    guard = BatchBudget(Experiment(max_cost_cents=available_cents, max_model_calls=192,
                                   max_known_tokens=192_000_000), report, save)
    save()
    for case, arm in schedule:
        if reason := guard.blocking_reason():
            report.update(state="stopped", stop_reason=reason)
            break
        report["active"] = {"case_id": case["id"], "arm": arm}
        save()
        try:
            row = await trial(case, arm, batch, key=key, guard=guard, real=True)
        except Exception as error:
            report.update(state="stopped", stop_reason=type(error).__name__)
            save()
            raise
        rows.append(row)
        report["active"] = None
        print(f"{case['id']} {arm} {row['execution_status']}/{row['stop_reason']} "
              f"patch={row['patch_passed']} read={row['repeated_reads']} misuse={row['misuse']}", flush=True)
        if guard.stop_reason or row["patch_passed"] is None or row["cleanup_status"] not in {"complete", "not_needed"}:
            report.update(state="stopped", stop_reason=guard.stop_reason or "evaluation_infrastructure")
            save()
            break
        save()
    if report["state"] == "running":
        report["state"] = "complete"
    save()
    (batch / "report.md").write_text(render(report), encoding="utf-8")
    print(f"{report['state']} {len(rows)}/{len(schedule)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
