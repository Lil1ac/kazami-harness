"""规则级持续授权的逐动作核验；模型不能创建、更新或消费其他规则的许可。"""

import json
from pathlib import Path

from agent_harness.permissions.controller import file_identity
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import WorkStore, TaskError


def relative(value: str, workspace: Path) -> str:
    """将workspace内绝对value规范为占位路径，外部路径保留；不扩大实际文件边界。"""
    path = Path(value)
    return "{workspace}/" + path.relative_to(workspace).as_posix() if path.is_absolute() and path.is_relative_to(workspace) else value


def command_binding(inputs: dict, workspace: Path) -> dict:
    """将权限入口inputs转换为跨工作树可比较内容；保留外部程序身份，脚本按内容比较。"""
    result = {key: value for key, value in inputs.items() if key not in {"identity", "scripts"}}
    result["cwd"] = relative(inputs["cwd"], workspace)
    result["scripts"] = {relative(path, workspace): value.get("sha256") for path, value in inputs.get("scripts", {}).items()}
    executable = inputs.get("executable", "")
    result["executable"] = relative(executable, workspace)
    if result["executable"] != executable:
        result.pop("executable_identity", None)
        result["executable_sha256"] = file_identity(Path(executable), 32 * 1024 * 1024).get("sha256")
    return result


def current_rule(root: Path, origin) -> dict:
    """读取origin当前有效规则；版本或授权代次变化抛TaskError，不自动沿用新授权。"""
    with WorkStore(root).transaction() as db:
        row = db.execute("SELECT body FROM automation_rules WHERE id=?", (origin.rule_id,)).fetchone()
    rule = json.loads(row[0]) if row else None
    if rule is None or rule["revision"] != origin.revision or rule["grant_generation"] != origin.grant_generation or rule.get("invalid_reason"):
        raise TaskError("automation_authorization_revoked", "自动化定义、来源或授权已变化，请重新审核规则。")
    return rule


def check_automation(controller, name: str, arguments: dict, inputs: dict, hook: dict | None) -> bool:
    """核验controller当前动作并返回是否有持续许可；普通运行返回False，撤销阻止所有新副作用。"""
    origin = controller.manifest.automation
    if origin is None:
        return False
    rule = current_rule(controller.context.runs_root, origin)
    # 每次副作用都核对源目录和宿主配置；暂停仅停止新调度，保留在途运行的原许可。
    from agent_harness.automations.service import environment_snapshot
    snapshot = environment_snapshot(rule["definition"], controller.context.runs_root, rule["host_config"])
    if snapshot["fingerprint"] != rule["environment"]["fingerprint"]:
        raise TaskError("automation_source_changed", "自动化项目、配置或入口内容变化，未执行新动作。")
    grant = rule.get("grant")
    if not grant or not controller.policy.automation_grants or controller.manifest.config.permission_mode == "strict":
        return False
    if payload_digest(controller.manifest.config.model_dump(mode="json")) != rule["config_digest"]:
        return False  # 人工接续修改配置后，不把原规则持续许可扩展到新运行配置。
    if name not in {"run_command", "verification"}:
        return False
    binding = command_binding(inputs, controller.workspace)
    if hook is not None:
        definitions = controller.context.hooks.definitions if controller.context.hooks else []
        current = next((item for item in definitions if item["key"] == hook["key"] and item["fingerprint"] == hook["fingerprint"]), None)
        return current is not None and any(item["key"] == hook["key"] and item["rule"] == current["rule"] and item["binding"] == binding for item in grant["hooks"])
    if name == "verification":
        return binding == grant.get("verification")
    argv = [relative(value, controller.workspace) for value in arguments["argv"]]
    return any(item["argv"] == argv and item["binding"] == binding for item in grant["commands"])


def inherit_hook_trust(context, controller, definitions: list[dict]) -> None:
    """仅将context规则明确授权的同内容Hook审核继承到实际工作树；controller核验真实入口身份。"""
    if context.manifest.automation is None:
        return
    rule = current_rule(context.runs_root, context.manifest.automation)
    grant = rule.get("grant")
    if not grant or not controller.policy.automation_grants:
        return
    for item in definitions:
        saved = next((value for value in grant["hooks"] if value["key"] == item["key"] and value["rule"] == item["rule"]), None)
        if saved is None or not item["enabled"]:
            continue
        binding = command_binding(controller.inputs("run_command", {"argv": item["rule"]["argv"], "cwd": item["cwd"]}), controller.workspace)
        if binding == saved["binding"]:
            item["trusted"], item["inherited_trust"] = True, True
