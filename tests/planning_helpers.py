"""为上下文边界测试计入当前宿主规划协议与真实工具声明，保持原有历史空间压力。"""

from pathlib import Path

from agent_harness.context import ContextBuilder
from agent_harness.execution import prepare_python_execution
from agent_harness.planning import PlanningController
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.tools.registry import ToolExecutor
from agent_harness.workspace import Workspace


def initial_input(value: RunManifest, root: Path) -> tuple[ContextBuilder, list[dict], list[dict]]:
    """返回value当前协议下的builder、完整首轮消息和工具；root仅用于边界，不创建运行。"""
    # 与 Runner 一样先固定解释器，精确边界测试不能使用尚未准备的任务提示代替真实输入。
    execution = value.task.python_execution or prepare_python_execution(value.task.workspace)
    task = value.task.model_copy(update={"python_execution": execution})
    builder = ContextBuilder(Workspace(task.workspace, root), task, value.config)
    executor = ToolExecutor(task.workspace, root, value.config, python_execution=execution)

    def emit(kind: str, payload: dict) -> None:
        """输入尺寸准备不执行工具；kind/payload不会实际调用，返回None。"""

    planning = PlanningController(value.planning, value.task.workspace, emit)
    executor.bind_planning(planning)
    messages = builder.initial_messages()
    builder.bind_planning(planning, messages[0]["content"])
    messages[0]["content"] += planning.instructions()
    return builder, messages, executor.definitions


def include_planning_overhead(config: RunConfig, workspace: Path, root: Path) -> None:
    """给测试config计入新增固定协议开销，返回None；不调整生产预算，也不放宽历史断言。

    原config的字节预算表示旧工具下可用的测试空间；仅增加当前新增系统正文/工具定义的精确
    差额，并换算触发/目标比例，使留给工具历史的绝对空间与原用例一致。workspace/root不写入。
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
