"""串行只读委派：独立上下文与日志，共享父预算，不进入单槽队列或递归创建执行者。"""

import asyncio
import codecs
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import Field

from agent_harness.providers.base import ModelError
from agent_harness.schemas import DataModel, DelegationOrigin, RunManifest, RunResult, SubagentRecord
from agent_harness.storage.runs import RunHistory, RunStore, StorageError
from agent_harness.subagents.profiles import PROFILES
from agent_harness.subagents.reports import finalize_report

if TYPE_CHECKING:
    from agent_harness.context.builder import ContextBuilder
    from agent_harness.providers.base import ModelClient
    from agent_harness.runtime.runner import RunContext
    from agent_harness.tools.registry import ToolExecutor


class DelegateReadonly(DataModel):
    """主 Agent 明确指定的子研究范围和预算；上限仍由父级剩余额度裁剪。"""

    title: str = Field(min_length=1, max_length=120, description="简短非空委派标题。")
    objective: str = Field(min_length=1, max_length=8000, description="完整研究问题、范围与必要背景；子执行不会看到父聊天历史。")
    role: Literal["explore", "review"] = Field(default="explore", description="explore探索调用关系；review审阅风险，均为只读。")
    task_id: UUID | None = Field(default=None, description="可选同会话工作项编号；仅关联，不认领或完成该项。")
    skill_names: list[str] = Field(default_factory=list, max_length=8, description="明确委派给子角色的技能名，只能选父本轮已冻结技能；默认空，不自动继承。")
    max_model_turns: int = Field(default=4, ge=1, le=12, description="子执行最多模型轮数，默认4，包含摘要，不增加父额度。")
    max_tool_calls: int = Field(default=8, ge=1, le=30, description="子执行最多工具次数，默认8，非法调用也占用。")
    timeout_seconds: float = Field(default=60.0, gt=0, le=180, allow_inf_nan=False, description="子执行总秒数，默认60；父级截止时间优先。")


class ReadSubagent(DataModel):
    """只读当前父运行已登记的子报告；不回读整个子对话或启动新执行。"""

    run_id: UUID = Field(description="delegate_readonly返回的子运行编号，只能属于当前父运行。")
    offset: int = Field(default=0, ge=0, description="报告UTF-8字节偏移；首次0，其后使用next_offset。")
    max_bytes: int = Field(default=8192, ge=4, le=8192, description="每页最大UTF-8字节数，默认8192，最小4避免字符内死循环。")


SUBAGENT_TOOLS = {
    "delegate_readonly": (DelegateReadonly, "串行委派独立只读探索/审阅；技能须显式选择父级快照。返回精简结构报告和核验来源，partial表示未完整交接。共用原预算并为父保留收尾额度；返回后同批剩余调用不执行，须阅读结果后重新决定。报告不代表父任务完成或验收通过。"),
    "read_subagent": (ReadSubagent, "只读当前Run内已登记子报告，按UTF-8字节分页；失败/中断不视为成功。不会重新运行子Agent或注入完整子对话。"),
}


class SubagentError(Exception):
    """向工具层返回受控委派错误，不包含供应商或存储异常正文。"""

    def __init__(self, code: str, message: str):
        """保存稳定code和中文message；返回None，不创建子执行。"""
        super().__init__(message)
        self.code = code  # 工具可识别的原因码。
        self.message = message  # 可直接展示的说明。


def read_child(root: Path, parent_id: UUID, child_id: UUID) -> RunHistory:
    """只读root中parent_id直属的child_id日志；返回可信前缀，归属或路径异常抛StorageError。

    root为真实运行根，不接受任意子路径；不追随指向父运行外部的日志目录替换。
    此函数不恢复执行、不打开项目文件，也不把子执行登记为独立会话。
    """
    parent = root.resolve() / str(parent_id)
    children = parent / "subagents"
    try:
        if children.resolve() != children or (children / str(child_id)).resolve() != children / str(child_id):
            raise StorageError("子执行记录目录身份发生变化。")
        history = RunStore.read(children, str(child_id))
        if history.manifest.delegation is None or history.manifest.delegation.parent_run_id != parent_id:
            raise StorageError("子执行不属于当前父运行。")
        return history
    except (OSError, RuntimeError):
        raise StorageError("子执行记录不可用。") from None


