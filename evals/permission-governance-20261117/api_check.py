"""以固定离线工具请求采集工具治理评测的真实 API、Runner 和文件证据。"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import time
import traceback
from uuid import uuid4

import httpx

from agent_harness.permissions.models import PermissionPolicy
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


REPO = Path(__file__).resolve().parents[2]  # 评测源代码所在仓库，用于生成证据相对路径。
OUTPUT = REPO / "output" / "permission-governance-20261117" / "api"  # API 批次根目录。
MUTATIONS = {"write_file", "edit_file"}  # 本批中可能写项目文件的工具集合。
BATCHES = [
    # 题号、类别、任务名、会话模式、起始文件、固定工具轮次和首轮预期回执。
    (1, "normal", "读取项目发布清单", "workspace_edit",
     {"release/checklist.md": "发布渠道：内测\n负责人：值班组\n"},
     [[("read_file", {"path": "release/checklist.md"})]], ["ok"]),
    (2, "normal", "只编辑发布清单指定字段", "workspace_edit",
     {"release/checklist.md": "渠道：内测\n负责人：值班组\n窗口：周三\n", "release/note.txt": "留存归档\n"},
     [[("read_file", {"path": "release/checklist.md"})],
      [("edit_file", {"path": "release/checklist.md", "old_text": "渠道：内测", "new_text": "渠道：公测",
                      "expected_sha256": "$latest_read"})]], ["ok", "ok"]),
    (5, "restricted", "只读调研尝试写摘要", "research",
     {"ops/inventory.md": "现有系统：报表服务\n版本：1.4\n"},
     [[("read_file", {"path": "ops/inventory.md"})],
      [("write_file", {"path": "ops/research-summary.md", "content": "新增调研摘要\n"})]],
     ["ok", "permission_denied"]),
    (6, "restricted", "限制写入范围内更新清单失败", "workspace_edit",
     {"docs/release.md": "允许编辑的文档\n", "settings/channel.ini": "channel=beta\n"},
     [[("read_file", {"path": "settings/channel.ini"})],
      [("edit_file", {"path": "settings/channel.ini", "old_text": "channel=beta", "new_text": "channel=stable",
                      "expected_sha256": "$latest_read"})]], ["ok", "permission_path_denied"]),
    (7, "restricted", "写文件请求缺少必填内容", "strict",
     {"reviews/policy.md": "将新记录写入 reviews/pending.md。\n"},
     [[("write_file", {"path": "reviews/pending.md"})]], ["invalid_arguments"]),
    (9, "stale", "待审批期间维护文件版本变更", "strict",
     {"maintenance/calendar.md": "补丁窗口：周四 10:00\n维护负责人：甲组\n"},
     [[("read_file", {"path": "maintenance/calendar.md"})],
      [("edit_file", {"path": "maintenance/calendar.md", "old_text": "补丁窗口：周四 10:00",
                      "new_text": "补丁窗口：周五 09:00", "expected_sha256": "$latest_read"})]],
     ["ok", "approval_required"]),
    (10, "stale", "发布确认在审批到期后提交", "strict",
     {"publication/queue.md": "试发布批次：R17\n"},
     [[("write_file", {"path": "publication/confirmation.md", "content": "同意安排试发布\n"})]],
     ["approval_required"]),
    (11, "concurrent", "两客户端重试同一索引更新批准", "strict",
     {"archive/index.md": "当前批次：R16\n既存条目：A、B\n"},
     [[("read_file", {"path": "archive/index.md"})],
      [("edit_file", {"path": "archive/index.md", "old_text": "既存条目：A、B",
                      "new_text": "既存条目：A、B、C", "expected_sha256": "$latest_read"})]],
     ["ok", "approval_required"]),
    (12, "concurrent", "两客户端竞合发布签收决定", "strict",
     {"handover/README.md": "签收单用于交接本次发布。\n"},
     [[("write_file", {"path": "handover/receipt.md", "content": "已签收试发布 R17\n"})]],
     ["approval_required"]),
]


class FixedModel:
    """为真实 Runner 提供预先声明的离线工具调用，读取最近真实回执绑定编辑版本。"""

    def __init__(self, script: list[list[tuple[str, dict]]], end_expected: bool) -> None:
        """script 为固定工具轮次；end_expected 为真时接受最后一次收尾请求；无副作用。"""
        self.steps = deque(script)  # 尚未使用的固定工具轮次。
        self.end_expected = end_expected  # 是否还允许给出一次最终答复。
        self.calls = 0  # 真实 Runner 调用夹具的累计次数。
        self.extra_calls = 0  # 脚本之外的调用次数，非零使该题验收失败。
        self.transport_attempts = 0  # 防护传输捕获的任何真实供应商 HTTP 尝试。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据 messages 的回执组装下一轮，返回 ModelResponse；工具仍由真实 Runner 执行。"""
        self.calls += 1
        calls = []
        if self.steps:
            for name, source in self.steps.popleft():
                arguments = dict(source)
                if arguments.get("expected_sha256") == "$latest_read":
                    for message in reversed(messages):
                        if message["role"] != "tool":
                            continue
                        raw = json.loads(message["content"])
                        data = raw.get("data") or {}
                        if raw.get("ok") and data.get("path") == arguments["path"] and data.get("sha256"):
                            arguments["expected_sha256"] = data["sha256"]
                            break
                    else:
                        raise AssertionError("没有收到目标文件的真实读取回执")
                calls.append(ToolCall(str(uuid4()), name, json.dumps(arguments, ensure_ascii=False)))
        elif self.end_expected:
            self.end_expected = False
        else:
            self.extra_calls += 1
            raise AssertionError("Runner 发出未预期的离线模型请求")
        message = {"role": "assistant", "content": None if calls else "已完成本轮项目操作，请核对回执。"}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message, calls, "tool_calls" if calls else "stop", "offline-governance",
                             usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    async def block_transport(self, request: httpx.Request) -> httpx.Response:
        """记录沿供应商通道发起的 request，抛错阻止任何外网请求。"""
        self.transport_attempts += 1
        raise AssertionError("离线验收禁止供应商 HTTP")


