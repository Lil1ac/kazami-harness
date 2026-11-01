"""为上下文边界测试计入当前宿主规划协议与真实工具声明，保持原有历史空间压力。"""

from pathlib import Path

from agent_harness.context.builder import ContextBuilder
from agent_harness.execution.python import prepare_python_execution
from agent_harness.work.planning import PlanningController
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.tools.registry import ToolExecutor
from agent_harness.execution.workspace import Workspace
from agent_harness.work.tasks import WorkStore, TaskController
from agent_harness.memory.service import MemoryController
from agent_harness.subagents.service import SUBAGENT_TOOLS


def initial_input(value: RunManifest, root: Path) -> tuple[ContextBuilder, list[dict], list[dict]]:
    """返回value当前协议下的builder、消息和工具；root允许初始化宿主任务元数据，不创建Run或项目文件。"""
    # 与 Runner 一样先固定解释器，精确边界测试不能使用尚未准备的任务提示代替真实输入。
    execution = value.task.python_execution or prepare_python_execution(value.task.workspace)
    task = value.task.model_copy(update={"python_execution": execution})
    builder = ContextBuilder(Workspace(task.workspace, root), task, value.config)
    executor = ToolExecutor(task.workspace, root, value.config, python_execution=execution)

    def emit(kind: str, payload: dict) -> None:
        """输入尺寸准备不执行工具；kind/payload不会实际调用，返回None。"""

    planning = PlanningController(value.planning, value.task.workspace, emit)
    executor.bind_planning(planning)
    executor.tasks = TaskController(WorkStore(root), value.model_copy(update={"task": task}), planning)
    executor.memory = MemoryController(root, value, emit)
    builder.memory = executor.memory
    builder.tasks = executor.tasks
    messages = builder.initial_messages()
    builder.bind_planning(planning, messages[0]["content"])
    messages[0]["content"] += planning.instructions()
    definitions = executor.definitions
    if {"read_file", "search_files"}.intersection(value.config.tools):
        # 此处只准备输入、不创建Runner或供应商连接；补齐运行时绑定的只读委派声明。
        definitions.extend({"type": "function", "function": {"name": name, "description": description,
                            "parameters": arguments.model_json_schema()}} for name, (arguments, description) in SUBAGENT_TOOLS.items())
    return builder, messages, definitions


def include_planning_overhead(config: RunConfig, workspace: Path, root: Path) -> None:
    """给测试config计入新增固定协议开销，返回None；不调整生产预算，也不放宽历史断言。

    原config的字节预算表示基础文件工具下可用的测试空间；仅增加当前规划、任务和委派系统正文/工具定义的精确
    差额，并换算触发/目标比例，使留给工具历史的绝对空间与原用例一致；workspace只读，root只初始化宿主元数据。
    """
    from uuid import uuid4
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="计算固定协议开销", workspace=workspace, python_execution=prepare_python_execution(workspace)), config=config)
    plain = ContextBuilder(Workspace(workspace, root), manifest.task, config)
    tools = ToolExecutor(workspace, root, config).definitions
    original = plain.build(plain.initial_messages(), tools, 1).check.input_bytes
    builder, messages, current_tools = initial_input(manifest, root)
    overhead = builder.build(messages, current_tools, 1).check.input_bytes - original
    previous = config.context.max_input_bytes
    current = previous + overhead
    config.context.trigger_ratio = (previous * config.context.trigger_ratio + overhead) / current
    config.context.target_ratio = (previous * config.context.target_ratio + overhead) / current
    config.context.max_input_bytes = current
