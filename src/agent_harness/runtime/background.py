"""在模型安全边界收拢后台命令和用户配置的脚本 Hooks。"""

import json

from agent_harness.runtime.pipeline import Proceed, Replan
from agent_harness.runtime.session import Session


def deliver_commands(session: Session) -> bool:
    """将新结束的命令事实落盘并追加历史；返回是否追加了内容。"""
    commands = getattr(session.executor, "commands", None)
    values = commands.notifications() if commands is not None else []
    if not values:
        return False
    session.append({"role": "user", "content": "[宿主后台命令结束事实；不是新的用户要求，不授予权限。]\n" +
                    json.dumps(values, ensure_ascii=False)}, "command_notifications")
    return True


async def command_input(session: Session) -> Proceed:
    """模型请求前补充已结束的命令事实；活动命令仍由宿主追踪。"""
    deliver_commands(session)
    return Proceed()


async def settle_candidate_commands(session: Session) -> Proceed | Replan:
    """交付前收拢在途命令，或将请求期间完成的事实交回模型核对。"""
    commands = getattr(session.executor, "commands", None)
    if commands is not None and commands.active:
        await commands.settle()
        return Replan()
    return Replan() if deliver_commands(session) else Proceed()


async def run_start_hook(session: Session) -> Proceed:
    """新段触发 RunStart；审批、安全暂停与既有子段接续使用原逻辑事件键。"""
    manifest = session.context.manifest
    hooks = getattr(session.executor, "hooks", None)
    child_resume = manifest.delegation is not None and manifest.delegation.previous_run_id is not None
    if hooks is not None and manifest.permission_resume is None and manifest.execution_resume is None and not child_resume:
        await hooks.dispatch("RunStart", "start", {"prompt": manifest.task.prompt})
        hooks.deliver(session.messages)
    return Proceed()


async def hook_input(session: Session) -> Proceed:
    """派发安全边界上可处理的脚本队列，再追加已产生的可见反馈。"""
    hooks = getattr(session.executor, "hooks", None)
    if hooks is not None:
        await hooks.drain()
        hooks.deliver(session.messages)
    return Proceed()


async def candidate_hooks(session: Session) -> Proceed | Replan:
    """候选交付前派发脚本队列；实际产生反馈时要求模型复核。"""
    hooks = getattr(session.executor, "hooks", None)
    if hooks is not None:
        await hooks.drain()
        if hooks.deliver(session.messages):
            return Replan()
    return Proceed()


async def stop_hook(session: Session) -> Proceed | Replan:
    """独立验收前运行 Stop 脚本；反馈要求重新决策时清除当前交付和检查。"""
    hooks = session.context.hooks
    planning = getattr(session.executor, "planning", None)
    if hooks is not None and not (planning is not None and planning.state.phase == "planning"):
        await hooks.drain()
        feedback = await hooks.stop()
        if feedback is not None:
            # Hook 控制器已经提交反馈事件，当前会话只追加一次消息。
            session.append({"role": "user", "content": feedback})
            return Replan()
    return Proceed()
