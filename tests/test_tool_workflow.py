"""验证四工具真实文件与子进程副作用，以及预算、取消和产物记录的集成边界。"""

import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agent_harness.config import ConfigurationError
from agent_harness.runner import exit_code, run_task
from agent_harness.schemas import ArtifactRef, RunConfig, RunLimits, RunManifest, TaskSpec
from agent_harness.store import RunStore, StorageError
from agent_harness.tools import ToolExecutor


TOOLS = ["read_file", "search_files", "write_file", "run_command"]


def _manifest(workspace: Path, *, expected=None, **limits) -> RunManifest:
    """创建启用四工具并允许覆盖预算的任务，方便外部行为集成测试。"""
    return RunManifest(run_id=uuid4(), task=TaskSpec(prompt="分析 app.log 并生成 report.json", workspace=workspace, expected_outputs=expected or []), config=RunConfig(tools=TOOLS, limits=RunLimits(**limits)))


def _call(identifier: str, name: str, arguments: dict) -> dict:
    """构造供应商工具调用，保持参数为原始 JSON 字符串。"""
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def _reply(*calls: dict, answer: str | None = None) -> dict:
    """构造单候选模型响应，不混入真实网络依赖。"""
    message = {"role": "assistant", "content": answer}
    if calls:
        message["tool_calls"] = list(calls)
    return {"model": "offline-deepseek", "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _transport(replies: list[dict], requests: list) -> httpx.MockTransport:
    """按固定顺序提供模型响应并捕获每次请求，用来检查工具配对和停止调度。"""
    def handler(request):
        """记录发送给供应商的消息体，再返回下一项固定响应。"""
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(requests) - 1])
    return httpx.MockTransport(handler)


def test_four_tool_workflow_generates_script_and_report(tmp_path: Path):
    """通过真实本地文件与Python程序完成搜索、读、写、运行和产物回读闭环。"""
    (tmp_path / "app.log").write_text("ERROR code=timeout\nWARN code=timeout\nERROR code=auth\n", encoding="utf-8")
    script = "import json,re\nfrom pathlib import Path\nfrom collections import Counter\ncounts=Counter(re.search(r'code=(\\w+)',line).group(1) for line in Path('app.log').read_text().splitlines() if line.startswith('ERROR'))\nPath('report.json').write_text(json.dumps({'counts':dict(counts),'total':sum(counts.values())}),encoding='utf-8')\nprint('report written')\n"
    replies = [
        _reply(_call("search", "search_files", {"query": "ERROR", "glob": "*.log"}), _call("read", "read_file", {"path": "app.log"})),
        _reply(_call("write", "write_file", {"path": "analyze_logs.py", "content": script})),
        _reply(_call("run", "run_command", {"argv": ["python", "analyze_logs.py"]})),
        _reply(_call("report", "read_file", {"path": "report.json"})),
        _reply(answer="已生成 report.json，ERROR 共2条。"),
    ]
    requests = []
    value = _manifest(tmp_path, expected=["report.json"])
    outcome = asyncio.run(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport(replies, requests)))
    assert outcome.result.execution_status == "completed"
    assert outcome.result.validation_status == "unverified"
    assert exit_code(outcome.result) == 3
    assert json.loads((tmp_path / "report.json").read_text()) == {"counts": {"timeout": 1, "auth": 1}, "total": 2}
    assert outcome.result.tool_calls == 5
    assert outcome.result.model_turns == 5
    paths = {(item.location, item.path) for item in outcome.result.artifacts}
    assert ("workspace", "analyze_logs.py") in paths
    assert ("workspace", "report.json") in paths
    assert len([item for item in outcome.result.artifacts if item.location == "run"]) == 2
    for artifact in outcome.result.artifacts:
        base = tmp_path if artifact.location == "workspace" else outcome.run_dir
        assert (base / artifact.path).is_file()
    assert [message["tool_call_id"] for message in requests[1]["messages"][-2:]] == ["search", "read"]
    history = RunStore.read(tmp_path / ".runs", str(value.run_id))
    assert history.result == outcome.result
    assert [event.type for event in history.events].count("run_finished") == 1


