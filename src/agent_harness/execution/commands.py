"""Run 所属命令的统一生命周期；事件是事实来源，进程只由创建它的宿主管理。"""

import asyncio
import codecs
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from pydantic import Field

from agent_harness.execution.processes import ProcessRunner
from agent_harness.schemas import DataModel
from agent_harness.storage.runs import StorageError
from agent_harness.work.tasks import TaskError, now

if TYPE_CHECKING:
    from agent_harness.tools.command import RunCommandArguments


class ReadCommandArguments(DataModel):
    """读取本 Run 命令的状态和增量输出，不接受路径或其他运行编号。"""

    command_id: str = Field(min_length=36, max_length=36, description="run_command 返回的本轮命令 UUID。")
    stream: str = Field(default="stdout", pattern="^(stdout|stderr)$", description="stdout 或 stderr，默认标准输出。")
    offset: int = Field(default=0, ge=0, description="原始字节游标，首次为 0；后续原样使用 next_offset。")
    max_bytes: int = Field(default=4096, ge=4, le=65536, description="本页原始输出字节上限，默认 4096。")


class CommandArguments(DataModel):
    """定位当前 Run 所属命令，不能用父或兄弟运行的编号越权控制。"""

    command_id: str = Field(min_length=36, max_length=36, description="本 Run 的命令 UUID，不是系统 PID。")


COMMAND_TOOLS = {
    "read_command": (ReadCommandArguments, "读取本轮命令状态及按字节分页的输出，使用next_offset续读；结束与清理分别展示。"),
    "wait_command": (CommandArguments, "等待本轮后台命令真实结束，不重复启动或请求模型；仍受运行总时限约束。"),
    "stop_command": (CommandArguments, "停止本轮命令并等待进程树清理；幂等，不回滚已发生的文件或外部副作用。"),
}  # 只在本轮启用 run_command 时提供，归属检查由宿主完成。


def output_page(run_dir: Path, record: dict, stream: str, offset: int, max_bytes: int) -> dict:
    """读取 record 固定命令目录的 stream 分页；字节游标范围错误抛 TaskError。

    run_dir 为已核验的日志目录；stream 仅接受双流名，max_bytes 范围 4—65536。
    活动输出末尾不完整 UTF-8 暂留到下一页，任意非 UTF-8 字节用替换字符展示；游标仍按原字节前进。
    """
    if stream not in {"stdout", "stderr"} or not 4 <= max_bytes <= 65536 or offset < 0:
        raise TaskError("command_invalid_cursor", "命令输出分页参数无效。")
    identifier = str(UUID(record["id"]))
    path = run_dir / "artifacts" / "commands" / identifier / (stream + ".bin")
    if path.resolve() != path or not path.resolve().is_relative_to(run_dir.resolve()):
        raise StorageError("命令输出路径身份发生变化。")
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            if offset > size:
                raise TaskError("command_invalid_cursor", "输出游标超出当前已保存字节。")
            handle.seek(offset)
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        if offset:
            raise TaskError("command_invalid_cursor", "命令尚未产生输出。") from None
        raw, size = b"", 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    active = record["status"] in {"starting", "running", "stopping"}
    text = decoder.decode(raw, final=not active and offset + len(raw) == size)
    consumed = len(raw) - len(decoder.getstate()[0])
    return {"content": text, "offset": offset, "next_offset": offset + consumed,
            "size_bytes": size, "eof": offset + consumed == size, "stream": stream}


def command_records(history, *, active: bool = False) -> list[dict]:
    """从 history 事件重建命令；active=False 时将缺终态命令投影为待核对，不修改原记录。"""
    records = {}
    for event in history.events:
        if event.type == "command_snapshot":
            value = deepcopy(event.payload["command"])
            if not active and value["status"] in {"starting", "running", "stopping"}:
                value.update(status="needs_review", cleanup_status="unknown")
            records[value["id"]] = value
    return list(records.values())


