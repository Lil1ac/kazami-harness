"""用离线模型回执和真实 Web 审批页面验收三道全新文件治理任务。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import uvicorn

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runtime.runner import REQUEST_CONTEXT
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


ROOT = Path(__file__).resolve().parents[2]  # 项目源码及产物根目录。
OUTPUT = ROOT / "output/permission-governance-20261117/ui"  # 页面评测证据总目录。
SCREENSHOTS = ROOT / "output/playwright/permission-governance-20261117"  # 浏览器截图证据。
FINISH = "离线验收文件动作已完成。"  # 审批后由脚本模型产生的固定会话答复。


@dataclass(frozen=True)
class Case:
    """一份前端题目协议，包含冻结输入、页面文案及预期文件状态。"""

    number: int  # 与总评测题单一致的编号。
    name: str  # 表格与回执中的中文题名。
    target: str  # 临时仓库内被审阅的相对路径。
    initial: str | None  # 初始文件文本，None 表示不存在。
    proposed: str  # 创建或覆盖的完整文本，局部编辑时为新片段。
    decision: str  # 页面执行的 approve 或 reject 决定。
    old_fragment: str | None = None  # 局部编辑旧片段，None 表示整文件操作。
    deferred: str | None = None  # 同批第二个动作请求的目标，未设置时只申请一次。

    @property
    def prompt(self) -> str:
        """返回展示在页面并用于查找冻结题目的唯一提示文本。"""
        return f"工具治理验收 {self.number:02d} · {self.name}"

    @property
    def workspace_id(self) -> str:
        """返回真实 Web 工作区配置使用的固定 ID。"""
        return f"approval-case{self.number:02d}"

    @property
    def final(self) -> str | None:
        """返回验收期望的最终文件全文，None 表示路径仍应不存在。"""
        if self.decision == "reject":
            return self.initial
        if self.old_fragment is not None:
            assert self.initial is not None
            return self.initial.replace(self.old_fragment, self.proposed)
        return self.proposed


CASES = (
    Case(3, "同批申请发布说明和草稿", "docs/release-note.md", None,
         "发布说明 · 客服交接窗口已确认\n", "approve", deferred="docs/temporary-draft.md"),
    Case(4, "审阅维护记录的局部变更", "docs/maintenance-log.md",
         "维护记录 · 保留此标题\n交接时间：周三 11:00\n审批人：值班负责人\n",
         "交接时间：周四 15:00", "approve", old_fragment="交接时间：周三 11:00"),
    Case(8, "拒绝覆盖已复核草稿", "docs/reviewed-draft.md", "原已复核草稿 · 保持蓝色封面\n",
         "替换草稿 · 改用绿色封面\n", "reject"),
)  # 冻结的三道 UI 题，按总评测编号保存。


def save_json(path: Path, value: object) -> None:
    """用 UTF-8 写入 path 指定的证据，value 为可序列化对象，无返回值。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture_tool(name: str, **arguments: str | bool) -> ToolCall:
    """创建 name 对应的固定工具请求，arguments 原样交给宿主校验。"""
    return ToolCall(str(uuid4()), name, json.dumps(arguments, ensure_ascii=False))


