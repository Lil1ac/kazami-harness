"""建立强制工具边界，并保证正常及异常退出都清理后台资源。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from agent_harness.hooks.controller import HookController
from agent_harness.tools.registry import ToolExecutor

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext


@asynccontextmanager
async def execution_environment(context: "RunContext", executor: ToolExecutor) -> AsyncIterator[None]:
    """绑定 context 的文件权限及 executor 的宿主控制器；退出时收拢子段与进程。"""

    from agent_harness.permissions.policy import FILE_SCOPE, roots
    policy = context.manifest.config.permission_policy
    token = FILE_SCOPE.set((context.manifest.task.workspace.resolve(), roots(context.manifest.task.workspace.resolve(), policy.read_roots) if policy.read == "allow" else ()))
    try:
        if isinstance(executor, ToolExecutor):
            from agent_harness.execution.commands import CommandController
            from agent_harness.runtime.control import RuntimeControl
            if context.runtime_control is None:
                context.runtime_control = RuntimeControl(context)
            executor.commands = CommandController(context, executor)
            context.command_controller = executor.commands
            (context.budget_parent or context).command_controllers[str(context.manifest.run_id)] = executor.commands
        if getattr(executor, "permissions", None) is None and isinstance(executor, ToolExecutor):
            from agent_harness.permissions.controller import PermissionController
            spent = context.result.permissions
            executor.permissions = PermissionController(context, getattr(executor, "planning", None))
            if context.manifest.delegation is not None and context.manifest.permission_resume is None:
                for field in ("spent_model_turns", "spent_tool_calls", "spent_seconds", "spent_usage", "spent_usage_complete"):
                    setattr(executor.permissions.state, field, getattr(spent, field))
            context.result.permissions = executor.permissions.state.model_copy(deep=True)
            context.emit("permissions_prepared", {"state": context.result.permissions.model_dump(mode="json")})
        if isinstance(executor, ToolExecutor):
            from agent_harness.mcp.controller import McpController
            executor.mcp = McpController(context)
            executor.permissions.external = executor.mcp
            if executor.mcp.entries:
                context.emit("mcp_prepared", {"tools": [{"name": name, "config": item["record"]["config"],
                    "fingerprint": item["record"]["fingerprint"], "definition": item["tool"],
                    "research": executor.mcp.research(name), "local_identity": item["local_identity"]}
                    for name, item in executor.mcp.entries.items()]})
            executor.hooks = HookController(context, executor)
            context.hooks = executor.hooks
            context.emit("hooks_prepared", {"hooks": executor.hooks.snapshot})
        yield
    finally:
        FILE_SCOPE.reset(token)
        try:
            controller = getattr(executor, "subagents", None)
            if controller is not None:
                await controller.close()
        finally:
            commands = getattr(executor, "commands", None)
            if commands is not None:
                await commands.settle(stop=True)