class CommandController:
    """一个 Run 的命令句柄、输出及收尾；父子共享目录占用表，但各自只可控制自己的命令。"""

    def __init__(self, context, executor):
        """绑定 context 日志和 executor；返回 None，不启动进程，子项复用根目录占用表。"""
        self.context = context  # 命令所属执行、固定权限与时间预算。
        self.executor = executor  # 复用参数解析和结果、产物、清理聚合。
        self.records: dict[str, dict] = {}  # 本段命令的已发布元数据；历史从事件重建。
        self.jobs: dict[str, asyncio.Task] = {}  # 本宿主实际创建的协程，重启不会补造句柄。
        self.reported: set[str] = set()  # 已投递给模型的完成事件，避免等待回执后再次注入。
        root = context.budget_parent or context
        self.gates = root.command_workspaces  # 规范绝对目录到拥有命令编号的占用表。
        self.workspace = str(context.manifest.task.workspace.resolve())  # 按整个实际工作区保守互斥。

    @property
    def active(self) -> bool:
        """返回是否仍有未完成命令；只读内存，不推测系统 PID 是否存活。"""
        return any(not task.done() for task in self.jobs.values())

    def publish(self, record: dict) -> None:
        """将 record 快照先提交日志，再更新内存；返回 None，存储失败阻止后续副作用。"""
        self.context.emit("command_snapshot", {"command": deepcopy(record)})
        self.records[record["id"]] = deepcopy(record)

    def get(self, identifier: str) -> dict:
        """返回本 Run 的 identifier 元数据副本；未知编号抛 TaskError，不解析任意系统 PID。"""
        if identifier not in self.records:
            raise TaskError("command_not_found", "该命令不属于本轮执行。")
        return deepcopy(self.records[identifier])

    async def start(self, arguments: "RunCommandArguments") -> dict:
        """执行已获原参数授权的 arguments，后台返回句柄，同步等待实际结果。

        同一模型请求及调用编号只接纳一次；目录被活动命令占用时拒绝。副作用包含进程和输出文件。
        """
        key = f"{self.context.request_turn}:{self.executor.call_id}"
        identifier = str(uuid5(self.context.manifest.run_id, key))
        if identifier not in self.records:
            if self.workspace in self.gates:
                raise TaskError("workspace_command_active", "当前目录仍有活动命令，请先等待或停止，再启动命令或修改文件。")
            manifest = self.context.manifest
            record = {"id": identifier, "run_id": str(manifest.run_id),
                      "parent_run_id": str(manifest.delegation.parent_run_id) if manifest.delegation else None,
                      "member_id": manifest.delegation.team_member_id if manifest.delegation else None,
                      "call_id": self.executor.call_id, "model_turn": self.context.request_turn,
                      "workspace": self.workspace, "argv": arguments.argv, "cwd": arguments.cwd,
                      "background": arguments.background, "status": "starting", "pid": None,
                      "created_at": now(), "finished_at": None, "cleanup_status": "not_needed", "result": None}
            self.publish(record)
            self.gates[self.workspace] = identifier  # 在首次 await 前占用，避免同事件循环双重启动。
            self.jobs[identifier] = asyncio.create_task(self._run(identifier, arguments))
        if arguments.background:
            return {"accepted": True, "command": self.get(identifier)}
        return await self.wait(identifier)

    async def _run(self, identifier: str, arguments: "RunCommandArguments") -> dict:
        """运行 identifier 对应 arguments；所有退出均释放目录，结果先落盘再交等待者。"""
        from agent_harness.tools.command import run_command
        record = self.get(identifier)
        runner = ProcessRunner(self.context.manifest.config.limits, self.context.store.run_dir / "artifacts")

        def spawned(pid: int) -> None:
            """记录已取得的 pid；返回 None，写日志失败由进程执行器负责停止和清理。"""
            record.update(pid=pid, status="running")
            self.publish(record)

        try:
            try:
                data = await run_command(self.context.manifest.task.workspace, self.context.runs_root, arguments,
                                         runner, self.executor.python_execution, command_id=identifier, on_spawn=spawned)
            except asyncio.CancelledError as error:
                data = getattr(error, "harness_process_result", None)
                if data is None:
                    record.update(status="cancelled", finished_at=now())
                    self.publish(record)
                    return {"exit_code": None, "cancelled": True, "cleanup_status": "not_needed", "error_code": "command_not_started"}
            record.update(status="cancelled" if data.get("cancelled") else "completed" if data.get("error_code") is None else "failed",
                          result=data, cleanup_status=data["cleanup_status"], finished_at=now())
            self.executor._command_result(data)
            self.publish(record)
            if self.context.hooks is not None:
                self.context.hooks.enqueue("CommandFinished", record["id"], {"command": record})
            return data
        except BaseException:
            # 存储或验证异常不得伪造成命令完成；历史保留最后快照以供重启核对。
            raise
        finally:
            if record.get("cleanup_status") not in {"unknown", "incomplete"}:
                self.gates.pop(self.workspace, None)

    async def wait(self, identifier: str) -> dict:
        """等待 identifier 的既有任务并返回真实结果；调用方取消先收拢进程，再传播取消。"""
        self.get(identifier)
        task = self.jobs[identifier]
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            error.harness_process_result = await self.stop(identifier)
            raise
        self.reported.add(identifier)
        return result

    async def stop(self, identifier: str) -> dict:
        """幂等停止 identifier 并等待同一清理协程；返回真实命令结果，不新增命令。"""
        record = self.get(identifier)
        task = self.jobs[identifier]
        if not task.done() and record["status"] != "stopping":
            record["status"] = "stopping"
            self.publish(record)
            # 先让新协程进入 try/finally；启动前取消也必须保存明确终态。
            await asyncio.sleep(0)
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # 多次取消不会再次打断进程清理。
        self.reported.add(identifier)
        return task.result()

    async def settle(self, *, stop: bool = False) -> None:
        """等待全部已接纳命令；stop=True 先停止，False 自然完成，异常不被吞掉。"""
        for identifier in list(self.jobs):
            if stop:
                await self.stop(identifier)
            else:
                reported = identifier in self.reported
                await self.wait(identifier)
                if not reported:
                    self.reported.discard(identifier)  # 自动等待仍须把完成事实交给下一次模型决策。

    def notifications(self) -> list[dict]:
        """返回尚未报告的结束命令并标记已提取；模型请求失败时事实仍留在历史事件中。"""
        values = []
        for identifier, task in self.jobs.items():
            if task.done() and identifier not in self.reported:
                task.result()  # 先传播后台存储异常，不能带故障继续执行。
                values.append(self.get(identifier))
                self.reported.add(identifier)
        return values
