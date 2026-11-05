"""发现、固定并审核 Hooks；仅读取配置和入口文件，不执行命令。"""

import hashlib
import json
import shutil
from pathlib import Path
import tomllib

from pydantic import ValidationError

from agent_harness.hooks.models import HookPolicy, HookRule
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import WorkStore, TaskError, encode


class HookStore:
    """使用宿主工作数据库保存审核及阶段检查点，避免把批准信息交给项目修改。"""

    def __init__(self, root):
        """绑定记录根 root，返回 None；构造本身不创建数据库。"""
        self.work = WorkStore(root)  # 与审批共用受保护的数据目录和事务。

    def get(self, key: str) -> dict | None:
        """读取稳定 key 的审核或检查点；缺失返回 None，不触发脚本。"""
        with self.work.transaction() as db:
            row = db.execute("SELECT body FROM hook_state WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict) -> None:
        """原子保存 key/value；返回 None，存储失败由 WorkStore 上抛。"""
        with self.work.transaction() as db:
            db.execute("INSERT OR REPLACE INTO hook_state VALUES (?,?)", (key, encode(value)))


def discover(workspace: Path, policy: HookPolicy, store: HookStore, *, agent_scope: str | None = None) -> list[dict]:
    """按宿主、项目顺序读取 workspace 规则，返回含入口身份与审核状态的快照。

    policy 控制项目发现，agent_scope=None 展示全部；非空时只解析该角色实际适用的脚本路径。
    配置最大 256KiB、入口脚本最大 2MiB。无配置返回空列表；
    非法字段、重复编号、路径越界、链接配置或无法读取入口均抛 TaskError，不执行任何脚本。
    """
    workspace = workspace.resolve(strict=True)
    groups = [("host", policy.rules)]
    path = workspace / ".agents" / "hooks.toml"
    try:
        if policy.project_enabled and path.exists():
            if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(workspace) or path.stat().st_size > 262144:
                raise ValueError("invalid config")
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            if set(raw) - {"hooks"} or not isinstance(raw.get("hooks", []), list):
                raise ValueError("invalid config")
            groups.append(("project", [HookRule.model_validate(item) for item in raw.get("hooks", [])]))
        result = []
        info = workspace.stat()
        for source, rules in groups:
            seen = set()
            for rule in rules:
                if rule.id in seen:
                    raise ValueError("duplicate id")
                seen.add(rule.id)
                if agent_scope is not None and agent_scope not in rule.agent_scopes:
                    continue
                cwd = (workspace / rule.cwd).resolve(strict=True)
                if not cwd.is_dir() or not cwd.is_relative_to(workspace):
                    raise ValueError("invalid cwd")
                scripts = {}
                previews = {}
                for argument in rule.argv[1:]:
                    candidate = cwd / argument
                    if argument.lower().endswith((".py", ".ps1", ".js", ".sh")):
                        if not candidate.is_file() or candidate.stat().st_size > 2097152:
                            raise ValueError("invalid entry script")
                        resolved = candidate.resolve(strict=True)
                        with resolved.open("rb") as stream:
                            content = stream.read(2097153)
                        if len(content) > 2097152:
                            raise ValueError("entry too large")
                        scripts[str(resolved)] = hashlib.sha256(content).hexdigest()
                        previews[str(resolved)] = {"text": content[:16384].decode("utf-8", errors="replace"), "truncated": len(content) > 16384}
                key = source + ":" + rule.id
                program = Path(rule.argv[0])
                resolved_program = str((cwd / program).resolve()) if program.is_absolute() or "/" in rule.argv[0] or "\\" in rule.argv[0] else shutil.which(rule.argv[0])
                definition = {"key": key, "rule": rule.model_dump(mode="json"), "workspace": str(workspace), "identity": [info.st_dev, info.st_ino], "cwd": str(cwd), "scripts": scripts, "executable": resolved_program}
                fingerprint = payload_digest(definition)
                review_key = "review:" + payload_digest({"workspace": str(workspace), "key": key})
                review = store.get(review_key) or {}
                result.append({**definition, "fingerprint": fingerprint, "review_key": review_key,
                               "script_previews": previews, "previous_definition": review.get("definition"),
                               "trusted": review.get("fingerprint") == fingerprint,
                               "enabled": rule.enabled and review.get("enabled", True)})
        if len(result) > 128:
            raise ValueError("too many hooks")
        return result
    except (OSError, ValueError, ValidationError) as error:
        raise TaskError("hooks_config_invalid", "Hooks 配置或入口脚本无法核对，请检查配置、目录和重复编号。") from None


def review(workspace: Path, policy: HookPolicy, store: HookStore, request) -> list[dict]:
    """核验 request 指纹后保存 workspace 的审核/启停，返回最新目录；变化抛 TaskError。"""
    items = discover(workspace, policy, store)
    item = next((value for value in items if value["key"] == request.key), None)
    if item is None or item["fingerprint"] != request.fingerprint:
        raise TaskError("hook_definition_changed", "Hook 定义或脚本已变化，请重新加载并审核。")
    if request.action == "disable" and item["key"].startswith("host:") and item["rule"]["required"]:
        raise TaskError("hook_managed_required", "宿主必需门禁不能从页面禁用。")
    state = store.get(item["review_key"]) or {}
    if request.action == "trust":
        state["fingerprint"] = item["fingerprint"]
        state["definition"] = {"rule": item["rule"], "scripts": item["scripts"], "script_previews": item["script_previews"]}
    else:
        state["enabled"] = request.action == "enable"
    store.put(item["review_key"], state)
    return discover(workspace, policy, store)