@pytest.mark.parametrize("tool_name", ["write_file", "run_command"])
def test_storage_failure_before_tool_start_prevents_actual_side_effect(tmp_path: Path, monkeypatch, tool_name: str):
    """开始事件写失败时不能写文件或启动能够修改文件的命令。"""
    original = RunStore.append

    def fail_start(self, kind, payload=None):
        """仅让工具开始事件失败，其他真实日志操作保持不变。"""
        if kind == "tool_started":
            raise StorageError("模拟写入失败")
        return original(self, kind, payload)

    monkeypatch.setattr(RunStore, "append", fail_start)
    arguments = {"path": "marker.txt", "content": "wrong"} if tool_name == "write_file" else {"argv": ["python", "-c", "from pathlib import Path; Path('marker.txt').write_text('wrong')"]}
    with pytest.raises(StorageError):
        asyncio.run(run_task(_manifest(tmp_path), tmp_path / ".runs", "dummy-key", transport=_transport([_reply(_call("one", tool_name, arguments))], [])))
    assert not (tmp_path / "marker.txt").exists()


def test_bad_cleanup_stops_batch_and_cannot_be_downgraded(tmp_path: Path):
    """当前真实结果为清理不完整时，即使执行器随后声称正常也必须停止且保留最坏状态。"""
    class InconsistentExecutor:
        """模拟执行器内部状态被错误重置，用于验证上层的最坏状态保护。"""
        definitions = []
        cleanup_status = "complete"
        calls = 0

        async def execute(self, name, arguments_json):
            """返回一个清理不完整的工具结果，并观察是否误调度同批第二项。"""
            self.calls += 1
            return {"ok": False, "data": {"cleanup_status": "incomplete", "exit_code": 0}, "error": {"code": "command_cleanup_failed", "message": "清理不完整"}, "truncated": False}

    executor = InconsistentExecutor()
    value = _manifest(tmp_path)
    requests = []
    response = _reply(_call("one", "run_command", {"argv": ["python", "-c", "pass"]}), _call("two", "write_file", {"path": "wrong.txt", "content": "wrong"}))
    outcome = asyncio.run(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport([response], requests), executor=executor))
    assert outcome.result.execution_status == "failed"
    assert outcome.result.stop_reason == "process_cleanup_failed"
    assert outcome.result.cleanup_status == "incomplete"
    assert executor.calls == len(requests) == 1
    history = RunStore.read(tmp_path / ".runs", str(value.run_id))
    results = [event.payload for event in history.events if event.type == "tool_result"]
    assert [item["call_id"] for item in results] == ["one", "two"]
    assert results[1]["result"]["error"]["code"] == "skipped_cleanup"


def test_cancelled_command_keeps_real_output_and_cleanup(tmp_path: Path):
    """取消真实命令时先完成进程清理，保留输出并跳过未执行的同批写入。"""
    value = _manifest(tmp_path)
    requests = []
    command = {"argv": ["python", "-c", "from pathlib import Path; import time; print('ready',flush=True); Path('ready.txt').write_text('ready'); time.sleep(20)"]}
    replies = [_reply(_call("command", "run_command", command), _call("later", "write_file", {"path": "must-not-exist.txt", "content": "wrong"}))]

    async def scenario():
        """等待子进程真正开始运行再取消任务，避免只测试取消请求阶段。"""
        task = asyncio.create_task(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport(replies, requests)))
        async with asyncio.timeout(5):
            while not (tmp_path / "ready.txt").exists():
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    outcome = asyncio.run(scenario())
    assert outcome.result.execution_status == "cancelled"
    assert outcome.result.cleanup_status == "complete"
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert len(requests) == 1
    history = RunStore.read(tmp_path / ".runs", str(value.run_id))
    results = [event.payload for event in history.events if event.type == "tool_result"]
    assert len(results) == 2
    assert results[0]["result"]["data"]["cancelled"] is True
    assert "ready" in results[0]["result"]["data"]["stdout"]
    assert results[1]["result"]["error"]["code"] == "skipped_cancelled"
    assert len(outcome.result.artifacts) == 2
    assert [event.type for event in history.events].count("run_finished") == 1


