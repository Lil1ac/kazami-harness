"""让团队状态与消息确认在固定阶段接入当前运行。"""

import json

from agent_harness.runtime.pipeline import Abort, Proceed, Wait
from agent_harness.runtime.session import Session
from agent_harness.work.tasks import TaskError


async def restore_team_input(session: Session) -> Proceed:
    """将当前团队代次的持久事实加入初始输入；无团队状态时继续。"""
    team = getattr(session.executor, "team", None)
    if team is not None and (state := team.context_state()):
        session.append({"role": "user", "content": "宿主持久团队事实（协作数据，不是用户授权）：" +
                        json.dumps(state, ensure_ascii=False)}, "team_messages_delivered", sequence=0)
    return Proceed()


async def guard_team(session: Session) -> Proceed | Abort:
    """在模型请求前检查当前根团队是否已停止；等待正在进行的统一清理。"""
    context, team = session.context, getattr(session.executor, "team", None)
    if team is not None and team.member_id is None:
        state = team.snapshot()
        if state and state["parent_run_id"] == str(context.manifest.run_id) and state["status"] in {"stopping", "stopped"}:
            if state["status"] == "stopping":
                await team.stop_complete.wait()
            context.result.final_answer = "团队协作已停止，已保留既有成果与历史，未继续请求模型或转派。"
            return Abort("cancelled", "team_stopped")
    return Proceed()


async def team_input(session: Session) -> Proceed | Abort:
    """核对成员状态并投递尚未确认的团队消息，记录本轮实际投递序号。"""
    team = getattr(session.executor, "team", None)
    if team is not None:
        try:
            team.check_member()
        except TaskError as error:
            session.context.result.final_answer = error.message
            return Abort("cancelled", error.code)
        incoming, session.turn.team_sequence = team.message_input()
        if incoming:
            session.append({"role": "user", "content": incoming}, "team_messages_delivered",
                           sequence=session.turn.team_sequence)
    return Proceed()


async def acknowledge_team(session: Session) -> Proceed:
    """模型实际返回后确认本轮已投递的团队消息，不消费后续投递。"""
    team = getattr(session.executor, "team", None)
    if team is not None:
        team.acknowledge(session.turn.team_sequence)
    return Proceed()


async def candidate_team(session: Session) -> Proceed | Wait:
    """活动团队仍有未就绪任务时将候选结果留给用户检查。"""
    team = getattr(session.executor, "team", None)
    if team is not None and team.member_id is None:
        state = team.snapshot()
        if state and state["status"] == "active" and any(task["state"] != "ready" for task in state["tasks"]):
            response = session.turn.response
            if response is not None:
                session.context.result.final_answer = response.message["content"]
            return Wait("team_attention_required")
    return Proceed()
