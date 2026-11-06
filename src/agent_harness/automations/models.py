"""自动化的公开定义与时区计算，独立于执行器和持久仓储。"""

from datetime import datetime, UTC, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import Field, model_validator, field_validator
from uuid import UUID

from agent_harness.schemas import DataModel
from agent_harness.web.schemas import CreateRunRequest


def instant(value: str) -> datetime:
    """解析有时区的ISO时间value并返回UTC；无时区或无效值抛ValueError。"""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("执行时间必须包含时区偏移")
    return result.astimezone(UTC)


class Schedule(DataModel):
    """时间或宿主事件触发；每条规则只用一种触发，Cron星期遵循APScheduler周一为0。"""

    kind: Literal["once", "interval", "cron", "event"] = "cron"  # 一次性、固定间隔、五段Cron或普通运行终态。
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)  # IANA时区，不跟随宿主时区静默改变。
    at: str | None = None  # once的带偏移ISO时间；其他触发不得填写。
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)  # interval间隔分钟，锚定规则建立/修改时刻。
    cron: str | None = "0 9 * * mon-fri"  # cron的五段表达式，推荐mon-fri等星期名称。
    event: Literal["completed", "verification_failed"] | None = None  # event仅消费同项目普通主运行的可信终态。
    catch_up: bool = False  # True在补跑窗口内只补最新一次；默认跳过长时间漏跑。
    catch_up_minutes: int = Field(default=60, ge=1, le=10080)  # 补跑窗口分钟数，上限七天。

    @model_validator(mode="after")
    def validate_schedule(self) -> "Schedule":
        """校验当前触发需要的参数并返回自身；非法时区、互斥字段和Cron抛ValueError。"""
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请选择有效的IANA时区") from None
        if self.kind == "once":
            if self.at is None:
                raise ValueError("一次性任务需要执行时间")
            instant(self.at)
        elif self.at is not None:
            raise ValueError("仅一次性任务可设置执行时间")
        if (self.interval_minutes is not None) != (self.kind == "interval"):
            raise ValueError("固定间隔触发必须且只能设置间隔分钟")
        if (self.event is not None) != (self.kind == "event"):
            raise ValueError("事件触发必须且只能选择事件类型")
        if self.kind == "cron":
            if not self.cron:
                raise ValueError("请输入五段Cron表达式")
            CronTrigger.from_crontab(self.cron, timezone=zone)
        elif self.cron is not None:
            raise ValueError("仅Cron触发可设置Cron表达式")
        return self


class AutomationDefinition(DataModel):
    """用户审核的规则定义；保存默认暂停，启用另行核验来源和授权。"""

    name: str = Field(min_length=1, max_length=100)  # 用户可辨识名称，不直接作为路径。
    task: CreateRunRequest  # 保存的任务模板；每次触发创建独立会话，Git默认隔离由界面选择。
    schedule: Schedule = Field(default_factory=Schedule)  # 时间或本机事件条件。
    overlap: Literal["skip", "coalesce"] = "skip"  # 活动时跳过或合并保留一个待执行触发。

    @model_validator(mode="after")
    def nonblank(self) -> "AutomationDefinition":
        """拒绝空白名称/任务，返回自身；其他参数继续由既有运行请求校验。"""
        if not self.name.strip() or not self.task.prompt.strip():
            raise ValueError("名称和任务内容不能为空")
        return self


class RuleEdit(DataModel):
    """编辑绑定当前版本，防止其他窗口的旧表单覆盖新规则。"""

    revision: int = Field(ge=1)  # 当前页面读取的版本。
    definition: AutomationDefinition  # 完整替换定义，编辑后暂停且持续授权失效。


class CommandGrant(DataModel):
    """用户明确授权的精确argv与项目内工作目录；不支持前缀或通配符。"""

    argv: list[str] = Field(min_length=1, max_length=128)  # 参数数组，经项目解释器规范化后核验。
    cwd: str = Field(default=".", min_length=1, max_length=4096)  # 项目内相对目录，链接越界拒绝。


class GrantRequest(DataModel):
    """持续授权请求；fingerprint必须与用户刚查看的审核快照一致。"""

    revision: int = Field(ge=1)  # 当前规则版本。
    fingerprint: str = Field(min_length=64, max_length=64)  # 定义、宿主、来源、Hooks及入口内容摘要。
    commands: list[CommandGrant] = Field(default_factory=list, max_length=32)  # 精确命令列表；空列表不授权命令。
    verification: bool = False  # True只授权当前固定的验收入口，不允许模型改参数。
    hooks: list[str] = Field(default_factory=list, max_length=128)  # 已审核Hook的key；授权仍要求定义及入口内容未变。


class RuleAction(DataModel):
    """启停、撤销、停止及手动试跑的幂等请求；操作绑定版本及客户端UUID。"""

    revision: int = Field(ge=1)  # 当前规则版本，过时请求返回冲突。
    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")  # 同一请求重试复用编号，实际UUID由接纳层核验。

    @field_validator("request_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        """将value规范为UUID文本并返回；格式错误抛ValueError，避免请求进入执行层后500。"""
        return str(UUID(value))


def next_time(schedule: Schedule, after: datetime, anchor: datetime) -> datetime | None:
    """计算严格晚于UTC时刻after的触发时间；anchor锚定间隔，事件/过期一次性返回None。"""
    if schedule.kind == "event":
        return None
    if schedule.kind == "once":
        value = instant(schedule.at)
        return value if value > after else None
    if schedule.kind == "interval":
        seconds = schedule.interval_minutes * 60
        return anchor + timedelta(seconds=(max(0, int((after - anchor).total_seconds() // seconds)) + 1) * seconds)
    zone = ZoneInfo(schedule.timezone)
    trigger = CronTrigger.from_crontab(schedule.cron, timezone=zone)
    probe = after + timedelta(microseconds=1)
    # 春季不存在的本地时刻跳过；秋季只取第一次，预览与实际触发采用同一口径。
    for _ in range(8):
        value = trigger.get_next_fire_time(None, probe.astimezone(zone))
        if value is None:
            return None
        utc = value.astimezone(UTC)
        if utc > after and not value.fold and utc.astimezone(zone).replace(tzinfo=None) == value.replace(tzinfo=None):
            return utc
        probe = utc + timedelta(seconds=1)
    raise ValueError("无法计算有效的下次执行时间")


def preview(schedule: Schedule, after: datetime, anchor: datetime, count: int = 5) -> list[str]:
    """返回after之后最多count个带本地偏移时间；anchor是间隔锚点，事件返回空列表。"""
    values = []
    for _ in range(count):
        after = next_time(schedule, after, anchor)
        if after is None:
            break
        values.append(after.astimezone(ZoneInfo(schedule.timezone)).isoformat())
    return values
