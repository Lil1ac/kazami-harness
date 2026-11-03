"""复现当前工作区为副本、Python 却导入宿主已安装源码的问题。"""



import asyncio

import json

from pathlib import Path



import pytest



from tests.helpers.permissions import trusted_config
from agent_harness.schemas import RunConfig

from agent_harness.tools import ToolExecutor
from agent_harness.execution.python import PythonExecutionError, prepare_python_execution
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runtime.runner import run_task
from agent_harness.schemas import RunManifest, TaskSpec, VerifierSpec
from agent_harness.storage.runs import RunStore
from uuid import uuid4
import sys
import shutil





def selected_executor(tmp_path: Path) -> ToolExecutor:

    """在 tmp_path 创建同名包的 src 布局副本并绑定真实进程输出，返回仅启用命令的执行器。"""

    workspace = tmp_path / "selected"

    package = workspace / "src" / "agent_harness"

    package.mkdir(parents=True)

    (package / "__init__.py").write_text("VALUE = 'selected-copy'\n", encoding="utf-8")

    (workspace / "tests").mkdir()

    runs = tmp_path / "runs"

    run_dir = runs / "probe"

    run_dir.mkdir(parents=True)

    executor = ToolExecutor(workspace, runs, trusted_config(tools=["run_command"]))

    executor.bind_run(run_dir)

    return executor





@pytest.mark.parametrize("directory", [".", "tests"])

def test_python_imports_selected_source_without_manual_flags(tmp_path: Path, directory: str) -> None:

    """从根目录或 directory 子目录启动普通 Python；源码必须来自副本，且不向子进程传递密钥。"""

    executor = selected_executor(tmp_path)

    program = (

        "import agent_harness,json,os;"

        "print(json.dumps({'source':agent_harness.__file__,"

        "'value':getattr(agent_harness,'VALUE','host-install'),"

        "'has_key':'DEEPSEEK_API_KEY' in os.environ}))"

    )

    result = asyncio.run(executor.execute(

        "run_command", json.dumps({"argv": ["python", "-c", program], "cwd": directory}),

    ))

    assert result["ok"], result

    data = json.loads(result["data"]["stdout"])

    assert Path(data["source"]).resolve() == executor.workspace / "src/agent_harness/__init__.py"

    assert data["value"] == "selected-copy"

    assert data["has_key"] is False



class CommandModel:
    """仅调用一次真实 Python 命令并保存观察，后续给出最终回答，不访问供应商。"""

    def __init__(self, program: str | None = None) -> None:
        """保存子进程 program；None 时直接完成，用于检查无命令任务的落盘快照。"""
        self.program = program  # 本测试实际执行的 Python 程序；None 不调度工具。
        self.observation: dict | None = None  # 工具结果回传后保存，初始没有结果。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据 messages 中的真实工具结果完成任务；返回模型协议对象，不直接操作工作区。"""
        observed = [item for item in messages if item["role"] == "tool"]
        if self.program is not None and not observed:
            call = ToolCall("probe", "run_command", json.dumps({"argv": ["python", "-c", self.program]}))
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}},
            ]}
            return ModelResponse(message=message, tool_calls=[call], finish_reason="tool_calls", response_model="offline-import-probe")
        if observed:
            self.observation = json.loads(observed[-1]["content"])
        return ModelResponse(message={"role": "assistant", "content": "执行完成"}, tool_calls=[], finish_reason="stop", response_model="offline-import-probe")