class FixtureModel:
    """只生成固定工具请求，不做文件副作用或外部调用。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据当前题及真实读文件回执生成模型响应，tools 由宿主提供。"""
        manifest = REQUEST_CONTEXT.get().manifest
        calls = []
        if not manifest.permission_resume:
            case = next(case for case in CASES if case.prompt == manifest.task.prompt)
            if case.old_fragment:
                reads = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
                if reads:
                    receipt = reads[-1]
                    assert receipt["ok"] and receipt["data"]["path"] == case.target
                    calls.append(fixture_tool("edit_file", path=case.target, old_text=case.old_fragment,
                                              new_text=case.proposed, expected_sha256=receipt["data"]["sha256"]))
                else:
                    calls.append(fixture_tool("read_file", path=case.target))
            else:
                calls.append(fixture_tool("write_file", path=case.target, content=case.proposed,
                                          overwrite=case.initial is not None))
                if case.deferred:
                    calls.append(fixture_tool("write_file", path=case.deferred, content="草稿仅供下次单独审阅。\n"))
        message = {"role": "assistant", "content": None if calls else FINISH}
        if calls:
            message["tool_calls"] = [
                {"id": call.call_id, "type": "function",
                 "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls
            ]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
                             response_model="offline-governance",
                             usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


class OfflineTransport(httpx.AsyncBaseTransport):
    """阻止本地验收意外经过真实模型 HTTP 通道。"""

    def __init__(self, root: Path) -> None:
        """绑定批次 root 的越界计数文件，构造时不发请求。"""
        self.marker = root / "external-request.txt"  # 命中时保留固定标记，不保存请求内容。

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """拒绝任何进入真实模型通道的 request，标记后抛错，无正常返回。"""
        self.marker.write_text("unexpected model HTTP request\n", encoding="utf-8")
        raise AssertionError("零模型验收出现模型 HTTP 请求")


def serve(root: Path, frontend: Path, port: int) -> None:
    """绑定回环 port，服务 root 下的独立工作区及 frontend 的现有构建。"""
    workspaces = []
    for case in CASES:
        workspace = root / case.workspace_id / "project"
        workspaces.append(WebWorkspace(id=case.workspace_id, label=case.name, path=workspace))
    configuration = RunConfig(
        provider="custom-governance",
        custom_providers={"custom-governance": {
            "name": "本地审批验收", "base_url": "http://127.0.0.1:1/v1", "auth": "none",
            "default_model": "offline-governance",
        }},
        tools=["read_file", "write_file", "edit_file"],
    )
    web = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=frontend, workspaces=workspaces)
    app = create_app(web, configuration, root / "runs", model_client=FixtureModel(), transport=OfflineTransport(root))
    uvicorn.run(app, host="127.0.0.1", port=port, loop="asyncio", timeout_graceful_shutdown=3)


def hashes(root: Path) -> dict[str, str]:
    """按 root 内的文件相对路径返回完整 SHA-256 清单，包含所有普通文件。"""
    return {file.relative_to(root).as_posix(): sha256(file.read_bytes()).hexdigest()
            for file in sorted(root.rglob("*")) if file.is_file()}


def get(client: httpx.Client, path: str) -> dict:
    """通过真实 client GET 本地 path，返回状态为成功的 JSON 对象。"""
    receipt = client.get(path)
    receipt.raise_for_status()
    return receipt.json()


def wait_for(read, ready, label: str, limit: float = 20.0):
    """轮询 read 直到 ready 为真，返回最后结果；limit 秒后附 label 抛错。"""
    deadline = time.monotonic() + limit
    last = None
    while time.monotonic() < deadline:
        try:
            last = read()
            if ready(last):
                return last
        except httpx.TransportError:
            pass
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
        time.sleep(0.16)
    raise RuntimeError(f"等待{label}超时：{str(last)[:700]}")


class Browser:
    """通过 Playwright CLI 读取真实页面，不注入审批或 DOM 脚本。"""

    def __init__(self, cli: Path, root: Path, session: str) -> None:
        """绑定 cli 脚本、root 下的浏览器日志和独立 session 名称。"""
        self.command = ["node", str(cli), "-s=" + session]  # 复用已安装 CLI，避开 npm 下载。
        self.root = root  # CLI 与快照的进程工作目录。

    def do(self, *args: str) -> str:
        """执行 args 指定的 CLI 命令并返回文本；失败保留输出并抛错。"""
        result = subprocess.run([*self.command, *args], cwd=self.root, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=30)
        with (self.root / "browser.log").open("a", encoding="utf-8") as log:
            log.write(f"{args!r}\n{result.stdout}\n{result.stderr}\n")
        if result.returncode or "### Error" in result.stdout:
            raise RuntimeError(f"Playwright {args[0]}：{(result.stdout + result.stderr)[-1200:]}")
        return result.stdout

    def snapshot(self, path: Path) -> str:
        """将当前页面可访问性快照保存至 path 并返回完整文本。"""
        self.do("snapshot", f"--filename={path}")
        return path.read_text(encoding="utf-8")


def tool_events(events: list[dict], kind: str, name: str) -> list[dict]:
    """返回事件数组中与 kind、工具 name 一致的载荷，顺序保持不变。"""
    return [event["payload"] for event in events if event["type"] == kind and event["payload"].get("name") == name]


