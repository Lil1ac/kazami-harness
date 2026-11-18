"""用真实 Runner 对 12 个已冻结检查点续跑，并分别验收实际补丁和完整交付。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime
from difflib import unified_diff
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from agent_harness.config import prepare_task
from agent_harness.evaluation.budget import BatchBudget, MODEL_NAME, validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.models import Experiment
from agent_harness.model_selection import CustomProvider, valid_model_key
from agent_harness.providers.base import ModelResponse
from agent_harness.runtime.runner import run_task
from agent_harness.schemas import ContextPolicy, HistorySource, RunConfig, RunManifest, RunResult, TaskSpec
from agent_harness.storage.runs import RunStore
from agent_harness.storage.sessions import history_messages, payload_digest
from agent_harness.tools.registry import ToolExecutor

from continuation_report import render, summarize
from freeze import HERE, OUTPUT
from replay import _input_content


BASE_TOOLS = ("list_files", "read_file", "search_files", "edit_file", "write_file")  # 续跑双方共用可读写的项目内工具。
PROVIDER = "custom-checkpoint-eval"  # 本轮内存连接标识，不覆盖桌面配置。
SCORER_IMAGE = "sha256:501605cc419443e770dcd7da28ae65cd060bd70f68f8c1bc36f89042d6adf596"  # 本机离线评分镜像。
INPUT_STOPS = frozenset({"max_model_input_bytes", "max_model_input_tokens"})  # 硬输入门禁原因。
SOURCES = ("log", "cross_file", "multiturn")  # 冻结题单认可的源类别。


class EvalTools(ToolExecutor):
    """将双方可见和可执行的工具固定为有界文件操作，混合组额外提供产品回读。"""

    def _enabled_names(self) -> list[str]:
        """返回当前实际工具白名单，交接时清空；规划、委派和本机命令均不进入盲评分环境。"""
        if self.handoff:
            return []
        return list(BASE_TOOLS) + (["read_context"] if self.config.context.compression == "hybrid" else [])


class OfflineFinish:
    """零请求预检时验证真实初始化路径和输入门禁的固定响应。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接受已投影消息和工具，返回固定结束回执，不读盘、不编辑、不发请求。"""
        return ModelResponse({"role": "assistant", "content": "零请求预检"}, [], "stop", MODEL_NAME,
                             usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


def read_json(path: Path) -> dict:
    """读取一个冻结清单或评分文件，格式错误交由调度报告。"""
    return json.loads(path.read_bytes())


def digest(data: bytes) -> str:
    """返回原始字节的 SHA-256，用于冻结核验和评分证据。"""
    return sha256(data).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    """记录工作区普通文件的相对路径和哈希，拒绝可指向外部对象的符号链接。"""
    files = {}
    for file in root.rglob("*"):
        if file.is_symlink():
            raise ValueError("候选目录含符号链接")
        if file.is_file():
            files[file.relative_to(root).as_posix()] = digest(file.read_bytes())
    return files


def load_cases() -> tuple[list[dict], ContextPolicy]:
    """校验冻结检查点和观察过的源码字节，返回原始有序题单与压缩策略。"""
    manifest = read_json(OUTPUT / "manifest.json")
    if len(manifest["cases"]) != 12 or len({item["id"] for item in manifest["cases"]}) != 12:
        raise ValueError("冻结清单不是十二道独立题")
    cases = []
    for expected in manifest["cases"]:
        item = read_json(OUTPUT / "checkpoints" / (expected["id"] + ".json"))
        data = _input_content(item["messages"], item["tools"])
        if (len(data) != expected["input_bytes"] or digest(data) != expected["checkpoint_sha256"]
                or item["category"] not in SOURCES or item["messages"][-1]["role"] != "user"):
            raise ValueError("检查点内容变化：" + item["id"])
        workspace = Path(item["workspace"]).resolve(strict=True)
        for relative, previous in item["source_files"].items():
            path = (workspace / relative).resolve(strict=True)
            if not path.is_relative_to(workspace) or digest(path.read_bytes()) != previous:
                raise ValueError("冻结源码变化：" + relative)
        cases.append(item)
    return cases, ContextPolicy.model_validate(manifest["policy"])


def snapshot_sources(cases: list[dict], destination: Path) -> dict[str, Path]:
    """按项目建立新只读基线副本，保留所有曾观察的文件相对位置和字节。"""
    stored = {}
    for category in SOURCES:
        subset = [item for item in cases if item["category"] == category]
        source = Path(subset[0]["workspace"])
        if any(Path(item["workspace"]) != source for item in subset):
            raise ValueError("同一项目的起始工作区不一致")
        target = destination / category
        target.mkdir(parents=True)
        if category == "log":
            for package in ("pytest", "_pytest"):
                shutil.copytree(source / package, target / package, ignore=shutil.ignore_patterns("__pycache__"))
        elif category == "cross_file":
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            # 标准库答题模块是根目录文件，保留所有根模块以及检查点引用的包内来源。
            relpaths = {path.name for path in source.glob("*.py")}
            relpaths.update(path for item in subset for path in item["source_files"])
            for relative in relpaths:
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, path)
        for item in subset:
            for relative, expected in item["source_files"].items():
                if digest((target / relative).read_bytes()) != expected:
                    raise ValueError("工作区快照字节不一致：" + relative)
        stored[category] = target
    return stored


def configuration(case: dict, policy: ContextPolicy, arm: str) -> RunConfig:
    """冻结 A(off) 或 B(hybrid) 的运行配置，保护历史调用格式并固定计费边界。"""
    settings = CustomProvider(name="DeepSeek Flash", base_url="https://api.deepseek.com", default_model=MODEL_NAME)
    context = policy.model_copy(update={"compression": "off" if arm == "A" else "hybrid",
                                        "load_project_instructions": False, "root_handoff": False,
                                        "root_handoff_after_input_rejection": False, "budget_projection": False})
    return RunConfig(
        provider=PROVIDER, requested_model=MODEL_NAME, custom_providers={PROVIDER: settings},
        context=context, tools=[name for name in BASE_TOOLS if name != "list_files"],
        system_prompt=case["messages"][0]["content"] + "\n直接在当前工作区完成末轮代码修改。"
        "先用文件工具核对源码。由宿主在本次答复结束后执行独立评分。评分材料在工作区外。",
        generation={"thinking": "disabled"}, memory={"use": False, "capture": False},
        permission_policy={"command": "deny"}, auto_repair=False,
        limits={"max_model_turns": 20, "max_tool_calls": 40, "run_timeout_seconds": 300,
                "model_max_output_tokens": 4096, "max_http_attempts_per_turn": 1},
    ).freeze_models()


def history_parent(case: dict, config: RunConfig, workspace: Path, runs: Path) -> tuple[RunManifest, HistorySource]:
    """把同一份脚本化前序写成普通可核验父记录，不重放历史工具动作。"""
    parent_id = uuid4()
    parent = RunManifest(run_id=parent_id, session_id=parent_id,
                         task=TaskSpec(prompt=case["messages"][1]["content"], workspace=workspace), config=config)
    store = RunStore.create(runs, parent)
    store.append("frozen_history_seed", {"case_id": case["id"], "checkpoint_sha256": case["checkpoint_sha256"]})
    store.append("run_started", {"messages": case["messages"][:-1], "tools": case["tools"], "conversation_start": 1})
    store.finalize(RunResult(run_id=parent_id, execution_status="completed", usage_complete=True))
    saved = RunStore.read(runs, str(parent_id))
    inherited = history_messages(saved)
    if inherited != case["messages"][1:-1]:
        raise ValueError("Runner 解出的历史与冻结原文不一致")
    return parent, HistorySource(last_seq=saved.events[-1].seq, messages_sha256=payload_digest(inherited))


def unified_patch(snapshot: Path, workspace: Path, target: Path) -> list[str]:
    """按哈希逐文件留存实际差异，返回所有发生修改的相对路径。"""
    before, after = inventory(snapshot), inventory(workspace)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    lines = []
    for relative in changed:
        old = (snapshot / relative).read_bytes() if relative in before else b""
        new = (workspace / relative).read_bytes() if relative in after else b""
        try:
            old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeError:
            lines.append(f"Binary path changed: {relative}\n")
            continue
        lines.extend(unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                  fromfile="a/" + relative, tofile="b/" + relative))
    target.write_text("".join(lines), encoding="utf-8", newline="\n")
    return changed


def graders(case: dict, workspace: Path, evidence: Path, dependencies: Path) -> dict:
    """在无网络、只读挂载的容器中执行既有评分器；保留进程回执和机器评分。"""
    category, identifier = case["category"], case["id"]
    mount_source = workspace.parent if category == "cross_file" else workspace
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "512m", "--cpus", "1", "--pids-limit", "64",
        "--tmpfs", "/tmp:rw,nosuid,noexec,size=32m", "--workdir", "/tmp",
        "-v", f"{mount_source}:/candidate:ro", "-v", f"{HERE}:/grader:ro", "-v", f"{dependencies}:/deps:ro",
        "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "-e", f"AH_REPLAY_LOG_CASE={identifier}",
        "-e", "AH_REPLAY_LOG_CANDIDATE=/candidate", "--entrypoint", "python", SCORER_IMAGE, "-I", "-B",
    ]
    if category == "log":
        command += ["-c", "import sys;sys.path.insert(0,'/deps');import pytest;"
                    "raise SystemExit(pytest.main(['-q','--tb=line','-p','no:cacheprovider','/grader/log_tasks.py']))"]
    elif category == "cross_file":
        command += ["/grader/cross_file/checks.py", "/candidate", identifier]
    else:
        command += ["/grader/multiturn/precheck.py", "--case", identifier, "--candidate", "/candidate"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "error", "reason": type(error).__name__}
    (evidence / "scorer.stdout").write_bytes(result.stdout)
    (evidence / "scorer.stderr").write_bytes(result.stderr)
    if category == "log":
        status = "passed" if result.returncode == 0 else "failed" if result.returncode == 1 else "error"
        return {"status": status, "exit_code": result.returncode}
    try:
        score = json.loads(result.stdout)
        if category == "cross_file" and result.returncode == 0:
            checks = [row for group in ("regression", "feature") for row in score[group].values()]
            status = "passed" if checks and all(row["passed"] for row in checks) else "failed"
        else:
            status = {0: "passed", 1: "failed"}.get(result.returncode, "error")
            if category == "cross_file":
                status = "error" if result.returncode in {125, 126, 127} or "regression" not in score else "failed"
    except (ValueError, TypeError, KeyError):
        return {"status": "error", "exit_code": result.returncode, "reason": "scorer_response_unreadable"}
    return {"status": status, "exit_code": result.returncode, "details": score}


async def run_one(case: dict, arm: str, policy: ContextPolicy, snapshot: Path, batch: Path, key: str,
                  guard: BatchBudget | None, offline: bool, pre_request: Callable[[str], None]) -> dict:
    """从冻结历史续跑一臂，先记录活动 Run ID，再用独立环境判断补丁和完整交付。"""
    slot = batch / "slots" / case["id"] / arm
    workspace = slot / ("click" if case["category"] == "cross_file" else "workspace")
    shutil.copytree(snapshot, workspace)
    runs = batch / "runs"
    config = configuration(case, policy, arm)
    validate_billing_config(config)
    parent, source = history_parent(case, config, workspace, runs)
    task = prepare_task(case["messages"][-1]["content"], workspace, python_executable=sys.executable)
    manifest = RunManifest(run_id=uuid4(), session_id=parent.session_id, parent_run_id=parent.run_id,
                           turn_index=2, task=task, config=config, history_source=source)
    pre_request(str(manifest.run_id))
    result = (await run_task(manifest, runs, key, executor=EvalTools(workspace, runs, config),
                             model_client=OfflineFinish() if offline else None, model_request_guard=guard)).result
    history = RunStore.read(runs, str(manifest.run_id))
    starts = [event.payload for event in history.events if event.type == "run_started"]
    expected_messages = case["messages"][1:]
    if len(starts) != 1 or starts[0]["messages"][starts[0]["conversation_start"]:] != expected_messages:
        raise ValueError("正式 Runner 未完整继承检查点：" + case["id"])
    checks = [event.payload for event in history.events if event.type == "context_checked"
              and event.payload.get("purpose") == "execution"]
    changed = unified_patch(snapshot, workspace, slot / "candidate.patch")
    grade = graders(case, workspace, slot, batch / "scorer-deps")
    atomic_json(slot / "score.json", grade)
    patch_pass = grade["status"] == "passed" if grade["status"] in {"passed", "failed"} else None
    delivery = (patch_pass and bool(changed) and result.execution_status == "completed"
                and result.cleanup_status in {"complete", "not_needed"} and bool(result.final_answer)) if patch_pass is not None else None
    return {"case_id": case["id"], "title": case["title"], "category": case["category"], "band": case["band"],
            "arm": arm, "run_id": str(manifest.run_id), "run_directory": str(runs / str(manifest.run_id)),
            "checkpoint_sha256": case["checkpoint_sha256"], "execution_status": result.execution_status,
            "stop_reason": result.stop_reason, "cleanup_status": result.cleanup_status, "grade_status": grade["status"],
            "patch_passed": patch_pass, "delivery": delivery, "changed_files": changed,
            "initial_check": checks[0] if checks else None, "max_execution_input_bytes": max((item["input_bytes"] for item in checks), default=None),
            "model_turns": result.model_turns, "http_attempts": result.http_attempts, "tool_calls": result.tool_calls,
            "usage": result.usage.model_dump(), "usage_complete": result.usage_complete,
            "cost": guard.run_cost(str(manifest.run_id)) if guard else None}


def stage_grader_dependencies(destination: Path, log_source: Path) -> None:
    """冻结 pytest 评分运行使用的可信发行包源码，缺失依赖时在开跑前报错。"""
    destination.mkdir()
    for name in ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "pygments"):
        shutil.copytree(log_source / name, destination / name, ignore=shutil.ignore_patterns("__pycache__"))


def preflight_reference(case: dict, snapshot: Path, batch: Path) -> dict:
    """验证同一离线评分环境能够判定参考修改，参考材料只在本地预检阶段使用。"""
    slot = batch / "references" / case["id"]
    category = case["category"]
    workspace = slot / ("click" if category == "cross_file" else "workspace")
    shutil.copytree(snapshot, workspace)
    if category == "log":
        from log_tasks import TASKS

        template = next(item for item in TASKS if item["id"] == case["id"])
        path = workspace / template["evidence_file"]
        original = path.read_text(encoding="utf-8")
        if original.count(template["old"]) != 1:
            raise ValueError("参考修改位置不唯一：" + case["id"])
        path.write_text(original.replace(template["old"], template["new"], 1), encoding="utf-8")
    elif category == "cross_file":
        from cross_file.reference import apply_reference

        apply_reference(workspace, case["id"])
    else:
        from multiturn.reference_solutions import apply_solution

        path = workspace / case["expected_evidence"][0]["path"]
        path.write_text(apply_solution(case["id"], path.read_text(encoding="utf-8")), encoding="utf-8")
    grade = graders(case, workspace, slot, batch / "scorer-deps")
    atomic_json(slot / "score.json", grade)
    return {"case_id": case["id"], "status": grade["status"]}


async def main() -> None:
    """先完成逐题无模型验证，再在一次性的持久账本内调度 24 个正式槽位。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="执行已批准的付费批次。")
    parser.add_argument("--key-from-stdin", action="store_true", help="从受保护的匿名管道读取一次密钥。")
    args = parser.parse_args()
    key = sys.stdin.readline(2048).strip() if args.key_from_stdin else ""
    if args.real and not valid_model_key(key):
        raise SystemExit("缺少官方模型凭据。")
    cases, policy = load_cases()
    root = OUTPUT / ("continuation" if args.real else "continuation-preflight")
    batch = root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    print(f"Batch directory: {batch}", flush=True)
    snapshots = snapshot_sources(cases, batch / "base")
    stage_grader_dependencies(batch / "scorer-deps", Path(cases[0]["workspace"]))
    schedule = [(case, arm) for index, case in enumerate(cases) for arm in ("AB" if index % 2 == 0 else "BA")]
    if not args.real:
        # 每题至少验证一次真实 Runner 接续与评分；首两题及一个超限题补双臂数据。
        double = {cases[0]["id"], cases[1]["id"], cases[2]["id"]}
        schedule = [(case, arm) for case, arm in schedule if arm == "B" or case["id"] in double]
    rows: list[dict] = []
    report = {"state": "running", "stop_reason": None, "active": None, "real": args.real,
              "schedule": [{"case_id": case["id"], "title": case["title"], "arm": arm} for case, arm in schedule],
              "trials": rows, "scorer_image": SCORER_IMAGE, "base_sha256": {name: inventory(path) for name, path in snapshots.items()}}

    def save() -> None:
        """原子保存账本、活动槽位和结果，便于中断后只读检查。"""
        report["summary"] = summarize(rows)
        atomic_json(batch / "batch.json", report)

    # 真实发单按已批准的费用与任务轮次约束；已结算 token 门槛仅取所有请求共同的服务端窗口上界。
    guard = BatchBudget(Experiment(max_cost_cents=600, max_model_calls=480, max_known_tokens=480_000_000), report, save) if args.real else None
    save()
    for case, arm in schedule:
        if guard and guard.blocking_reason():
            report.update(state="stopped", stop_reason=guard.blocking_reason())
            break

        def record_active(run_id: str) -> None:
            """在首次请求前落盘当前题目和 Run ID。"""
            report["active"] = {"case_id": case["id"], "arm": arm, "run_id": run_id}
            save()

        try:
            row = await run_one(case, arm, policy, snapshots[case["category"]], batch,
                                key if args.real else "offline", guard, not args.real, record_active)
        except Exception as error:
            report.update(state="stopped", stop_reason=type(error).__name__)
            save()
            raise
        rows.append(row)
        report["active"] = None
        print(f"{row['case_id']} {arm} {row['execution_status']}/{row['stop_reason']} "
              f"grade={row['grade_status']} tokens={row['usage']['total_tokens']}", flush=True)
        if row["grade_status"] == "error" or row["cleanup_status"] not in {"not_needed", "complete"} or (guard and guard.stop_reason):
            report.update(state="stopped", stop_reason=(guard.stop_reason if guard else None) or "evaluation_infrastructure")
            break
        save()
    if report["state"] == "running" and not args.real:
        references = [await asyncio.to_thread(preflight_reference, case, snapshots[case["category"]], batch) for case in cases]
        report["references"] = references
        if any(row["status"] != "passed" for row in references):
            report.update(state="stopped", stop_reason="reference_grader_disagreed")
    if report["state"] == "running":
        report["state"] = "complete"
    save()
    (batch / "report.md").write_text(render(rows, report["summary"], report), encoding="utf-8")
    print(f"{report['state']} {len(rows)}/{len(schedule)}", flush=True)
    if not args.real and (report["state"] != "complete" or any(row["patch_passed"] is not False for row in rows)):
        raise SystemExit("候选基线预检未通过。")


if __name__ == "__main__":
    asyncio.run(main())