@pytest.mark.parametrize("relative", [None, [], ["lib", "."], ["src"]])
def test_snapshot_default_and_explicit_paths(tmp_path: Path, relative: list[str] | None) -> None:
    """relative 为共享配置；None 自动根/src，[] 仅根，显式列表以根优先并去重。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "lib").mkdir()
    snapshot = prepare_python_execution(tmp_path, relative)
    expected = ["src"] if relative is None else [part for part in relative if part != "."]
    assert snapshot.python_paths == [tmp_path, *(tmp_path / part for part in expected)]
    assert snapshot.executable == Path(shutil.which("python") or shutil.which("python3")).resolve()


@pytest.mark.parametrize("relative", ["missing", "../outside", "/outside", " ", "src\x00bad"])
def test_invalid_sources_fail_before_execution(tmp_path: Path, relative: str) -> None:
    """拒绝缺失、越界或无效的显式 relative 源码目录，不回退到宿主的安装包。"""
    with pytest.raises(PythonExecutionError):
        prepare_python_execution(tmp_path, [relative])


def test_deleted_source_refuses_spawn_without_falling_back(tmp_path: Path) -> None:
    """快照固定后源码目录消失时，拒绝启动会写 marker 的程序，并返回明确工具错误。"""
    workspace = tmp_path / "selected"
    (workspace / "src").mkdir(parents=True)
    runs = tmp_path / "runs"
    (runs / "probe").mkdir(parents=True)
    executor = ToolExecutor(workspace, runs, trusted_config(tools=["run_command"]))
    executor.bind_run(runs / "probe")
    (workspace / "src").rmdir()  # 本测试拥有且确认空的目录；不删除其他文件。
    result = asyncio.run(executor.execute("run_command", json.dumps({
        "argv": ["python", "-c", "from pathlib import Path; Path('marker').write_text('wrong')"],
    })))
    assert result["ok"] is False
    assert result["error"]["code"] == "python_execution_changed"
    assert not (workspace / "marker").exists()
    assert not (runs / "probe/artifacts").exists()


@pytest.mark.parametrize("with_verifier", [False, True])
def test_runner_and_private_verifier_import_the_same_project(tmp_path: Path, with_verifier: bool) -> None:
    """执行真实命令及可选私有验收；两者导入副本，私有验收依赖不进入模型命令环境。"""
    selected = selected_executor(tmp_path).workspace
    checks = tmp_path / "private-checks"
    checks.mkdir()
    (checks / "only_verifier.py").write_text("VALUE = 'private'\n", encoding="utf-8")
    program = (
        "import agent_harness,json,importlib.util;"
        "print(json.dumps({'source':agent_harness.__file__,"
        "'value':agent_harness.VALUE,'private_visible':importlib.util.find_spec('only_verifier') is not None}))"
    )
    verifier = VerifierSpec(
        argv=[sys.executable, "-c",
              "import agent_harness,only_verifier; assert agent_harness.VALUE=='selected-copy'; assert only_verifier.VALUE=='private'"],
        cwd=checks, python_paths=[checks],
    ) if with_verifier else None
    model = CommandModel(program)
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="核对实际导入源码", workspace=selected, verifier=verifier), config=trusted_config(tools=["run_command"]))
    outcome = asyncio.run(run_task(manifest, tmp_path / "task-runs", "offline-probe-key", model_client=model))
    assert outcome.result.execution_status == "completed"
    assert outcome.result.validation_status == ("passed" if with_verifier else "unverified")
    observed = json.loads(model.observation["data"]["stdout"])
    assert Path(observed["source"]) == selected / "src/agent_harness/__init__.py"
    assert observed["private_visible"] is False
    history = RunStore.read(tmp_path / "task-runs", str(manifest.run_id))
    assert history.manifest.task.python_execution.python_paths == [selected, selected / "src"]


def test_history_never_rediscovers_current_source_directories(tmp_path: Path) -> None:
    """运行结束后新增 src，不改变已有记录的根目录快照，读取本身不重新准备执行配置。"""
    workspace = tmp_path / "selected"
    workspace.mkdir()
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="只记录环境", workspace=workspace), config=trusted_config())
    asyncio.run(run_task(manifest, tmp_path / "runs", "offline-probe-key", model_client=CommandModel()))
    (workspace / "src").mkdir()
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    assert history.manifest.task.python_execution.python_paths == [workspace]


def test_injected_executor_uses_manifest_source_config(tmp_path: Path) -> None:
    """宿主注入真实执行器时也必须服从当轮清单，不能残留执行器创建时自动选择的 src。"""
    executor = selected_executor(tmp_path)
    selected = executor.workspace
    package = selected / "lib/agent_harness"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'configured-library'\n", encoding="utf-8")
    snapshot = prepare_python_execution(selected, ["lib"])
    task = TaskSpec(prompt="按显式项目配置运行", workspace=selected, python_execution=snapshot)
    model = CommandModel("import agent_harness; print(agent_harness.VALUE)")
    outcome = asyncio.run(run_task(RunManifest(run_id=uuid4(), task=task, config=executor.config),
                                   executor.runs_root, "offline-probe-key", model_client=model, executor=executor))
    assert outcome.result.execution_status == "completed"
    assert model.observation["data"]["stdout"].strip() == "configured-library"
    assert executor.python_execution.python_paths == snapshot.python_paths
    assert executor.python_execution is not snapshot


def test_explicit_project_python_is_used_by_tool_and_verifier(tmp_path: Path) -> None:
    """真实创建与 Harness 分离的虚拟环境；工具和验收均使用该入口及所选源码，无需激活。"""
    import subprocess
    from agent_harness.execution.python import python_command
    from agent_harness.execution.processes import ProcessRunner
    from agent_harness.schemas import RunLimits
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src/probe_module.py").write_text("VALUE = 'selected-source'\n", encoding="utf-8")
    environment = tmp_path / "project-env"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, capture_output=True)
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    snapshot = prepare_python_execution(project, python_executable=str(executable))
    executor = ToolExecutor(project, tmp_path / "runs", trusted_config(tools=["run_command"]), python_execution=snapshot)
    run_dir = tmp_path / "runs/tool"
    run_dir.mkdir(parents=True)
    executor.bind_run(run_dir)
    code = "import json,sys,probe_module; print(json.dumps([sys.executable,probe_module.VALUE]))"
    tool = asyncio.run(executor.execute("run_command", json.dumps({"argv":["python","-c",code]})))
    assert tool["ok"], tool
    assert json.loads(tool["data"]["stdout"]) == [str(executable), "selected-source"]
    process = ProcessRunner(RunLimits(), tmp_path / "verify-artifacts")
    verification = asyncio.run(process.run(["python3", "-c", code], project, python_execution=snapshot))
    assert verification["exit_code"] == 0
    assert json.loads(verification["stdout"]) == [str(executable), "selected-source"]
    assert python_command(["uv", "run", "pytest"], snapshot) == ["uv", "run", "pytest"]
    from agent_harness.config import prepare_task
    verifier_file = tmp_path / "verify.toml"
    verifier_file.write_text("argv = " + json.dumps(["python", "-c", code]) + "\ncwd = " + json.dumps(str(project)), encoding="utf-8")
    task = prepare_task("verify configured Python", project, verifier_file, python_executable=str(executable))
    assert task.verifier.argv[0] == str(executable)
    cli_check = asyncio.run(process.run(task.verifier.argv, project, python_execution=task.python_execution))
    assert cli_check["exit_code"] == 0 and json.loads(cli_check["stdout"]) == [str(executable), "selected-source"]


def test_absent_path_python_does_not_block_other_commands(tmp_path: Path, monkeypatch) -> None:
    """PATH 无 Python 时仍可执行显式程序；只有调用裸 Python 别名才受控失败。"""
    from agent_harness.execution.python import python_command
    from agent_harness.execution.processes import ProcessRunner
    from agent_harness.schemas import RunLimits
    monkeypatch.setenv("PATH", "")
    snapshot = prepare_python_execution(tmp_path)
    assert snapshot.executable is None
    process = ProcessRunner(RunLimits(), tmp_path / "outputs")
    result = asyncio.run(process.run([sys.executable, "-c", "print('other-entry')"], tmp_path, python_execution=snapshot))
    assert result["exit_code"] == 0 and result["stdout"].strip() == "other-entry"
    with pytest.raises(PythonExecutionError, match="PATH"):
        python_command(["python", "-c", "print('must-not-start')"], snapshot)


def test_invalid_explicit_interpreter_refuses_fallback(tmp_path: Path) -> None:
    """显式入口丢失时拒绝准备，不能静默退回 PATH 或 Harness 环境。"""
    with pytest.raises(PythonExecutionError, match="解释器"):
        prepare_python_execution(tmp_path, python_executable=".venv/missing-python")
