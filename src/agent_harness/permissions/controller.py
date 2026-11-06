"""在副作用入口判定权限、冻结审批动作并核验批准后的原动作。"""

import hashlib
import json
from datetime import datetime, UTC
from pathlib import Path
from uuid import uuid4

from agent_harness.execution.workspace import Workspace
from agent_harness.permissions.models import PermissionState
from agent_harness.permissions.policy import evaluate, roots, path_allowed, FILE_SCOPE
from agent_harness.permissions.store import PermissionStore
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import TaskError


def file_identity(path: Path, max_bytes: int, *, include_text: bool = False) -> dict:
    """返回 path 的缺失状态或有界内容指纹；max_bytes 为扫描字节数，特殊文件或超限拒绝。

    include_text=True从同一份已计算指纹的字节生成审批前文；非UTF-8保留None，不伪造文本差异。
    """
    if not path.exists():
        return {"exists": False}
    if not path.is_file() or path.is_symlink() or path.is_junction() or path.stat().st_size > max_bytes:
        raise TaskError("permission_input_unverifiable", "目标或脚本不是可核验的有界普通文件。")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise TaskError("permission_input_unverifiable", "动作输入超出审批核验大小限制。")
    info = path.stat()
    result = {"exists": True, "sha256": hashlib.sha256(raw).hexdigest(), "identity": [info.st_dev, info.st_ino]}
    if include_text:
        try:
            result["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            result["text"] = None
    return result


class PermissionController:
    """绑定一个真实 Run 的授权视图；不向模型提供批准或修改权限的工具。"""

    def __init__(self, context, planning=None):
        """绑定运行 context 与可选 planning；构造只计算快照，目录异常拒绝运行。"""
        self.context = context  # 提供清单、预算、真实记录根与事件提交入口。
        self.manifest = context.manifest  # 本段执行的不可变任务和宿主配置。
        self.planning = planning  # 根执行的实时规划控制器，子执行为空。
        self.policy = self.manifest.config.permission_policy  # 宿主上限，不从网页批准载荷读取。
        self.store = PermissionStore(context.runs_root)  # 与作业共用事务事实源。
        self.approved: dict | None = None  # 仅接续原动作时临时绑定，消费后立即清空。
        self.external = None  # Agent 绑定本轮 MCP 控制器；未绑定时所有外部名称拒绝。
        self.workspace = self.manifest.task.workspace.resolve()  # 实际工作树或原目录。
        self.read_roots = roots(self.workspace, self.policy.read_roots)  # 已解析并确认不越界的可读目录。
        self.write_roots = roots(self.workspace, self.policy.write_roots)  # 与读取范围独立，不能由动作批准扩展。
        self.state = PermissionState(mode=self.manifest.config.permission_mode, policy_fingerprint=self.fingerprint())  # 当前运行唯一权限投影，终态固定副本。
        resume = self.manifest.permission_resume or self.manifest.execution_resume
        if resume is not None:
            self.state.spent_model_turns = resume.spent_model_turns
            self.state.spent_tool_calls = resume.spent_tool_calls
            self.state.spent_seconds = resume.spent_seconds
            self.state.spent_usage = resume.spent_usage.copy()
            self.state.spent_usage_complete = resume.spent_usage_complete
        self.snapshot()

    def snapshot(self) -> PermissionState:
        """返回实时权限的独立快照；保留本轮待审批对象，计算规划阶段变化后的有效判定。"""
        self.state.policy_fingerprint = self.fingerprint()
        self.state.effective = {name: self.decision(name) for name in ("read_file", "write_file", "edit_file", "run_command", "verification")}
        if self.external is not None:
            self.state.effective.update({name: self.decision(name) for name in self.external.entries})
        return self.state.model_copy(deep=True)

    def fingerprint(self) -> str:
        """返回当前宿主、会话、工作区、规划和执行配置指纹，不包含凭据或动态进度。"""
        state = self.planning.state if self.planning is not None else self.manifest.planning
        plan = state.plans[-1] if state.plans else None
        info = self.workspace.stat()
        return payload_digest({"policy": self.policy.model_dump(), "mode": self.manifest.config.permission_mode,
            "tools": self.manifest.config.tools, "workspace": str(self.workspace), "identity": [info.st_dev, info.st_ino],
            "phase": state.phase, "plan": None if plan is None else [plan.version, plan.status],
            "python": self.manifest.task.python_execution.model_dump(mode="json") if self.manifest.task.python_execution else None,
            "verifier": self.manifest.task.verifier.model_dump(mode="json") if self.manifest.task.verifier else None,
            "hooks": self.manifest.config.hooks.model_dump(mode="json"),
            "mcp_tools": [item.model_dump() for item in self.manifest.config.mcp_tools]})

    def decision(self, name: str) -> str:
        """返回工具 name 的有效判定；实时规划和子只读约束在实际入口再次检查。"""
        if name.startswith("mcp_"):
            if self.external is None or name not in self.external.entries or self.manifest.automation is not None:
                return "deny"
            research = self.external.research(name)
            planning = (self.planning.state if self.planning is not None else self.manifest.planning).phase == "planning"
            readonly = self.manifest.config.permission_mode == "research" or self.manifest.delegation is not None and self.manifest.delegation.role != "develop"
            if (planning or readonly) and not research or research and self.policy.read == "deny":
                return "deny"
            if self.manifest.delegation is not None and self.manifest.delegation.role != "develop" and (self.policy.external != "allow" or self.manifest.config.permission_mode == "strict"):
                return "deny"  # 只读子执行没有动作接续检查点；仅宿主已放行的研究工具可继承，不能制造孤立审批。
            return "deny" if self.policy.external == "deny" else "ask" if self.manifest.config.permission_mode == "strict" else self.policy.external
        if name in {"read_file", "search_files", "write_file", "edit_file", "run_command"} and name not in self.manifest.config.tools:
            return "deny"
        if name == "verification" and self.manifest.task.verifier is None:
            return "deny"
        if self.manifest.delegation is not None and self.manifest.delegation.role != "develop":
            from agent_harness.subagents.profiles import READONLY_TOOLS
            if name not in READONLY_TOOLS:
                return "deny"
        planning = (self.planning.state if self.planning is not None else self.manifest.planning).phase == "planning"
        return evaluate(self.policy, self.manifest.config.permission_mode, name, planning=planning)

    def inputs(self, name: str, arguments: dict) -> dict:
        """固定 name/arguments 的目标身份并返回核验字段；文件版本或路径不合法时拒绝且无副作用。"""
        if name.startswith("mcp_"):
            if self.external is None:
                raise TaskError("permission_denied", "本轮未开放外部工具。")
            return self.external.inputs(name)
        boundary = Workspace(self.workspace, self.context.runs_root)
        result = {}
        token = FILE_SCOPE.set(None)  # 这里独立核验写权限，不误用当前只读上下文的目录范围。
        try:
            if name in {"write_file", "edit_file"}:
                target = boundary.resolve(arguments["path"])
                if any(part.lower() in {".git", ".agents", ".codex"} for part in target.relative_to(self.workspace).parts):
                    raise TaskError("permission_protected_path", "不能通过文件工具修改版本库元数据或代理配置目录。")
                if not path_allowed(target, self.write_roots):
                    raise TaskError("permission_path_denied", "目标文件不属于当前可写范围。")
                result = {"target": str(target), "file": file_identity(target, self.manifest.config.limits.max_file_write_bytes, include_text=name == "write_file")}
            elif name in {"run_command", "verification"}:
                if name == "run_command":
                    cwd = boundary.resolve(arguments.get("cwd", "."), must_exist=True)
                    if not path_allowed(cwd, self.read_roots, directory=True):
                        raise TaskError("permission_path_denied", "命令目录不属于当前授权范围。")
                else:
                    cwd = Path(arguments["cwd"]).resolve(strict=True)  # 独立验收允许宿主登记的项目外入口，必须与固定 spec 匹配。
                info = cwd.stat()
                if not cwd.is_dir():
                    raise TaskError("permission_path_denied", "命令工作目录不存在。")
                from agent_harness.execution.python import python_command
                import shutil
                argv = python_command(arguments["argv"], self.manifest.task.python_execution)
                executable = Path(argv[0])
                if not executable.is_absolute():
                    found = shutil.which(argv[0]) if not any(c in argv[0] for c in "/\\") else str(cwd / argv[0])
                    executable = Path(found).resolve() if found else executable
                result = {"cwd": str(cwd), "identity": [info.st_dev, info.st_ino], "executable": str(executable), "scripts": {}}
                if executable.is_absolute() and executable.is_file():
                    binary = executable.stat()
                    result["executable_identity"] = [binary.st_dev, binary.st_ino, binary.st_size, binary.st_mtime_ns]
                # 只核验显式指定的普通脚本，不声称静态推断任意命令的全部传递依赖。
                for value in argv[1:]:
                    candidate = cwd / value
                    if value.lower().endswith((".py", ".ps1", ".js", ".sh")) and candidate.is_file():
                        result["scripts"][str(candidate.resolve())] = file_identity(candidate, 2 * 1024 * 1024)
        finally:
            FILE_SCOPE.reset(token)
        return result

    def check(self, name: str, arguments: dict, call_id: str = "", *, hook: dict | None = None, hook_operation: dict | None = None) -> dict | None:
        """在执行 name 前检查 arguments；允许返回 None，审批返回请求，拒绝抛 TaskError。

        call_id 关联原调用；hook 固定脚本事件输入，hook_operation 固定原工具阶段，None 表示普通动作。
        审批保存失败时不开放执行，不消费已有批准。
        """
        decision = self.decision(name)
        if decision == "deny":
            raise TaskError("permission_denied", "宿主、会话或规划权限禁止此操作。")
        inputs = self.inputs(name, arguments) if name.startswith("mcp_") or name in {"write_file", "edit_file", "run_command", "verification"} else {}
        policy = self.fingerprint()
        self.context.result.permissions = self.snapshot()
        if self.manifest.automation is not None:
            from agent_harness.automations.authorization import check_automation
            if check_automation(self, name, arguments, inputs, hook) and decision == "ask":
                decision = "allow"  # 仅匹配精确规则许可；deny已在上方拒绝，规划及严格模式仍生效。
                self.context.emit("automation_authorized", {"origin": self.manifest.automation.model_dump(), "name": name,
                    "arguments_sha256": payload_digest(arguments), "inputs": inputs, "policy_fingerprint": policy})
        if self.approved is not None:
            original = self.approved
            if original["name"] != name or original["arguments"] != arguments or original["inputs"] != inputs or original["policy_fingerprint"] != policy:
                raise TaskError("permission_stale", "动作输入、工作区或权限发生变化，尚未执行。")
            if hook is not None and (original.get("hook", {}).get("input") != hook["input"] or original.get("hook", {}).get("fingerprint") != hook["fingerprint"]):
                raise TaskError("permission_stale", "Hook 事件输入或定义与本次批准不一致。")
            self.approved = None  # 临时单次许可不能被后续调用复用。
            return None
        if decision == "allow":
            return None
        created = datetime.now(UTC).timestamp()
        value = {"id": str(uuid4()), "session_id": str(self.manifest.session_id or self.manifest.run_id),
            "source_run_id": str(self.manifest.run_id), "call_id": call_id, "name": name, "arguments": arguments,
            "inputs": inputs, "policy_fingerprint": policy, "workspace": str(self.workspace),
            "created_at": created, "expires_at": created + self.policy.approval_ttl_seconds,
            "status": "pending", "decision": None, "execution_run_id": None,
            "final_answer": self.context.result.final_answer if name == "verification" else None}
        if self.manifest.delegation is not None and self.manifest.delegation.role == "develop":
            value.update(source_run_id=str(self.manifest.delegation.parent_run_id),
                         child_parent_id=str(self.manifest.delegation.parent_run_id), child_run_id=str(self.manifest.run_id))
        if hook is not None:
            value["hook"] = hook
        if hook_operation is not None:
            value["hook_operation"] = hook_operation
        if self.context.hooks is not None and (hook is not None or hook_operation is not None):
            value["hook_definitions"] = {item["key"]: item["fingerprint"] for item in self.context.hooks.definitions}
        value["fingerprint"] = payload_digest(value)
        self.context.emit("permission_requested", {"action": value})
        self.store.save(value)
        self.state.pending = value.copy()
        self.context.result.permissions = self.snapshot()
        return value

    def prepare_resume(self) -> dict:
        """核验并认领清单中的已批准动作，返回保存动作；所有条件先于消费和真实副作用检查。"""
        resume = self.manifest.permission_resume
        value = self.store.get(resume.action_id)
        if value.get("decision") != resume.decision or value["session_id"] != str(self.manifest.session_id or self.manifest.run_id):
            raise TaskError("permission_stale", "接续决定与当前会话不匹配。")
        if resume.decision != "approve":
            return value
        if self.decision(value["name"]) == "deny" or value["policy_fingerprint"] != self.fingerprint() or value["inputs"] != self.inputs(value["name"], value["arguments"]):
            raise TaskError("permission_stale", "审批期间输入或权限发生变化，原动作未执行。")
        if "hook_definitions" in value:
            hooks = self.context.hooks
            current = {item["key"]: item["fingerprint"] for item in hooks.definitions} if hooks is not None else {}
            if current != value["hook_definitions"]:
                raise TaskError("hook_definition_changed", "审批期间 Hook 定义或脚本发生变化，原动作未执行。")
            if value.get("hook"):
                item = next((entry for entry in hooks.definitions if entry["key"] == value["hook"]["key"]), None)
                if item is None or not item["trusted"] or not item["enabled"]:
                    raise TaskError("hook_definition_changed", "Hook 未审核或已禁用，旧批准不会执行。")
        value = self.store.claim(value["id"], str(self.manifest.run_id))
        if self.manifest.delegation is not None:
            value["execution_child_parent_id"] = str(self.manifest.delegation.parent_run_id)
            self.store.save(value)
        self.approved = value
        return value

    def finish(self, value: dict, ok: bool) -> None:
        """保存 value 已经落盘的执行结论；ok 表示真实工具/验收是否成功，返回 None。"""
        value = self.store.get(value["id"])
        value.update(status="executed" if ok else "failed")
        self.store.save(value)