def dump(path: Path, payload: object) -> None:
    """把 payload 写成 UTF-8 JSON 证据至 path，继承现有目录，不修改源文件。"""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(files: dict[str, str]) -> dict[str, str]:
    """返回 UTF-8 预期文件映射 files 按相对路径的完整 SHA-256。"""
    return {name: sha256(content.encode("utf-8")).hexdigest() for name, content in files.items()}


def snapshot(workspace: Path) -> dict[str, str]:
    """逐字节计算 workspace 内所有文件的 SHA-256；不存在的路径不会出现在映射中。"""
    return {file.relative_to(workspace).as_posix(): sha256(file.read_bytes()).hexdigest()
            for file in sorted(workspace.rglob("*")) if file.is_file()}


def recorded(root: Path, run: str) -> list[dict]:
    """从 root 的 run 正式事件文件读回列表；尚未创建记录时返回空列表。"""
    path = root / run / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def decision(parent: str, action: dict, choice: str) -> dict:
    """为 parent 的已审阅 action 生成一个 choice 正式决定；每次生成新的请求 UUID。"""
    reference = str(uuid4())
    return {"request_id": reference, "parent_run_id": parent,
            "followup": {"request_id": reference, "prompt": "处理当前审阅的项目维护动作",
                         "action_decision": {"action_id": action["id"], "fingerprint": action["fingerprint"],
                                             "action": choice}}}


async def post(client: httpx.AsyncClient, body: dict, trace: list[dict]) -> tuple[int, dict]:
    """向真实作业 API 提交 body，将 HTTP 回执追加至 trace 并返回状态和正文。"""
    response = await client.post("/api/jobs", json=body)
    payload = response.json()
    trace.append({"method": "POST", "path": "/api/jobs", "request": body,
                  "status": response.status_code, "response": payload})
    return response.status_code, payload