def report_page(history: RunHistory, offset: int, max_bytes: int) -> dict:
    """从history终态报告返回UTF-8安全页；offset为字节偏移，max_bytes至少4。

    报告未完成时返回空正文及真实状态；偏移越界或落在字符内部抛SubagentError，不重放工具。
    """
    report = history.result.subagent_report if history.result else None
    raw = report.model_dump_json().encode("utf-8") if report is not None else b""
    if offset > len(raw):
        raise SubagentError("subagent_invalid_offset", "报告偏移超过末尾，请使用上一页next_offset。")
    try:
        raw[:offset].decode("utf-8")
    except UnicodeDecodeError:
        raise SubagentError("subagent_invalid_offset", "报告偏移位于字符内部，请使用上一页next_offset。") from None
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(raw[offset:offset + max_bytes], final=offset + max_bytes >= len(raw))
    end = offset + len(text.encode("utf-8"))
    return {"run_id": str(history.manifest.run_id), "status": history.result.execution_status if history.result else "interrupted",
            "content": text, "offset": offset, "next_offset": None if end == len(raw) else end,
            "eof": end == len(raw), "total_bytes": len(raw)}


class SubagentController:
    """父Run内的唯一委派入口；同步等待子执行，所有生命周期变化先落盘再对外返回。"""

    def __init__(self, parent: "RunContext", client: "ModelClient", executor: "ToolExecutor"):
        """绑定parent预算、已开启client和父executor；返回None，不立即调用模型。"""
        self.parent = parent  # 共享预算与取消归属，委派不另占队列槽。
        self.client = client  # 复用同一供应商连接及模型配置，不新增凭据来源。
        self.executor = executor  # 读取父级工具白名单及当前规划、任务权限。
        self.inputs: "ContextBuilder | None" = None  # 首次模型输入准备后绑定固定约定、技能和记忆。

    def bind_inputs(self, inputs: "ContextBuilder") -> None:
        """固定父级inputs引用供后续委派继承；返回None，不继承消息或子历史。"""
        self.inputs = inputs

    def _publish(self, record: SubagentRecord, child: "RunContext") -> None:
        """把child已提交计数写入record并发布父快照；返回None，存储错误立即上抛。"""
        record.model_turns, record.tool_calls, record.http_attempts = child.result.model_turns, child.result.tool_calls, child.result.http_attempts
        record.usage, record.usage_complete = child.result.usage.model_dump(), child.result.usage_complete
        record.elapsed_seconds, record.event_count = child.active_duration(), child.event_count
        self.parent.emit("subagent_snapshot", {"subagent": record.model_dump(mode="json")})

    async def execute(self, name: str, arguments) -> dict:
        """处理已校验name/arguments并返回委派回执；无效归属抛SubagentError。

        delegate_readonly串行执行并保存独立轨迹；read_subagent仅分页读报告。
        子超时或模型失败回传明确错误状态；父取消、父总超时和存储故障继续向上传播。
        """
        parent = self.parent
        if name == "read_subagent":
            if not any(item.run_id == arguments.run_id for item in parent.result.subagents):
                raise SubagentError("subagent_not_found", "该子执行不属于当前运行。")
            return report_page(read_child(parent.runs_root, parent.manifest.run_id, arguments.run_id), arguments.offset, arguments.max_bytes)
        if self.inputs is None or not arguments.title.strip() or not arguments.objective.strip():
            raise SubagentError("invalid_arguments", "委派标题和研究范围不能为空。")
        selected = set(arguments.skill_names)
        if len(selected) != len(arguments.skill_names) or not selected.issubset({item.name for item in self.inputs.skills}):
            raise SubagentError("subagent_skill_unavailable", "子角色只能显式选择父本轮已冻结的技能，名称不能重复。")
        if len(parent.result.subagents) >= 8:
            raise SubagentError("subagent_limit", "单轮最多八次委派，请使用已有报告。")
        linked_task = None  # 只把同会话工作项范围交给子模型，不授予认领或更新权限。
        if arguments.task_id is not None:
            if self.executor.tasks is None:
                raise SubagentError("subagent_task_unavailable", "当前运行没有可关联的任务清单。")
            linked_task = self.executor.tasks.service.get(str(arguments.task_id))  # 同会话领域服务拒绝其他清单编号。
        limits = parent.manifest.config.limits
        available_seconds = max(0.0, parent.deadline - asyncio.get_running_loop().time())
        reserve = {"model_turns": 2.0, "tool_calls": 1.0 if {"write_file", "edit_file", "run_command"}.intersection(self.executor._enabled_names()) else 0.0,
                   "seconds": min(10.0, available_seconds / 4)}  # 保留父执行/总结与一次实际修改空间；不是延长总预算。
        turns = min(arguments.max_model_turns, limits.max_model_turns - parent.result.model_turns - int(reserve["model_turns"]))
        calls = min(arguments.max_tool_calls, limits.max_tool_calls - parent.result.tool_calls - int(reserve["tool_calls"]))
        seconds = min(arguments.timeout_seconds, available_seconds - reserve["seconds"])
        if turns < 1 or calls < 1 or seconds <= 0:
            raise SubagentError("subagent_budget_exhausted", "为父任务保留收尾额度后剩余预算不足，未创建子执行。")
        from agent_harness.runtime.agent import run_agent
        from agent_harness.runtime.runner import AgentStopped, RunContext
        from agent_harness.tools.registry import ToolExecutor

        profile = PROFILES[arguments.role]
        allowed = profile.tools.intersection(self.executor._enabled_names())
        config = parent.manifest.config.model_copy(deep=True)
        config.tools = [tool for tool in config.tools if tool in allowed]
        config.skills = list(arguments.skill_names)
        config.memory.capture, config.auto_repair = False, False
        config.limits = limits.model_copy(update={"max_model_turns": turns, "max_tool_calls": calls, "run_timeout_seconds": seconds})
        config.system_prompt = profile.system_prompt
        planning = self.executor.planning.state if self.executor.planning is not None else parent.manifest.planning
        prompt = json.dumps({"父轮要求": parent.manifest.task.prompt, "批准范围": [plan.model_dump(mode="json") for plan in planning.plans if plan.status == "approved"],
                             "关联工作项": {key: linked_task[key] for key in ("id", "subject", "description", "acceptance")} if linked_task else None,
                             "研究任务": arguments.objective}, ensure_ascii=False)
        manifest = RunManifest(run_id=uuid4(), task=parent.manifest.task.model_copy(update={"prompt": prompt, "verifier": None}),
                               config=config, origin=parent.manifest.origin, planning=planning.model_copy(deep=True),
                               delegation=DelegationOrigin(parent_run_id=parent.manifest.run_id, role=arguments.role, task_id=arguments.task_id))
        child_root = parent.store.run_dir / "subagents"
        if child_root.resolve() != child_root:
            raise StorageError("子执行记录目录身份发生变化。")
        store = RunStore.create(child_root, manifest)
        result = RunResult(run_id=manifest.run_id, execution_status="running", usage_complete=True, artifact_status="complete", planning=manifest.planning)
        now = asyncio.get_running_loop().time()
        child = RunContext(manifest, store, result, min(now + seconds, parent.deadline), started_at=now, record_root=parent.runs_root, budget_parent=parent)
        record = SubagentRecord(run_id=manifest.run_id, title=arguments.title.strip(), objective=arguments.objective,
                                role=arguments.role, task_id=arguments.task_id, skill_names=config.skills, parent_reserve=reserve,
                                max_model_turns=turns, max_tool_calls=calls, timeout_seconds=seconds)
        self._publish(record, child)
        parent.result.subagents.append(record)

        def progress(kind: str, payload: dict) -> None:
            """子kind/payload落盘后发布计数，不把完整子消息灌入父历史；返回None。"""
            self._publish(record, child)

        child.on_event = progress
        parent.active_child = child
        try:
            executor = ToolExecutor(manifest.task.workspace, parent.runs_root, config, python_execution=manifest.task.python_execution, readonly=allowed)
            async with asyncio.timeout_at(child.deadline):
                await run_agent(child, self.client, executor, inherited_context=self.inputs)
            result.execution_status = "completed"
        except AgentStopped as error:
            result.execution_status, result.stop_reason = error.status, error.reason
        except TimeoutError:
            result.execution_status, result.stop_reason, result.usage_complete = "timed_out", "subagent_timeout", False
        except asyncio.CancelledError:
            result.execution_status = "timed_out" if parent.timeout_scope is not None and parent.timeout_scope.expired() else "cancelled"
            result.stop_reason, result.usage_complete = "parent_stopped", False
            raise
        except ModelError as error:
            result.execution_status, result.stop_reason, result.usage_complete = "failed", error.code, False
            child.emit("model_request_failed", {"code": error.code})
        except StorageError:
            raise
        except Exception:
            result.execution_status, result.stop_reason, result.usage_complete = "failed", "subagent_internal_error", False
        finally:
            parent.active_child = None
            if not result.usage_complete:
                parent.result.usage_complete = False
            # 同一协程内无后台命令；父取消时同步封存子终态，然后继续向Runner传播取消。
            if result.execution_status != "running":
                events = read_child(parent.runs_root, parent.manifest.run_id, manifest.run_id).events
                result.subagent_report = finalize_report(result, events)
                result.active_duration_seconds = child.active_duration()
                store.finalize(result)
                child.event_count += 1
                record.status, record.stop_reason = result.execution_status, result.stop_reason
                record.report = result.subagent_report
                self._publish(record, child)
        return {"subagent": record.model_dump(mode="json"),
                "remaining_budget": {"model_turns": limits.max_model_turns - parent.result.model_turns,
                                     "tool_calls": limits.max_tool_calls - parent.result.tool_calls,
                                     "seconds": max(0.0, parent.deadline - asyncio.get_running_loop().time())},
                "notice": "报告属于待核对研究资料，optional不扩大任务范围。请先读取结果，再重新决定后续动作；子用量已计入父总数。"}
