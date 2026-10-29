"""自动交付文件的真实命令、逐轮基线、异常和固定下载证据回归。"""

import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from agent_harness.local_execution import LocalChangeStore
from agent_harness.providers.base import ModelError, ModelResponse, ToolCall
from agent_harness.runner import run_task
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.snapshots import FileSnapshotStore
from agent_harness.store import RunStore


class FileCommandModel:
    """首轮实际运行source命令，再按ending完成、失败或等待取消；供应商完全离线。"""

    def __init__(self, source: str, ending: str = 'complete') -> None:
        """固定source的Python程序和ending收尾方式；返回None，构造不执行文件或进程。"""
        self.source = source  # 只交给真实run_command的测试程序。
        self.ending = ending  # complete/failed/cancelled分别触发正常、模型错误、等待取消。
        self.calls = 0  # 本模型的实际调用次数，不用于推算供应商HTTP重试。
        self.entered = asyncio.Event()  # 文件命令已完成后第二次模型调用的同步点。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收真实messages/tools，首轮返回命令，后续按ending给出明确结果；不直接写工作区。"""
        self.calls += 1
        if self.calls == 1:
            call = ToolCall('generate', 'run_command', json.dumps({'argv': [sys.executable, '-c', self.source]}))
            return ModelResponse(message={'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json},
            }]}, tool_calls=[call], finish_reason='tool_calls', response_model='offline-files')
        self.entered.set()
        if self.ending == 'failed':
            raise ModelError('model_transport', '离线模拟连接失败')
        if self.ending == 'cancelled':
            await asyncio.Event().wait()
        return ModelResponse(message={'role': 'assistant', 'content': '文件操作已结束。'}, tool_calls=[], finish_reason='stop', response_model='offline-files')


def file_manifest(workspace: Path) -> RunManifest:
    """为已存在workspace返回只启用真实命令工具的当前格式任务，不声明任何输出文件。"""
    return RunManifest(run_id=uuid4(), task=TaskSpec(prompt='生成报告', workspace=workspace), config=RunConfig(tools=['run_command']))


def test_commands_collect_changed_files_and_publish_fixed_evidence_before_terminal(tmp_path: Path, monkeypatch) -> None:
    """自动发现报告、二进制和既有文件修改，排除未改文件/缓存，删除只入差异；返回None。"""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'dirty.txt').write_bytes(b'user uncommitted')
    (workspace / 'unchanged.txt').write_bytes(b'not a new delivery')
    (workspace / 'deleted.txt').write_bytes(b'delete me')
    namespaces = []  # 记录实际文件快照次数，防止自动收集又复制一次结束目录。
    capture = FileSnapshotStore.capture

    def observed_capture(self, run_id, root):
        """记录run_id在root上的快照命名空间，执行真实capture并原样返回结果。"""
        namespaces.append(self.root.name)
        return capture(self, run_id, root)

    monkeypatch.setattr(FileSnapshotStore, 'capture', observed_capture)
    source = "from pathlib import Path; Path('report.txt').write_bytes(b'report'); Path('dirty.txt').write_bytes(b'changed'); Path('image.bin').write_bytes(b'\\x00\\xff'); Path('deleted.txt').unlink(); Path('__pycache__').mkdir(); Path('__pycache__/cache.pyc').write_bytes(b'cache')"
    manifest = file_manifest(workspace)
    root = tmp_path / 'records'
    outcome = asyncio.run(run_task(manifest, root, 'offline-key', model_client=FileCommandModel(source)))
    assert outcome.result.execution_status == 'completed' and outcome.result.validation_status == 'unverified'
    assert outcome.result.artifact_status == 'complete' and not outcome.result.artifact_warnings
    assert {item.path for item in outcome.result.artifacts if item.location == 'workspace'} == {'report.txt', 'dirty.txt', 'image.bin'}
    assert namespaces == ['.local-before', '.local-after']
    changes = LocalChangeStore(root)
    assert any(item.path == 'deleted.txt' and item.status == 'deleted' for item in changes.summary(manifest.run_id).files)
    saved = changes.after.saved_file(manifest.run_id, 'report.txt')
    (workspace / 'report.txt').write_bytes(b'overwritten later')
    assert saved.read_bytes() == b'report'
    assert changes.after.saved_file(manifest.run_id, 'image.bin').read_bytes() == b'\x00\xff'
    history = RunStore.read(root, str(manifest.run_id))
    assert history.result == outcome.result and history.events[-1].type == 'run_finished'
    assert next(event.seq for event in history.events if event.type == 'artifacts_collected') < history.events[-1].seq
    assert [event.type for event in history.events].count('artifact_registered') == 3


