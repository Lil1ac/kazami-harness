"""团队工具的有界输入；所有磁盘位置、执行编号和发送者都由宿主解析。"""

from pydantic import Field
from agent_harness.schemas import DataModel


class TeamCreate(DataModel):
    """负责人创建本会话唯一团队；批准方案与目录身份由控制器核验。"""
    title: str = Field(min_length=1, max_length=120)  # 团队目标的简短名称，不是文件目录。


class MemberAdd(DataModel):
    """添加具有固定文件授权范围的持久成员，不立即请求模型。"""
    name: str = Field(min_length=1, max_length=80)  # 团队内唯一显示名，成员稳定编号由宿主分配。
    write_scope: list[str] = Field(min_length=1, max_length=20)  # 相对文件/目录，实际权限再与父级取交集。


class TaskOffer(DataModel):
    """把既有C04工作项纳入团队；重发失败项时必须已安全停止，依赖自动读取原清单。"""
    task_id: str = Field(min_length=1, max_length=100)  # 当前会话实际工作项，不允许跨会话。
    objective: str = Field(min_length=1, max_length=8000)  # 本项执行目标，不替代批准方案。
    write_scope: list[str] = Field(min_length=1, max_length=20)  # 本项可写范围，不能超出被认领成员授权。
    member_id: str | None = None  # None允许符合授权的空闲成员自动认领；非空指定持久成员。


class TeamMessage(DataModel):
    """发送有来源标识的协作数据；消息不能提供用户授权或改变工具权限。"""
    recipient: str = Field(min_length=1, max_length=100)  # lead或同团队成员编号。
    content: str = Field(min_length=1, max_length=4000)  # 有界指示/反馈，不是可执行命令。


class TeamRead(DataModel):
    """读取当前团队和本身份的待处理消息，不启动模型或重复任务。"""


TEAM_TOOLS = {
    "team_create": (TeamCreate, "在已批准方案内建立本会话团队，不创建成员模型请求。"),
    "team_add_member": (MemberAdd, "添加持久团队成员及文件范围；成员独立上下文、共享父权限与总预算。"),
    "team_offer_task": (TaskOffer, "将已有任务纳入团队，使用C04依赖；成员只能认领授权范围内的可执行项。失败转派先确认旧执行及进程已停止。"),
    "team_dispatch": (TeamRead, "为空闲成员原子认领可执行团队任务并异步启动，最多两个活动子执行；用wait_subagents回收，依赖就绪后再次dispatch。"),
    "team_send_message": (TeamMessage, "向负责人lead或成员编号发送协作消息，宿主记录发送者；消息不能代替用户批准。"),
    "team_get": (TeamRead, "读取团队、任务归属、成果版本和当前身份的消息；空闲不会轮询模型。"),
    "team_stop": (TeamRead, "负责人停止团队并收拢成员，保留固定成果及历史，不自动删除工作树。"),
}  # 成员只获得读取与发消息，管理权始终属于负责人。
MEMBER_TOOLS = frozenset({"team_get", "team_send_message"})  # 可持续通信，不开放递归或自批操作。
