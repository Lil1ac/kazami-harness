"""冻结和执行工具治理扩样，不访问外部模型。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
from uuid import uuid4

import httpx
import uvicorn

from agent_harness.permissions.models import PermissionPolicy
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace

from cases import CASES, Case


REPO = Path(__file__).resolve().parents[2]  # 证据文件的相对路径起点。
OUTPUT = REPO / "output/permission-governance-expanded-20261117"  # 冻结清单、预检和正式批次互相独立。
SCREENS = REPO / "output/playwright/permission-governance-expanded-20261117"  # 真实浏览器截图。
ORIGINAL = REPO / "evals/permission-governance-20261117"  # 可复用的 Runner 与浏览器夹具。
MUTATIONS = {"write_file", "edit_file"}  # 批次内具有工作区写入副作用的工具。
TOOLS = {"read_file", *MUTATIONS}  # 本批首轮回执应只使用这些文件工具。
CATEGORIES = ("normal", "parameters", "boundaries", "approval", "versions", "retries")  # 每类固定六案。
BROWSER_CHANNEL = "msedge"  # 本机启动预检已验证的 Playwright 浏览器通道。
sys.path.insert(0, str(ORIGINAL))
import api_check as api
import ui_eval as ui


def fixture_files(workspace: Path, files: dict[str, str | None]) -> None:
    """把 files 映射安装至 workspace，None 表示删除现有文件；不接收工作区外路径。"""
    for name, content in files.items():
        path = workspace / name
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("夹具文件超出工作区")
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            path.unlink()
        else:
            # 替换创建新文件对象，同正文版本题仍会产生可核验的身份变化。
            temporary = path.with_name(f".{path.name}.{uuid4().hex}")
            temporary.write_bytes(content.encode("utf-8"))
            os.replace(temporary, path)


def freeze() -> str:
    """冻结 36 条场景及夹具版本，返回供预检和正式报告核对的 SHA-256。"""
    if [case.number for case in CASES] != list(range(1, 37)):
        raise ValueError("题目必须连续编号为 1–36")
    if Counter(case.category for case in CASES) != dict.fromkeys(CATEGORIES, 6):
        raise ValueError("每类必须恰好 6 个场景")
    if sum(case.browser is not None for case in CASES) != 6:
        raise ValueError("须选定 6 个正式浏览器场景")
    if len({case.title for case in CASES}) != 36:
        raise ValueError("同批题名应唯一")
    paths = [Path(__file__), Path(__file__).with_name("cases.py"),
             ORIGINAL / "api_check.py", ORIGINAL / "ui_eval.py"]
    document = {"planned": 36, "categories": dict.fromkeys(CATEGORIES, 6), "browser_channel": BROWSER_CHANNEL,
                "formal_browser_cases": [case.number for case in CASES if case.browser],
                "cases": [asdict(case) for case in CASES],
                "fixtures": {path.relative_to(REPO).as_posix(): sha256(path.read_bytes()).hexdigest() for path in paths}}
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    digest = sha256(encoded).hexdigest()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "manifest.json"
    if path.exists() and path.read_bytes() != encoded:
        raise RuntimeError("冻结内容发生变化；须保留原清单另存并重新预检")
    path.write_bytes(encoded)
    (OUTPUT / "manifest.sha256").write_text(digest + "\n", encoding="utf-8")
    return digest


def offline_config(policy: dict) -> RunConfig:
    """创建安全的离线宿主配置，policy 是单个场景的真实权限覆盖。"""
    return RunConfig(provider="custom-fixture", custom_providers={"custom-fixture": {
        "name": "固定离线响应", "base_url": "http://127.0.0.1:1/v1", "auth": "none", "default_model": "fixture-model"}},
        tools=sorted(TOOLS), permission_policy=PermissionPolicy(**policy), auto_repair=False,
        memory={"use": False, "capture": False}, context={"compression": "off"})


async def decisions(case: Case, client: httpx.AsyncClient, other: list[httpx.AsyncClient], first_run: str,
                    approval: dict, trace: list[dict], runs: dict, root: Path) -> tuple[str | None, dict | None, dict, dict]:
    """经真实 API 提交一个或多个决定，返回成功选择、接续 Run、观察及验收项。

    case 定义决策模式；client 和 other 为独立客户端；first_run / approval 指明原动作。
    trace/runs 供保存证据，root 为实际事件目录。
    """
    choice = case.decision
    body = api.decision(first_run, approval, choice)
    pattern = case.approval_after
    checks = {}
    if pattern == "expired":
        await asyncio.sleep(max(0, approval["expires_at"] - time.time()) + .08)
        checks["submitted_after_expiry"] = time.time() >= approval["expires_at"]
    if pattern == "duplicate":
        responses = await asyncio.gather(*(api.post(sender, body, trace) for sender in [client, *other]))
        selected = responses[0]
    elif pattern in {"race_reject", "race_cancel"}:
        alternate = pattern.removeprefix("race_")
        alternate_body = api.decision(first_run, approval, alternate)
        # 两种提交顺序都验收，以实际接受结果决定预期文件状态。
        if alternate == "cancel":
            submitted = [(other[0], alternate, alternate_body), (client, choice, body)]
        else:
            submitted = [(client, choice, body), (other[0], alternate, alternate_body)]
        responses = await asyncio.gather(*(api.post(sender, payload, trace) for sender, _, payload in submitted))
        accepted = [(intent, answer) for (_, intent, _), answer in zip(submitted, responses) if answer[0] == 202]
        checks["single_race_winner"] = len(accepted) == 1 and sorted(x[0] for x in responses) == [202, 409]
        choice, selected = accepted[0] if accepted else (None, responses[0])
    else:
        selected = await api.post(client, body, trace)
        responses = [selected]
    after = await api.wait_run(client, selected[1], runs, "decision", root) if selected[0] == 202 else None
    if pattern == "sequence":
        responses.extend([await api.post(other[0], body, trace), await api.post(other[1], body, trace)])
    elif pattern == "conflict":
        changed = deepcopy(body)
        changed["followup"]["action_decision"]["action"] = "reject"
        responses.append(await api.post(other[0], changed, trace))
    elif pattern == "stale_parent":
        responses.append(await api.post(other[0], api.decision(first_run, approval, choice), trace))
    statuses = [status for status, _ in responses]
    errors = [(row.get("error") or {}).get("code") for status, row in responses if status != 202]
    state = after["result"] if after else None
    if pattern == "expired":
        checks["expired_no_resume"] = statuses == [409] and errors == ["permission_expired"] and state is None
    elif pattern == "stale":
        checks["stale_no_replay"] = statuses == [202] and state is not None and (
            state["stop_reason"] == "permission_stale" and state["model_turns"] == 0)
    else:
        checks["decision_terminal_state"] = state is not None and (
            state["execution_status"] == ("completed" if choice == "approve" else "cancelled"))
        if choice in {"reject", "cancel"}:
            checks["rejection_no_model"] = state is not None and state["model_turns"] == 0
        if pattern in {"duplicate", "sequence"}:
            identities = {(row.get("id"), row.get("run_id")) for _, row in responses}
            checks["retry_shares_original_job_and_run"] = statuses == [202, 202, 202] and len(identities) == 1
        elif pattern in {"conflict", "stale_parent"}:
            expected = "request_conflict" if pattern == "conflict" else "session_stale_parent"
            checks["replay_refused"] = statuses == [202, 409] and errors == [expected]
        elif pattern in {"race_reject", "race_cancel"}:
            checks["loser_has_conflict_receipt"] = len(errors) == 1 and errors[0] in {
                "permission_stale", "permission_decided", "session_stale_parent", "session_busy"}
    accepted_choice = choice if selected[0] == 202 else None
    return accepted_choice, after, {"decision_statuses": statuses, "decision_errors": errors,
                                    "decision_winner": accepted_choice,
                                    "continuation_stop_reason": state["stop_reason"] if state else None}, checks


async def exercise(case: Case, directory: Path) -> dict:
    """通过真实 ASGI 链路执行 case，在 directory 落盘并返回逐案证据。"""
    workspace = directory / "workspace"
    root = directory / "records"
    workspace.mkdir(parents=True)
    fixture_files(workspace, case.files)
    configuration = offline_config(case.policy)
    web = WebConfig(project_dir=directory, worktrees_root=directory / "trees", frontend_dist=directory / "dist",
                    workspaces=[WebWorkspace(id="fixture", label=case.title, path=workspace)])
    model = api.FixedModel(case.turns, end_expected=case.approval_after not in {"stale", "expired"})
    app = create_app(web, configuration, root, model_client=model, transport=httpx.MockTransport(model.block_transport))
    runs, trace, checks, observed = {}, [], {}, {}
    snapshots = {"before": api.snapshot(workspace)}
    expected = dict(case.files)
    writes = case.writes
    approval = None
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with (httpx.AsyncClient(transport=transport, base_url="http://localhost") as client,
                        httpx.AsyncClient(transport=transport, base_url="http://localhost") as second,
                        httpx.AsyncClient(transport=transport, base_url="http://localhost") as third):
                status, job = await api.post(client, {"request_id": str(uuid4()), "create": {
                    "workspace_id": "fixture", "prompt": case.title, "config": {"permission_mode": case.mode}}}, trace)
                if status != 202:
                    raise RuntimeError(f"初始作业未被接纳，HTTP {status}")
                first = await api.wait_run(client, job, runs, "initial", root)
                first_result = first["result"]
                events = api.recorded(root, first["run_id"])
                receipts = api.calls_with(events, "tool_result", TOOLS)
                codes = ["ok" if row["result"]["ok"] else row["result"]["error"]["code"] for row in receipts]
                observed["initial_tool_receipts"] = codes
                checks["initial_tool_receipts"] = codes == case.receipts
                checks["initial_state"] = first_result["execution_status"] == (
                    "waiting_input" if case.decision else "completed")
                for index, text in case.readbacks.items():
                    checks[f"readback_{index}"] = index < len(receipts) and (
                        (receipts[index]["result"].get("data") or {}).get("content") == text)
                if case.decision:
                    snapshots["waiting"] = api.snapshot(workspace)
                    checks["no_write_before_decision"] = snapshots["waiting"] == snapshots["before"]
                    approval = first_result["permissions"]["pending"]
                    actions = [event["payload"]["action"] for event in events if event["type"] == "permission_requested"]
                    checks["single_audited_approval"] = len(actions) == 1 and actions[0]["id"] == approval["id"]
                    fixture_files(workspace, case.fixture_edit)
                    for name, text in case.fixture_edit.items():
                        if text is None:
                            expected.pop(name)
                        else:
                            expected[name] = text
                    if case.fixture_edit:
                        snapshots["after_independent_edit"] = api.snapshot(workspace)
                        checks["independent_edit_has_expected_effect"] = snapshots["after_independent_edit"] == api.digest(expected)
                    winner, _, decision_view, assertions = await decisions(
                        case, client, [second, third], first["run_id"], approval, trace, runs, root)
                    observed.update(decision_view)
                    checks.update(assertions)
                    writes = case.writes if winner == "approve" else {}
                    actions = (await client.get(f"/api/runs/{first['run_id']}/permissions")).json()
                    observed["permissions"] = actions
                    checks["one_persisted_action"] = actions["total"] == 1 and actions["items"][0]["id"] == approval["id"]
                else:
                    checks["no_approval"] = not any(event["type"] == "permission_requested" for event in events)
                jobs = (await client.get("/api/jobs")).json()["items"]
                observed["jobs"] = [{"id": item["id"], "run_id": item.get("run_id"), "status": item["status"]} for item in jobs]
                checks["job_count"] = len(jobs) == len(runs)
    except Exception:
        observed["harness_error"] = traceback.format_exc()
        checks["exercised"] = False
    expected.update(writes)
    snapshots["after"] = api.snapshot(workspace)
    expected_sha = api.digest(expected)
    different = sorted(name for name in snapshots["after"].keys() | expected_sha.keys()
                       if snapshots["after"].get(name) != expected_sha.get(name))
    events = [event for info in runs.values() for event in api.recorded(root, info["run_id"])]
    starts = api.calls_with(events, "tool_started", MUTATIONS)
    successes = [row for row in api.calls_with(events, "tool_result", MUTATIONS) if row["result"]["ok"]]
    checks["workspace_hashes"] = not different
    checks["write_counts"] = len(starts) == len(successes) == len(writes)
    if approval and writes:
        checks["executed_bound_approval"] = len(starts) == 1 and starts[0].get("action_id") == approval["id"] and (
            json.loads(starts[0]["arguments_json"]) == approval["arguments"])
    external = sum(event["type"] == "model_http_attempt" for event in events)
    checks["zero_model_requests"] = external == model.transport_attempts == 0
    checks["no_unplanned_script_response"] = model.extra_calls == 0
    observed.update({"write_starts": len(starts), "write_successes": len(successes), "unexpected_files": different,
                     "model_http_attempt_events": external, "offline_responses": model.calls})
    api.dump(directory / "http.json", trace)
    api.dump(directory / "workspace-sha256.json", {**snapshots, "expected": expected_sha})
    result = {"id": case.number, "category": case.category, "name": case.title, "channel": "api",
              "passed": all(checks.values()), "assertions": checks, "observed": observed,
              "runs": runs, "approval_ids": [approval["id"]] if approval else [],
              "unexpected_writes": len(different), "extra_executions": max(0, len(starts) - len(writes)),
              "external_requests": model.transport_attempts,
              "evidence_dir": directory.relative_to(REPO).as_posix()}
    api.dump(directory / "result.json", result)
    return result


def browser_cases() -> dict[int, object]:
    """将冻结前端场景适配到已验证的审批页面夹具。"""
    return {case.number: ui.Case(case.number, case.title, case.browser.target, case.files.get(case.browser.target),
                                 case.browser.proposed, case.decision, case.browser.old_fragment, case.browser.deferred)
            for case in CASES if case.browser is not None}


def serve_ui(directory: Path, frontend: Path, port: int) -> None:
    """以 directory 的冻结工作区和 frontend 构建启动回环服务，port 为本机端口。"""
    ui.CASES = tuple(browser_cases().values())
    workspaces = [WebWorkspace(id=case.workspace_id, label=case.name, path=directory / case.workspace_id / "project")
                  for case in ui.CASES]
    web = WebConfig(project_dir=directory, worktrees_root=directory / "trees", frontend_dist=frontend,
                    workspaces=workspaces)
    app = create_app(web, offline_config({}), directory / "runs", model_client=ui.FixtureModel(),
                     transport=ui.OfflineTransport(directory))
    uvicorn.run(app, host="127.0.0.1", port=port, loop="asyncio", timeout_graceful_shutdown=3)


def verify_page_diff(case: object, trial: Path, row: dict) -> None:
    """核对 trial 中的 case 页面差异，多行内容按实际渲染的每一行验证，更新 row 的断言。"""
    original = (case.initial or "").strip()
    proposed = case.proposed.strip()
    if case.old_fragment is None and ("\n" in original or "\n" in proposed):
        snapshot = (trial / "page-before.md").read_text(encoding="utf-8")
        visible = all(line in snapshot for line in {*original.splitlines(), *proposed.splitlines()} if line)
        row["assertions"]["page_shows_target_and_reviewed_text"] = Path(case.target).name in snapshot and visible
        row["passed"] = all(row["assertions"].values())


def run_pages(directory: Path, frontend: Path, cli: Path) -> list[dict]:
    """对 directory 下六个冻结场景提交真实浏览器决定，返回含证据目录的结果。"""
    screen_root = SCREENS / directory.parent.name
    directory.mkdir()
    screen_root.mkdir(parents=True)
    chosen = browser_cases()
    for case in CASES:
        if case.number in chosen:
            project = directory / chosen[case.number].workspace_id / "project"
            project.mkdir(parents=True)
            fixture_files(project, case.files)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    browser = ui.Browser(cli, directory, "governance-expanded-" + directory.parent.name)
    results = []
    with (directory / "service.log").open("wb") as log:
        service = subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                    "--serve", "--directory", str(directory), "--frontend", str(frontend),
                                    "--port", str(port)], cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/", trust_env=False, timeout=5) as client:
                ui.wait_for(lambda: ui.get(client, "/api/options"), bool, "本机审批服务")
                browser.do("open", str(client.base_url.join("runs")), f"--browser={BROWSER_CHANNEL}",
                           "--persistent", f"--profile={directory / 'edge-profile'}")
                browser.do("resize", "1440", "1024")
                for number, case in chosen.items():
                    trial = directory / case.workspace_id
                    try:
                        row = ui.execute_case(case, directory, client, browser, screen_root)
                        verify_page_diff(case, trial, row)
                        row["extra_executions"] = max(
                            0, row["observed"]["execution_count"] - row["expected"]["execution_count"])
                    except Exception:
                        row = {"id": number, "name": case.name, "passed": False, "error": traceback.format_exc(),
                               "evidence_dir": str(trial), "unexpected_writes": None, "repeated_executions": None}
                    row.update(category="approval", channel="browser",
                               evidence_dir=trial.relative_to(REPO).as_posix(),
                               extra_executions=row.get("extra_executions"),
                               external_requests=int((directory / "external-request.txt").exists()))
                    api.dump(trial / "result.json", row)
                    results.append(row)
                    show(row)
        finally:
            try:
                browser.do("close")
            finally:
                service.terminate()
                service.wait(timeout=10)
    return results


def show(row: dict) -> None:
    """简要打印逐案 row 的验收状态，详细信息仍从证据文件追溯。"""
    failure = ", ".join(name for name, passed in row.get("assertions", {}).items() if not passed)
    print(f'{row["id"]:02d} {row["channel"]} ' + ("PASS" if row["passed"] else f"FAIL {failure}"), flush=True)


def rollup(rows: list[dict], phase: str, directory: Path, digest: str) -> dict:
    """汇总 rows 在 phase 的分组指标，目录和 digest 用于复核冻结协议。"""
    rows.sort(key=lambda row: row["id"])
    by_category = {category: {"planned": 6, "checked": sum(row["category"] == category for row in rows),
                              "passed": sum(row["category"] == category and row["passed"] for row in rows)}
                   for category in CATEGORIES}
    return {"phase": phase, "manifest_sha256": digest, "planned": 36, "checked": len(rows),
            "passed": sum(row["passed"] for row in rows), "categories": by_category,
            "browser_checked": sum(row["channel"] == "browser" for row in rows),
            "browser_passed": sum(row["channel"] == "browser" and row["passed"] for row in rows),
            "unexpected_writes": sum(row["unexpected_writes"] or 0 for row in rows),
            "extra_executions": sum(row["extra_executions"] or 0 for row in rows),
            "external_requests": sum(row["external_requests"] for row in rows),
            "unknown_file_effect_cases": sum(row["unexpected_writes"] is None for row in rows),
            "restricted_or_stale_passed": sum(row["passed"] and (row["category"] in {"parameters", "boundaries", "versions"}
                                        or CASES[row["id"] - 1].decision == "reject") for row in rows),
            "restricted_or_stale_planned": 21,
            "evidence_dir": directory.relative_to(REPO).as_posix(), "cases": rows}


def run(phase: str, frontend: Path, cli: Path) -> int:
    """执行指定 phase，核对浏览器构建和 CLI，返回命令状态。"""
    digest = freeze()
    if not (frontend / "index.html").is_file() or not cli.is_file():
        raise RuntimeError("缺少已构建前端或本地 Playwright CLI")
    frontend_hashes = api.snapshot(frontend)
    cli_version = subprocess.check_output(["node", str(cli), "--version"], cwd=REPO, encoding="utf-8").strip()
    if phase == "formal":
        preflight = json.loads((OUTPUT / "preflight.json").read_text(encoding="utf-8"))
        if preflight["manifest_sha256"] != digest or preflight["passed"] != 36 or preflight["external_requests"] != 0:
            raise RuntimeError("对应的冻结题单还未通过完整零模型预检")
        if preflight["frontend_sha256"] != frontend_hashes:
            raise RuntimeError("正式浏览器必须使用已预检的前端构建")
    tag = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    directory = OUTPUT / phase / tag
    directory.mkdir(parents=True)

    async def run_api() -> list[dict]:
        """在隔离目录顺序运行当前阶段的 API 场景，返回每案结果。"""
        rows = []
        for case in CASES:
            if phase == "preflight" or case.browser is None:
                result = await exercise(case, directory / f"c{case.number:02d}")
                rows.append(result)
                show(result)
        return rows

    rows = asyncio.run(run_api())
    if phase == "formal":
        rows.extend(run_pages(directory / "browser", frontend, cli))
    result = rollup(rows, phase, directory, digest)
    result["playwright_cli"] = cli_version
    result["frontend_sha256"] = frontend_hashes
    api.dump(directory / "report.json", result)
    api.dump(OUTPUT / f"{phase}.json", result)
    print(f'{phase}: {result["passed"]}/{result["planned"]}，浏览器 {result["browser_passed"]}/{result["browser_checked"]}', flush=True)
    return int(result["checked"] != 36 or result["passed"] != 36 or result["external_requests"] != 0)


def main() -> int:
    """从 CLI 解析预检、正式流程或回环服务参数并返回状态码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("preflight", "formal"))
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--frontend", required=True, type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        serve_ui(args.directory, args.frontend, args.port)
        return 0
    if args.phase is None or args.cli is None:
        parser.error("运行验收需要 --phase 和 --cli")
    return run(args.phase, args.frontend.resolve(), args.cli.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