@pytest.mark.parametrize('ending', ['failed', 'cancelled'])
def test_failed_or_cancelled_execution_keeps_actual_generated_files(tmp_path: Path, ending: str) -> None:
    """ending选择模型失败或命令后取消；产物自动保存，原执行状态和未验收结论保持，返回None。"""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    model = FileCommandModel("from pathlib import Path; Path('partial.txt').write_bytes(b'partial')", ending)
    manifest = file_manifest(workspace)

    async def scenario():
        """等待真实文件生成后再模拟ending，返回Runner真实结果，不重新执行命令。"""
        task = asyncio.create_task(run_task(manifest, tmp_path / 'records', 'offline-key', model_client=model))
        if ending == 'cancelled':
            await asyncio.wait_for(model.entered.wait(), 10)
            task.cancel()
        return await task

    outcome = asyncio.run(scenario())
    assert outcome.result.execution_status == ending and outcome.result.validation_status == 'unverified'
    assert outcome.result.artifact_status == 'complete'
    assert {item.path for item in outcome.result.artifacts if item.location == 'workspace'} == {'partial.txt'}
    assert LocalChangeStore(tmp_path / 'records').after.saved_file(manifest.run_id, 'partial.txt').read_bytes() == b'partial'


def test_collection_limit_is_visible_without_changing_execution_result(tmp_path: Path, monkeypatch) -> None:
    """快照预算不足时明确收集不完整，已完成执行保持未验收，运行内命令输出仍登记；返回None。"""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'large.txt').write_bytes(b'123456789')
    monkeypatch.setattr(FileSnapshotStore, 'MAX_BYTES', 8)
    outcome = asyncio.run(run_task(file_manifest(workspace), tmp_path / 'records', 'offline-key',
                                   model_client=FileCommandModel("from pathlib import Path; Path('report.txt').write_bytes(b'ok')")))
    assert outcome.result.execution_status == 'completed' and outcome.result.validation_status == 'unverified'
    assert outcome.result.artifact_status == 'incomplete' and outcome.result.artifact_warnings
    assert not [item for item in outcome.result.artifacts if item.location == 'workspace']
    assert {item.kind for item in outcome.result.artifacts} == {'stdout', 'stderr'}


def test_cli_discovers_command_output_without_declared_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    """从真实CLI参数启动命令产物任务，show读取同一完整结果；返回None，无供应商调用。"""
    from agent_harness import cli
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    profile = tmp_path / 'harness.toml'
    profile.write_text('tools = ["run_command"]\n', encoding='utf-8')
    records = tmp_path / 'records'
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-cli-files')

    async def offline_run(manifest, runs_root, api_key, **kwargs):
        """保留CLI清单、目录、凭据及信号参数，注入只生成report.txt的离线模型，返回真实结果。"""
        return await run_task(manifest, runs_root, api_key, model_client=FileCommandModel(
            "from pathlib import Path; Path('report.txt').write_bytes(b'cli report')"), **kwargs)

    monkeypatch.setattr(cli, 'run_task', offline_run)
    code = cli.main(['run', '--task', '生成报告', '--workspace', str(workspace), '--config', str(profile), '--runs-dir', str(records), '--json'])
    result = json.loads(capsys.readouterr().out)
    assert code == 3 and result['artifact_status'] == 'complete'
    assert {item['path'] for item in result['artifacts'] if item['location'] == 'workspace'} == {'report.txt'}
    assert cli.main(['show', result['run_id'], '--runs-dir', str(records), '--json']) == 0
    history = json.loads(capsys.readouterr().out)
    assert history['artifacts'] == result['artifacts'] and history['artifact_status'] == 'complete'
