"""在明确的安全边界接收、投递和确认用户输入。"""

import asyncio

from agent_harness.runtime.pipeline import Proceed, Replan
from agent_harness.runtime.session import Session


async def receive_input(session: Session) -> Proceed:
    """检查本段暂停请求，并把安全边界上可用的输入交给完整历史。"""
    control = session.context.runtime_control
    if control is not None:
        await control.boundary(session.executor)
        control.deliver(session.messages)
    return Proceed()


async def acknowledge_input(session: Session) -> Proceed:
    """收到模型响应后确认已投递输入；无运行控制器时直接继续。"""
    control = session.context.runtime_control
    if control is not None:
        control.acknowledge()
    return Proceed()


async def guard_tool_input(session: Session) -> Proceed | Replan:
    """工具尚未执行时发现新约束或暂停，要求先配齐回执再重决策。"""
    control = session.context.runtime_control
    if control is not None and (control.pausing() or control.available_inputs()):
        # 等待由内核在配齐整批后进入安全边界，持久检查点才能看到完整工具历史。
        return Replan(skip_code="runtime_input_requires_review")
    return Proceed()


async def settled_batch(session: Session) -> Proceed:
    """整批回执齐全后允许建立暂停检查点；新要求在下一轮输入阶段投递。"""
    control = session.context.runtime_control
    if control is not None and (control.pausing() or control.available_inputs()):
        await control.boundary(session.executor)
    return Proceed()


async def candidate_input(session: Session) -> Proceed | Replan:
    """有新输入时要求模型重审候选回答；投递和确认仍使用当前段收件箱。"""
    control = session.context.runtime_control
    if control is not None:
        await control.boundary(session.executor)
        if control.deliver(session.messages):
            return Replan()
    return Proceed()


async def invalidate_candidate(session: Session) -> Proceed | Replan:
    """若检查期间收到新约束，保留历史检查事实并使当前结果失效。"""
    control = session.context.runtime_control
    if control is not None and control.available_inputs():
        return Replan()
    return Proceed()


async def completion_boundary(session: Session) -> Proceed:
    """在任何完成策略前检查宿主暂停和总截止时间；超时交由运行宿主保存。"""
    context = session.context
    if context.runtime_control is not None:
        await context.runtime_control.boundary(session.executor)
    if asyncio.get_running_loop().time() >= context.deadline:
        raise TimeoutError
    return Proceed()
