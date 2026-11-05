"""无副作用的权限判定及任务局部的文件访问范围，不解析命令的自然语言含义。"""

from contextvars import ContextVar
from pathlib import Path

from agent_harness.permissions.models import PermissionPolicy


# 上下文按 asyncio 任务隔离；子 Agent 继承父级目录范围，普通宿主文件收集不受此上下文影响。
FILE_SCOPE: ContextVar[tuple[Path, tuple[Path, ...]] | None] = ContextVar("permission_file_scope", default=None)
READ_TOOLS = frozenset({"read_file", "search_files", "list_files", "read_context", "read_plan", "task_get", "task_list", "memory_get", "memory_search", "read_subagent", "wait_subagents", "team_get"})  # 无项目写入的已知工具。
CONTROL_TOOLS = frozenset({"enter_plan_mode", "submit_plan", "ask_user_question", "delegate_readonly", "delegate_develop", "cancel_subagent", "task_create", "task_update", "memory_propose", "team_create", "team_add_member", "team_offer_task", "team_dispatch", "team_send_message", "team_stop"})  # 各自控制器继续验证归属和状态。


def planning_allows(name: str) -> bool:
    """返回规划阶段是否允许 name；规划控制器和统一权限入口共用这一份白名单。"""
    return name in READ_TOOLS or name in {"enter_plan_mode", "submit_plan", "ask_user_question", "delegate_readonly", "cancel_subagent", "read_command", "wait_command", "stop_command"}


def roots(workspace: Path, values: list[str]) -> tuple[Path, ...]:
    """将已校验的相对 values 解析为 workspace 内目录；返回元组，链接越界时抛 ValueError。"""
    result = tuple((workspace / value).resolve() for value in values)
    if any(not path.is_relative_to(workspace.resolve()) for path in result):
        raise ValueError("权限目录解析到工作区外")
    return result


def path_allowed(path: Path, allowed: tuple[Path, ...], *, directory: bool = False) -> bool:
    """判断 path 是否属于 allowed；directory=True 允许遍历授权目录的祖先，仍不允许读取旁支文件。"""
    return any(path.is_relative_to(root) or (directory and root.is_relative_to(path)) for root in allowed)


def evaluate(policy: PermissionPolicy, mode: str, name: str, *, planning: bool = False) -> str:
    """返回 name 的 allow/deny/ask；policy 是宿主上限，mode 为会话选择，planning 为只读规划。

    此函数不访问文件、不保存决定。未知工具默认拒绝，读取 ask 暂不提供人工提升。
    """
    if name in {"read_command", "wait_command", "stop_command"}:
        return "allow"  # 只访问宿主已授权启动的本 Run 命令；不能创建进程或扩展目录。
    if planning and not planning_allows(name):
        return "deny"
    if name in {"write_file", "edit_file"}:
        category = "write"
    elif name == "run_command":
        category = "command"
    elif name == "verification":
        category = "verification"
    elif name in READ_TOOLS:
        return "allow" if policy.read == "allow" else "deny"
    elif name in CONTROL_TOOLS:
        return "deny" if (mode == "research" and name in {"task_create", "task_update", "memory_propose", "delegate_develop", "team_create", "team_add_member", "team_offer_task", "team_dispatch", "team_send_message", "team_stop"}) else "allow"
    else:
        return "deny"
    if planning or mode == "research":
        return "deny"
    host = getattr(policy, category)
    if host == "deny":
        return "deny"
    return "ask" if host == "ask" or mode == "strict" else "allow"