def test_total_timeout_preserves_command_cleanup(tmp_path: Path):
    """任务总时限耗尽后终止活动命令，真实清理结果仍进入最终摘要。"""
    value = _manifest(tmp_path, run_timeout_seconds=0.25, command_timeout_seconds=10.0)
    replies = [_reply(_call("one", "run_command", {"argv": ["python", "-c", "import time; print('started',flush=True); time.sleep(10)"]}))]
    outcome = asyncio.run(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport(replies, [])))
    assert outcome.result.execution_status == "timed_out"
    assert outcome.result.stop_reason == "run_timeout"
    assert outcome.result.cleanup_status == "complete"
    assert exit_code(outcome.result) == 124


def test_command_timeout_is_returned_to_model_after_cleanup(tmp_path: Path):
    """单条命令超时但清理完成时，模型可读取失败结果再结束，不伪造任务总超时。"""
    value = _manifest(tmp_path, command_timeout_seconds=0.15)
    requests = []
    replies = [_reply(_call("one", "run_command", {"argv": ["python", "-c", "import time;time.sleep(5)"]})), _reply(answer="命令已超时，未生成产物。")]
    outcome = asyncio.run(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport(replies, requests)))
    assert outcome.result.execution_status == "completed"
    assert outcome.result.cleanup_status == "complete"
    data = json.loads(requests[1]["messages"][-1]["content"])
    assert data["error"]["code"] == "command_timeout"
    assert data["data"]["timed_out"] is True


def test_missing_expected_output_is_visible_without_claiming_validation(tmp_path: Path):
    """模型声称结束但未生成声明文件时，在结果和事件中提示缺失，验收仍未验证。"""
    value = _manifest(tmp_path, expected=["report.json"])
    outcome = asyncio.run(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport([_reply(answer="结束")], [])))
    assert outcome.result.validation_status == "unverified"
    assert outcome.result.missing_artifacts == ["report.json"]
    history = RunStore.read(tmp_path / ".runs", str(value.run_id))
    assert any(event.type == "artifact_missing" for event in history.events)


@pytest.mark.parametrize("relative", ["../outside.json", "records/report.json"])
def test_invalid_expected_output_rejected_before_model(tmp_path: Path, relative: str):
    """拒绝越界或位于运行记录目录的声明产物，且不创建半启动记录。"""
    with pytest.raises(ConfigurationError):
        asyncio.run(run_task(_manifest(tmp_path, expected=[relative]), tmp_path / "records", "dummy-key"))
    assert not (tmp_path / "records").exists()


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/outside", "."])
def test_artifact_reference_requires_scoped_relative_path(path: str):
    """产物索引不能包含绝对路径或父级路径，供后续前端按范围访问。"""
    with pytest.raises(ValidationError):
        ArtifactRef(location="workspace", path=path)


def test_command_cwd_and_disabled_tool_are_rejected(tmp_path: Path):
    """工具只按配置启用，命令目录不能越界，参数错误不创建子进程。"""
    read_only = ToolExecutor(tmp_path, tmp_path / ".runs", RunConfig())
    assert asyncio.run(read_only.execute("write_file", '{"path":"x","content":"x"}'))["error"]["code"] == "unknown_tool"
    tool = ToolExecutor(tmp_path, tmp_path / ".runs", RunConfig(tools=TOOLS))
    data = asyncio.run(tool.execute("run_command", json.dumps({"argv": ["python", "-c", "pass"], "cwd": str(tmp_path)})))
    assert data["error"]["code"] == "invalid_arguments"


def test_invalid_tool_artifact_does_not_commit_duplicate_result(tmp_path: Path):
    """内部工具给出越界产物引用时，只提交一次受控错误结果，不保存坏引用。"""
    class MalformedExecutor:
        """模拟工具结果构造缺陷，验证日志提交前的结果校验。"""
        definitions = []

        async def execute(self, name, arguments_json):
            """返回结构合法但位置越界的产物，触发宿主校验失败。"""
            return {"ok": True, "data": {}, "artifact_refs": [{"location": "run", "path": "../outside", "kind": "file"}]}

    value = _manifest(tmp_path)
    replies = [_reply(_call("one", "write_file", {"path": "a", "content": "a"}))]
    outcome = asyncio.run(run_task(value, tmp_path / ".runs", "dummy-key", transport=_transport(replies, []), executor=MalformedExecutor()))
    assert outcome.result.execution_status == "failed"
    history = RunStore.read(tmp_path / ".runs", str(value.run_id))
    results = [event.payload for event in history.events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0]["result"]["error"]["code"] == "tool_internal_error"
    assert outcome.result.artifacts == []
