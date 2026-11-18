"""在测试目录启动真实 HTTP 和 Runner，以可重复故障点验证 Python 后台强制退出。"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading
import time

import uvicorn

from agent_harness.execution.scheduling import Coordinator
from agent_harness.permissions.models import PermissionPolicy
from agent_harness.projects import ProjectPreset
from agent_harness.projects.integration_files import git
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunLimits
from agent_harness.storage.runs import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from tests.helpers.models import configured_run


BEFORE = b"status = 'old'\n"  # 固定基线，验收只核对工具实际写入的内容。
AFTER = b"status = 'recovered'\n"  # 恢复后需要保留的内容。
FIXTURE_PROMPT = "修正 app.txt 并独立验收"  # 真实页面提交的验收需求。


def append(root: Path, name: str, data: dict) -> None:
    """把 data 持久追加至 root 内的测试计数文件 name。"""
    with (root / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def prepare(root: Path) -> tuple[Path, list[str]]:
    """准备 root 的 Git 基线与独立验收命令。重启沿用已有项目与进程日志。"""
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").exists():
        (project / "app.txt").write_bytes(BEFORE)
        git(project, "init", "-q")
        git(project, "config", "core.autocrlf", "false")
        git(project, "add", "app.txt")
        git(project, "-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "baseline")
    check = root / "verify.py"
    check.write_text(
        'import os, time\nfrom pathlib import Path\n'
        'root = Path(__file__).parent\n'
        'with (root / "verifier-count.txt").open("a") as out:\n    out.write(str(os.getpid()) + "\\n")\n'
        'if (root / "arm.txt").read_text().strip() == "verification":\n'
        '    (root / "verifier-started.txt").write_text(str(os.getpid()))\n'
        '    until = time.monotonic() + 30\n'
        '    while not (root / "release-verifier").exists() and time.monotonic() < until: time.sleep(.05)\n'
        'target = "new.txt" if (root / "new-file-mode").exists() else "app.txt"\n'
        'assert Path(target).read_bytes() == ' + repr(AFTER) + '\n'
        'print("Independent verification passed")\n'
        'if (root / "verifier-started.txt").exists(): (root / "verifier-exited").touch()\n',
        encoding="utf-8")
    if not (root / "arm.txt").exists():
        (root / "arm.txt").write_text("", encoding="utf-8")
    argv = [sys.executable, str(check)]
    return project, argv


class CrashModel:
    """测试模型依据已提交的工具历史继续，真实文件与验收交给 Runner。"""

    def __init__(self, root: Path) -> None:
        """绑定 root 内独立的模型调用计数及故障开关。"""
        self.root = root  # 每次请求的计数跨 Python 进程保留。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """生成确定性的文件编辑和完成答复，落盘记录实际调用和接续看到的回执。"""
        receipts = [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]
        recovering = any("宿主接续说明" in str(item.get("content", "")) for item in messages)
        append(self.root, "model.jsonl", {"pid": os.getpid(), "recovering": recovering, "receipts": receipts})
        fault(self.root, "model")
        calls = []
        wrote = any(item.get("ok") and (item.get("data") or {}).get("bytes_written") for item in receipts)
        if not receipts or recovering and not wrote and any(
            item["error"]["code"] == "recovery_write_not_applied" for item in receipts if item.get("error")
        ):
            name = "edit-resumed" if recovering else "edit-once"
            if (self.root / "new-file-mode").exists():
                calls.append(ToolCall(name, "write_file", json.dumps({"path": "new.txt", "content": AFTER.decode()})))
            else:
                calls.append(ToolCall(name, "edit_file", json.dumps({"path": "app.txt", "old_text": "old", "new_text": "recovered",
                                                                      "expected_sha256": sha256(BEFORE).hexdigest()})))
            if not recovering:
                calls.append(ToolCall("tail", "read_file", json.dumps({"path": "app.txt"})))
        message = {"role": "assistant", "content": "恢复后代码已通过独立验收。" if not calls else None}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name,
                                      "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message, calls, "tool_calls" if calls else "stop", "offline-recovery",
                             usage={"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15})


def fault(root: Path, phase: str, **facts) -> None:
    """在测试指定 phase 保存进程身份与事实，然后等待测试控制器从进程外终止。"""
    if (root / "arm.txt").read_text(encoding="utf-8").strip() != phase:
        return
    path = root / "fault.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"phase": phase, "pid": os.getpid(), **facts}), encoding="utf-8")
    os.replace(temporary, path)
    threading.Event().wait(60)
    raise RuntimeError("测试控制器没有在已登记故障点终止后台")


def install_faults(root: Path) -> None:
    """只在显式夹具进程安装故障窗口，包装真实文件提交和日志写入。"""
    replace = os.replace
    link = os.link
    emit = RunStore._append
    acquire = Coordinator.acquire

    def observe_replace(source, target, *args, **kwargs) -> None:
        """完成真实替换后记录目标文件及对象，测试可停在工具回执前。"""
        replace(source, target, *args, **kwargs)
        source_path, target_path = Path(source), Path(target)
        if source_path.name.startswith(".harness-write-") and target_path.name == "app.txt" and target_path.is_relative_to(root):
            info = target_path.stat()
            append(root, "effects.jsonl", {"pid": os.getpid(), "target": str(target_path), "inode": info.st_ino})
            fault(root, "applied", workspace=str(target_path.parent), inode=info.st_ino)

    def observe_link(source, target, *args, **kwargs) -> None:
        """在真实硬链接提交后留下证据，覆盖文件已经创建但暂存名称仍存在的故障窗口。"""
        link(source, target, *args, **kwargs)
        source_path, target_path = Path(source), Path(target)
        if source_path.name.startswith(".harness-write-") and target_path.name == "new.txt" and target_path.is_relative_to(root):
            info = target_path.stat()
            append(root, "effects.jsonl", {"pid": os.getpid(), "target": str(target_path), "inode": info.st_ino})
            fault(root, "created", workspace=str(target_path.parent), inode=info.st_ino)

    def observe_event(store, event_type, payload):
        """等待真实日志事件提交后触发配置的故障，返回原事件。"""
        event = emit(store, event_type, payload)
        phase = {"file_write_intent": "intent", "tool_result": "receipt", "run_finished": "terminal"}.get(event_type)
        if phase is not None:
            fault(root, phase, run_id=str(event.run_id), seq=event.seq)
        return event

    def observe_claim(coordinator, owner, scope, session, run_id, job=None, **kwargs) -> None:
        """在资源已转移、准备尚未开始时登记第二次中断点。"""
        acquire(coordinator, owner, scope, session, run_id, job, **kwargs)
        if job is not None and job.get("recovery_phase") == "claimed":
            fault(root, "claimed", job_id=owner, run_id=run_id)

    os.replace = observe_replace
    os.link = observe_link
    RunStore._append = observe_event
    Coordinator.acquire = observe_claim


def desktop_fixture(root: Path) -> int:
    """以真实 Electron 认证后台启动，复用显式离线模型和进程故障夹具。"""
    from agent_harness.desktop.server import main
    prepare(root)
    install_faults(root)
    (root / "backend.pid").write_text(str(os.getpid()), encoding="ascii")
    return main(model_client=CrashModel(root))


def main() -> None:
    """按指定 data-dir 与 port 启动 Web 夹具，产物和计数写入该目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    project, argv = prepare(root)
    install_faults(root)
    web = WebConfig(project_dir=root, worktrees_root=root / "trees",
                    frontend_dist=Path(__file__).resolve().parents[2] / "frontend" / "dist",
                    projects=[ProjectPreset(id="crash", label="崩溃恢复验收", path=project,
                                            python_executable=sys.executable, test_argv=argv)])
    limits = RunLimits(max_model_turns=1) if (root / "budget-one").exists() else RunLimits()
    config = configured_run(local=True, tools=["read_file", "edit_file", "write_file"], limits=limits,
                            permission_policy=PermissionPolicy(verification="allow"))
    (root / "backend.pid").write_text(str(os.getpid()), encoding="ascii")
    uvicorn.run(create_app(web, config, root / "runs", model_client=CrashModel(root)),
                host="127.0.0.1", port=args.port, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