def execute_case(case: Case, root: Path, client: httpx.Client, browser: Browser, screens: Path) -> dict:
    """运行单题真实页面决定并返回总报告行；root 和 screens 保存逐阶段证据。"""
    trial = root / case.workspace_id
    workspace = trial / "project"
    before = hashes(workspace)
    response = client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {
        "workspace_id": case.workspace_id, "prompt": case.prompt, "config": {"permission_mode": "strict"},
    }})
    response.raise_for_status()
    initial_job = response.json()
    source = initial_job["run_id"]
    source_run = wait_for(lambda: get(client, f"/api/runs/{source}"),
                          lambda row: row["result"]["stop_reason"] == "action_approval" and not row["active"], "原动作待审批")
    action = source_run["result"]["permissions"]["pending"]
    action_id = action["id"]
    pending_hashes = hashes(workspace)
    initial_events = get(client, f"/api/runs/{source}/events")["items"]
    save_json(trial / "approval-before.json", get(client, f"/api/runs/{source}/permissions"))
    save_json(trial / "source-events.json", initial_events)
    browser.do("goto", str(client.base_url.join(f"runs/{source}")))
    ref = r"(\w*e\d+)"
    review_button = re.compile(rf'- button "审阅动作" \[ref={ref}\](?! \[disabled\])')
    conversation_page = wait_for(lambda: browser.snapshot(trial / "page-conversation.md"), review_button.search, "会话审阅入口", 35)
    browser.do("click", review_button.search(conversation_page).group(1))
    label = "批准本次" if case.decision == "approve" else r"拒\s*绝"
    button = re.compile(rf'- button "{label}" \[ref={ref}\](?! \[disabled\])')
    snapshot = wait_for(lambda: browser.snapshot(trial / "page-before.md"), button.search, "审批按钮可用", 35)
    marker = case.old_fragment or (case.initial or "").strip()
    approval_image = screens / f"{case.number:02d}-before.png"
    browser.do("screenshot", f"--filename={approval_image}")
    browser.do("click", button.search(snapshot).group(1))
    conversation = wait_for(lambda: get(client, f"/api/runs/{source}/conversation"),
                            lambda row: row.get("latest_run_id") != source and row.get("latest_run_id"), "作业接续")
    resume = conversation["latest_run_id"]
    outcome = wait_for(lambda: get(client, f"/api/runs/{resume}"),
                       lambda row: not row["active"] and row["result"]["execution_status"] in {"completed", "cancelled", "failed"}, "接续结束")
    final_message = FINISH if case.decision == "approve" else "已拒绝本次动作。"
    final_page = wait_for(lambda: browser.snapshot(trial / "page-after.md"),
                          lambda view: final_message in view, "页面接续结果", 35)
    final_image = screens / f"{case.number:02d}-after.png"
    browser.do("screenshot", f"--filename={final_image}")
    browser_requests = browser.do("requests", "--filter=/api/jobs")
    (trial / "page-requests.log").write_text(browser_requests, encoding="utf-8")
    events = get(client, f"/api/runs/{resume}/events")["items"]
    actions = get(client, f"/api/runs/{resume}/permissions")
    jobs = get(client, "/api/jobs")["items"]
    save_json(trial / "resume-events.json", events)
    save_json(trial / "approval-after.json", actions)
    save_json(trial / "jobs.json", [row for row in jobs if row.get("run_id") in {source, resume}])
    final_hashes = hashes(workspace)
    expected_final = dict(before)
    if case.final is not None:
        expected_final[case.target] = sha256(case.final.encode("utf-8")).hexdigest()
    action_name = "edit_file" if case.old_fragment else "write_file"
    started = tool_events(initial_events + events, "tool_started", action_name)
    success = [px for px in tool_events(events, "tool_result", action_name) if px["result"]["ok"]]
    expected_count = int(case.decision == "approve")
    terminal_action = next(item for item in actions["items"] if item["id"] == action_id)
    first_call = action.get("call_id")
    resume_links = [event["payload"] for event in events if event["type"] == "permission_resuming"]
    terminal_state = "completed" if expected_count else "cancelled"
    action_state = "executed" if expected_count else "rejected"
    assertions = {
        "page_shows_target_and_reviewed_text": Path(case.target).name in snapshot and case.proposed.strip() in snapshot and marker in snapshot,
        "no_write_before_decision": before == pending_hashes,
        "page_shows_final_decision_result": final_message in final_page,
        "final_files_match_expected": expected_final == final_hashes,
        "expected_execution_count": len(started) == expected_count and len(success) == expected_count,
        "decision_persisted_and_run_state_correct": terminal_action["status"] == action_state
        and terminal_action["decision"] == case.decision and outcome["result"]["execution_status"] == terminal_state,
        "no_external_model_requests": not (root / "external-request.txt").exists(),
    }
    if expected_count:
        assertions["resumed_exact_approved_action"] = resume_links == [{"action_id": action_id, "source_run_id": source}]
    else:
        assertions["rejection_did_not_call_model"] = outcome["result"]["model_turns"] == 0
    if case.deferred:
        codes = [(px["result"].get("error") or {}).get("code") for px in tool_events(initial_events, "tool_result", action_name)]
        assertions["second_tool_skipped_and_absent"] = codes == ["approval_required", "skipped_action_approval"] and case.deferred not in final_hashes
    if case.old_fragment:
        assertions["approved_edit_bound_to_reviewed_version"] = action["arguments"]["expected_sha256"] == before.get(case.target)
    row = {"id": case.number, "kind": "normal" if expected_count else "restricted", "name": case.name,
           "passed": all(assertions.values()), "assertions": assertions,
           "expected": {"files": expected_final, "execution_count": expected_count, "terminal_action": action_state},
           "observed": {"files": final_hashes, "execution_count": len(started), "terminal_action": terminal_action["status"],
                        "source_call_id": first_call, "source_state": source_run["result"]["execution_status"],
                        "resume_state": outcome["result"]["execution_status"]},
           "before": before, "pending": pending_hashes, "after": final_hashes, "runs": {"source": source, "resume": resume},
           "source_run_id": source, "resume_run_id": resume, "action_id": action_id, "approval_ids": [action_id],
           "evidence_dir": str(trial), "screenshots": [str(approval_image), str(final_image)],
           "unexpected_writes": len({key for key in expected_final.keys() | final_hashes.keys()
                                     if expected_final.get(key) != final_hashes.get(key)}),
           "repeated_executions": max(0, len(started) - max(expected_count, 1))}
    save_json(trial / "result.json", row)
    return row