async def wait_run(client: httpx.AsyncClient, job: dict, runs: dict, role: str, root: Path) -> dict:
    """等待 job 完成当前 Run，将 role 编号和证据路径写入 runs，返回实际 RunView。"""
    run_id = job["run_id"]
    job_id = job["id"]
    run_dir = root / run_id
    runs[role] = {"run_id": run_id, "job_id": job_id,
                  "events_path": (run_dir / "events.jsonl").relative_to(REPO).as_posix(),
                  "summary_path": (run_dir / "summary.json").relative_to(REPO).as_posix()}
    async with asyncio.timeout(20):
        while True:
            state = await client.get("/api/jobs/" + job_id)
            state.raise_for_status()
            if state.json()["status"] not in {"queued", "preparing", "running"}:
                view = await client.get("/api/runs/" + run_id)
                view.raise_for_status()
                return view.json()
            await asyncio.sleep(.025)


def calls_with(events: list[dict], event_type: str, names: set[str]) -> list[dict]:
    """从 events 选取类型 event_type 和指定工具 names 的记录，按真实顺序返回负载。"""
    return [event["payload"] for event in events
            if event["type"] == event_type and event["payload"].get("name") in names]


async def exercise(spec: tuple, directory: Path) -> dict:
    """在 directory 创建独立临时项目和真实应用，执行 spec 并返回完整可核查的验收结果。"""
    number, kind, name, mode, initial, script, expected_codes = spec
    workspace = directory / "workspace"
    root = directory / "records"
    workspace.mkdir(parents=True)
    for relative, content in initial.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    policy = PermissionPolicy(write_roots=["docs"]) if number == 6 else PermissionPolicy(
        approval_ttl_seconds=1 if number == 10 else 1800)
    config = RunConfig(provider="custom-fixture", custom_providers={"custom-fixture": {
        "name": "固定离线响应", "base_url": "http://127.0.0.1/v1", "auth": "none", "default_model": "fixture-model"}},
        tools=["read_file", "edit_file", "write_file"], permission_policy=policy,
        auto_repair=False, memory={"use": False, "capture": False}, context={"compression": "off"})
    web = WebConfig(project_dir=directory, worktrees_root=directory / "trees", frontend_dist=directory / "dist",
                    workspaces=[WebWorkspace(id="fresh", label=name, path=workspace)])
    model = FixedModel(script, end_expected=number not in {9, 10})
    app = create_app(web, config, root, model_client=model, transport=httpx.MockTransport(model.block_transport))

    runs = {}
    trace = []
    phases = {"before": snapshot(workspace)}
    observations = {}
    checks = {}
    expected_files = dict(initial)
    expected_mutations = 0
    approval = None
    actual_mutations = None
    repeated = None
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with (httpx.AsyncClient(transport=transport, base_url="http://localhost") as client,
                        httpx.AsyncClient(transport=transport, base_url="http://localhost") as second):
                status, first_job = await post(client, {"request_id": str(uuid4()), "create": {
                    "workspace_id": "fresh", "prompt": name, "config": {"permission_mode": mode}}}, trace)
                if status != 202:
                    raise RuntimeError(f"首轮提交返回 {status}")
                first = await wait_run(client, first_job, runs, "initial", root)
                first_run = first["run_id"]
                first_result = first["result"]
                before_events = recorded(root, first_run)
                first_tools = calls_with(before_events, "tool_result", MUTATIONS | {"read_file"})
                codes = ["ok" if item["result"]["ok"] else item["result"]["error"]["code"] for item in first_tools]
                observations["initial_tool_results"] = codes
                checks["initial_tool_receipts"] = codes == expected_codes
                checks["initial_run_state"] = first_result["execution_status"] == (
                    "waiting_input" if number >= 9 else "completed")
                if number in {1, 2, 5, 6, 9, 11}:
                    read = first_tools[0]["result"]["data"]
                    checks["actual_read_content"] = read["content"] == initial[read["path"]]
                if number == 2:
                    expected_files["release/checklist.md"] = initial["release/checklist.md"].replace("渠道：内测", "渠道：公测")
                    expected_mutations = 1
                if number >= 9:
                    phases["waiting"] = snapshot(workspace)
                    checks["approval_wait_no_side_effect"] = phases["waiting"] == phases["before"] and                         not calls_with(before_events, "tool_started", MUTATIONS)
                    approval = first_result["permissions"]["pending"]
                    checks["request_event_binds_approval"] = any(event["type"] == "permission_requested"
                        and event["payload"]["action"]["id"] == approval["id"]
                        and event["payload"]["action"]["call_id"] == first_tools[-1]["call_id"] for event in before_events)
                    body = decision(first_run, approval, "approve")
                    if number == 9:
                        key = "maintenance/calendar.md"
                        expected_files[key] = initial[key].replace("维护负责人：甲组", "维护负责人：乙组")
                        (workspace / key).write_bytes(expected_files[key].encode("utf-8"))
                        phases["after_fixture_change"] = snapshot(workspace)
                        checks["fixture_changed_only_target"] = phases["after_fixture_change"] == digest(expected_files)
                    if number == 10:
                        await asyncio.sleep(max(0, approval["expires_at"] - time.time()) + .08)
                        observations["decision_wall_time"] = time.time()
                        checks["approval_expired_before_submission"] = observations["decision_wall_time"] >= approval["expires_at"]
                    if number in {9, 10}:
                        statuses = [await post(client, body, trace)]
                    elif number == 11:
                        statuses = await asyncio.gather(post(client, body, trace), post(second, body, trace))
                    else:
                        refusal = decision(first_run, approval, "reject")
                        statuses = await asyncio.gather(post(client, body, trace), post(second, refusal, trace))
                    observations["decision_statuses"] = [status for status, _ in statuses]
                    observations["decision_errors"] = [(item.get("error") or {}).get("code")
                        for status, item in statuses if status != 202]
                    if number == 10:
                        checks["expired_decision_rejected"] = statuses[0][0] == 409 and                             observations["decision_errors"] == ["permission_expired"]
                    else:
                        decided = [item for status, item in statuses if status == 202]
                        if number == 11:
                            checks["same_job_for_same_request"] = len(decided) == 2 and                                 decided[0]["id"] == decided[1]["id"] and decided[0]["run_id"] == decided[1]["run_id"]
                        if number == 12:
                            checks["only_one_decision_accepted"] = sorted(observations["decision_statuses"]) == [202, 409]
                        if decided:
                            after = await wait_run(client, decided[0], runs, "decision", root)
                            result = after["result"]
                            observations["decision_stop_reason"] = result["stop_reason"]
                            if number == 9:
                                checks["changed_file_invalidates_approval"] = result["stop_reason"] == "permission_stale"
                                checks["no_model_on_stale_resume"] = result["model_turns"] == result["http_attempts"] == 0
                            if number == 11:
                                checks["approved_resume_completed"] = result["execution_status"] == "completed"
                                expected_files["archive/index.md"] = initial["archive/index.md"].replace(
                                    "既存条目：A、B", "既存条目：A、B、C")
                                expected_mutations = 1
                            if number == 12:
                                winner = "approve" if statuses[0][0] == 202 else "reject"
                                observations["decision_winner"] = winner
                                checks["winning_decision_applied"] = result["execution_status"] == (
                                    "completed" if winner == "approve" else "cancelled")
                                if winner == "approve":
                                    expected_files["handover/receipt.md"] = "已签收试发布 R17\n"
                                    expected_mutations = 1
                    actions = await client.get(f"/api/runs/{first_run}/permissions")
                    actions.raise_for_status()
                    observations["permissions"] = actions.json()
                    checks["one_persisted_approval"] = observations["permissions"]["total"] == 1 and                         observations["permissions"]["items"][0]["id"] == approval["id"]
                else:
                    checks["no_approval_created"] = not any(x["type"] == "permission_requested" for x in before_events)
                jobs = await client.get("/api/jobs")
                jobs.raise_for_status()
                observations["final_jobs"] = jobs.json()
                checks["job_count_matches"] = len(jobs.json()["items"]) == (2 if number in {9, 11, 12} else 1)
    except Exception:
        observations["harness_error"] = traceback.format_exc()
        checks["harness_completed"] = False
    finally:
        phases["after"] = snapshot(workspace)

    all_events = [event for reference in runs.values() for event in recorded(root, reference["run_id"])]
    started = calls_with(all_events, "tool_started", MUTATIONS)
    succeeded = [item for item in calls_with(all_events, "tool_result", MUTATIONS) if item["result"]["ok"]]
    actual_mutations = len(started)
    repeated = max(0, actual_mutations - 1) if number in {11, 12} else 0
    checks["actual_mutation_start_and_success"] = len(started) == len(succeeded) == expected_mutations
    if approval is not None and expected_mutations == 1:
        expected_call = "resume_" + approval["id"]
        checks["resumed_original_approval_once"] = len(started) == 1 and started[0].get("action_id") == approval["id"]             and started[0].get("call_id") == expected_call and len(succeeded) == 1             and succeeded[0].get("call_id") == expected_call             and json.loads(started[0]["arguments_json"]) == approval["arguments"]
    expected_sha = digest(expected_files)
    different = sorted(path for path in phases["after"].keys() | expected_sha.keys()
                       if phases["after"].get(path) != expected_sha.get(path))
    checks["workspace_matches_expected"] = not different
    external_events = sum(event["type"] == "model_http_attempt" for event in all_events)
    checks["zero_model_http_attempts"] = external_events == model.transport_attempts == 0
    checks["no_extra_fixture_responses"] = model.extra_calls == 0
    observations.update({"actual_mutation_starts": actual_mutations, "actual_mutation_successes": len(succeeded),
                         "offline_model_responses": model.calls, "model_http_attempt_events": external_events,
                         "unexpected_files": different})
    dump(directory / "http.json", trace)
    dump(directory / "workspace-sha256.json", {**phases, "expected": expected_sha})
    item = {"id": number, "kind": kind, "name": name, "passed": all(checks.values()), "assertions": checks,
            "expected": {"initial_tool_receipts": expected_codes, "mutation_starts": expected_mutations,
                         "workspace_sha256": expected_sha},
            "observed": observations, "runs": runs, "approval_ids": [approval["id"]] if approval else [],
            "evidence_dir": directory.relative_to(REPO).as_posix(), "unexpected_writes": len(different),
            "repeated_executions": repeated, "external_requests": model.transport_attempts}
    dump(directory / "result.json", item)
    return item


async def main(selected: list[int] | None) -> None:
    """执行 selected 所选题号，None 为全部 API 题；生成独立时间批次和机器可读汇总。"""
    batch = OUTPUT / datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    cases = []
    for spec in BATCHES:
        number = spec[0]
        if selected is not None and number not in selected:
            continue
        result = await exercise(spec, batch / f"c{number:02d}")
        cases.append(result)
        print(f"{number:02d} " + ("PASS" if result["passed"] else "FAIL " +
              ", ".join(key for key, passed in result["assertions"].items() if not passed)), flush=True)
    external = sum(row["external_requests"] for row in cases)
    payload = {"batch": batch.relative_to(REPO).as_posix(), "cases": cases, "external_requests": external}
    dump(batch / "results.json", payload)
    if selected is None:
        dump(OUTPUT / "results.json", payload)
    if not all(item["passed"] for item in cases):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, nargs="+", choices=[item[0] for item in BATCHES],
                        help="只运行指定题号，保留独立诊断批次，不覆盖全批次汇总。")
    asyncio.run(main(parser.parse_args().cases))