def run(frontend: Path, cli: Path) -> bool:
    """在独立批次启动 frontend 和 cli，落盘三题结果后返回整体是否通过。"""
    tag = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    root = OUTPUT / tag
    screens = SCREENSHOTS / tag
    root.mkdir(parents=True)
    screens.mkdir(parents=True)
    for case in CASES:
        target = root / case.workspace_id / "project" / case.target
        target.parent.mkdir(parents=True)
        if case.initial is not None:
            target.write_bytes(case.initial.encode("utf-8"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    browser = Browser(cli, root, "governance-" + tag)
    address = f"http://127.0.0.1:{port}/"
    rows = []
    print(f"页面证据目录 {root}", flush=True)
    with (root / "service.log").open("wb") as log:
        service = subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "--serve",
                                    "--data-dir", str(root), "--frontend", str(frontend), "--port", str(port)],
                                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=address, trust_env=False, timeout=5.0) as client:
                wait_for(lambda: get(client, "/api/options"), bool, "本地服务启动")
                browser.do("open", address + "runs", "--browser=chrome")
                browser.do("resize", "1440", "1024")
                for case in CASES:
                    print(f"开始页面场景 {case.number:02d}", flush=True)
                    try:
                        row = execute_case(case, root, client, browser, screens)
                    except Exception as error:
                        row = {"id": case.number, "kind": "restricted" if case.decision == "reject" else "normal",
                               "name": case.name, "passed": False, "assertions": {"completed": False},
                               "error": str(error), "evidence_dir": str(root / case.workspace_id)}
                        save_json(root / case.workspace_id / "result.json", row)
                    rows.append(row)
                    print(f"页面场景 {case.number:02d} {'通过' if row['passed'] else '未通过'} {row.get('error', '')}", flush=True)
        finally:
            try:
                browser.do("close")
            finally:
                service.terminate()
                service.wait(timeout=10)
    report = {"cases": rows, "external_requests": int((root / "external-request.txt").exists()), "evidence_dir": str(root)}
    save_json(root / "report.json", report)
    shutil.copyfile(root / "report.json", OUTPUT / "latest.json")
    return len(rows) == len(CASES) and all(row["passed"] for row in rows) and report["external_requests"] == 0


def main() -> int:
    """分支到本地服务模式或验收模式，返回进程状态码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, default=OUTPUT / "frontend")
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        serve(args.data_dir.resolve(), args.frontend.resolve(), args.port)
        return 0
    if not args.cli or not args.cli.is_file() or not (args.frontend / "index.html").is_file():
        parser.error("需要已安装 Playwright CLI 脚本和本地前端构建")
    return 0 if run(args.frontend.resolve(), args.cli.resolve()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
