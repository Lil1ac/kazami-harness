"""公开项目测试的有限修复、同轮预算、历史证据与独立验收边界回归。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from agent_harness.config import ConfigurationError
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runner import run_task
from agent_harness.schemas import RunConfig, RunLimits, RunManifest, TaskSpec, VerifierSpec
from agent_harness.sessions import history_messages
from agent_harness.store import RunStore, StorageError


CHECK_CODE = "import os; from pathlib import Path; value=(Path(os.environ['HARNESS_WORKSPACE'])/'answer.txt').read_text(); print('observed: '+value); assert value=='good', 'answer must be good'"  # 公开测试只检查真实文件，不由模型报告通过。


class RepairModel:
    """每个修复阶段通过真实命令写values对应内容；可在修复阶段挂起以验证取消。"""

    def __init__(self, values: list[str], wait_repair: bool = False) -> None:
        """values依次表示首次/修复后的文件内容，超出长度沿用末项；wait_repair仅供取消同步。"""
        self.values = values  # 每阶段预定输出，不直接写文件。
        self.wait_repair = wait_repair  # True时在首次收到本轮修复反馈后等待取消。
        self.entered = asyncio.Event()  # 已进入修复模型调用，不依靠轮询文件判断。
        self.requests: list[list[dict]] = []  # 实际模型输入深副本，用于核对会话没有重建。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收真实messages/tools，阶段内执行一次命令后回答；返回响应，不连接外部模型。"""
        self.requests.append(deepcopy(messages))
        users = [index for index, message in enumerate(messages) if message['role'] == 'user']
        initial = max(index for index in users if not messages[index]['content'].startswith('[宿主公开项目测试反馈'))
        attempt = sum(messages[index]['content'].startswith('[宿主公开项目测试反馈') for index in users if index > initial)
        boundary = users[-1]
        if any(message['role'] == 'tool' for message in messages[boundary + 1:]):
            return ModelResponse(message={'role': 'assistant', 'content': f'阶段{attempt}完成'}, tool_calls=[], finish_reason='stop', response_model='offline-repair')
        if attempt and self.wait_repair:
            self.entered.set()
            await asyncio.Event().wait()
        value = self.values[min(attempt, len(self.values) - 1)]
        program = "from pathlib import Path; Path('answer.txt').write_text(" + repr(value) + ",encoding='utf-8')"
        call = ToolCall(uuid4().hex, 'run_command', json.dumps({'argv': [sys.executable, '-c', program]}))
        return ModelResponse(message={'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json},
        }]}, tool_calls=[call], finish_reason='tool_calls', response_model='offline-repair')


def repair_manifest(workspace: Path, *, enabled: bool = True, kind: str = 'project_tests', **limits) -> RunManifest:
    """返回workspace的公开测试任务；enabled开关和limits覆盖总预算，kind可验证私有检查边界。"""
    return RunManifest(run_id=uuid4(), task=TaskSpec(prompt='生成符合公开测试的文件', workspace=workspace,
        verifier=VerifierSpec(kind=kind, argv=[sys.executable, '-c', CHECK_CODE], cwd=workspace)),
        config=RunConfig(auto_repair=enabled, tools=['run_command'], limits=RunLimits(**limits)))


@pytest.mark.parametrize('values,enabled,checks,repairs,status,reason', [
    (['good'], True, 1, 0, 'passed', 'verification_passed'),
    (['bad', 'good'], True, 2, 1, 'passed', 'verification_passed'),
    (['bad', 'bad', 'good'], True, 3, 2, 'passed', 'verification_passed'),
    (['bad'], True, 3, 2, 'failed', 'repair_attempts_exhausted'),
    (['bad'], False, 1, 0, 'failed', 'verification_failed'),
], ids=['first-pass', 'repair-pass', 'second-pass', 'exhausted', 'disabled'])
def test_public_tests_repair_in_one_run_with_fixed_history(tmp_path: Path, values, enabled, checks, repairs, status, reason) -> None:
    """values确定各阶段真实产物；核对检查次数、唯一终态、消息继承和各次输出，不修改验收。"""
    workspace = tmp_path / 'work'
    workspace.mkdir()
    manifest = repair_manifest(workspace, enabled=enabled)
    model = RepairModel(values)
    records = tmp_path / 'runs'
    outcome = asyncio.run(run_task(manifest, records, 'offline-key', model_client=model))
    result = outcome.result
    assert result.execution_status == 'completed' and result.validation_status == status
    assert result.stop_reason == reason and result.repair_attempts == repairs
    assert len(result.verification_attempts) == checks
    assert [item.repair_attempt for item in result.verification_attempts] == list(range(checks))
    assert result.verification == result.verification_attempts[-1].result
    assert result.model_turns == 2 * checks and result.tool_calls == checks
    assert (workspace / 'answer.txt').read_text() == values[-1]
    history = RunStore.read(records, str(manifest.run_id))
    kinds = [event.type for event in history.events]
    assert kinds.count('run_started') == kinds.count('context_prepared') == kinds.count('run_finished') == 1
    assert kinds.count('verification_result') == checks and kinds.count('repair_started') == repairs
    restored = history_messages(history)
    assert restored[-1]['content'] == result.final_answer
    assert sum(item['role'] == 'user' and item['content'].startswith('[宿主公开项目测试反馈') for item in restored) == repairs
    if repairs:
        assert 'answer must be good' in model.requests[2][-1]['content']
        assert any(item['role'] == 'tool' for item in model.requests[2])
    streams = [artifact.path for item in result.verification_attempts for artifact in item.result.artifacts]
    assert len(streams) == len(set(streams)) == checks * 2
    assert all((outcome.run_dir / name).is_file() for name in streams)
    assert history.result == result


@pytest.mark.parametrize('limits,repairs,validation,reason', [
    ({'max_model_turns': 2}, 0, 'failed', 'max_model_turns'),
    ({'max_tool_calls': 1}, 0, 'failed', 'max_tool_calls'),
    ({'max_model_turns': 3}, 1, 'unverified', 'max_model_turns'),
], ids=['model-before-repair', 'tool-before-repair', 'model-during-repair'])
def test_repair_uses_remaining_budget_and_invalidates_previous_check(tmp_path: Path, limits, repairs, validation, reason) -> None:
    """limits约束整个任务；修改后额度耗尽不能把旧失败检查冒充当前代码结果，也不重置计数。"""
    workspace = tmp_path / 'work'
    workspace.mkdir()
    outcome = asyncio.run(run_task(repair_manifest(workspace, **limits), tmp_path / 'runs', 'key', model_client=RepairModel(['bad', 'good'])))
    result = outcome.result
    assert result.repair_attempts == repairs and result.validation_status == validation and result.stop_reason == reason
    assert len(result.verification_attempts) == 1 and result.verification_attempts[0].result.status == 'failed'
    assert (result.verification is None) == (repairs > 0)
    assert result.model_turns <= limits.get('max_model_turns', 20)
    assert result.tool_calls <= limits.get('max_tool_calls', 40)


def test_private_verification_cannot_enable_repair_or_leak_feedback(tmp_path: Path) -> None:
    """独立检查的自动修复在副作用前拒绝；关闭修复时正常验收且输出不会进入模型上下文。"""
    model = RepairModel(['bad'])
    with pytest.raises(ConfigurationError, match='公开项目测试'):
        asyncio.run(run_task(repair_manifest(tmp_path, kind='independent'), tmp_path / 'runs', 'key', model_client=model))
    assert not model.requests and not (tmp_path / 'runs').exists()
    result = asyncio.run(run_task(repair_manifest(tmp_path, kind='independent', enabled=False), tmp_path / 'runs', 'key', model_client=model)).result
    assert result.validation_status == 'failed' and result.repair_attempts == 0
    assert all('observed:' not in json.dumps(request) for request in model.requests)


def test_cancel_during_repair_keeps_failed_check_without_claiming_current_validation(tmp_path: Path) -> None:
    """修复模型调用挂起后取消；保留首轮失败与产物，当前代码维持未验收，返回真实取消状态。"""
    workspace = tmp_path / 'work'
    workspace.mkdir()
    model = RepairModel(['bad', 'good'], wait_repair=True)

    async def scenario():
        """在修复请求已开始的同步点取消，返回Runner的唯一收尾结果。"""
        task = asyncio.create_task(run_task(repair_manifest(workspace), tmp_path / 'runs', 'key', model_client=model))
        await asyncio.wait_for(model.entered.wait(), 10)
        task.cancel()
        return await task

    result = asyncio.run(scenario()).result
    assert result.execution_status == 'cancelled' and result.validation_status == 'unverified'
    assert result.verification is None and result.repair_attempts == 1
    assert len(result.verification_attempts) == 1 and result.verification_attempts[0].result.status == 'failed'
    assert any(item.path == 'answer.txt' for item in result.artifacts)


def test_verifier_error_does_not_trigger_repair(tmp_path: Path) -> None:
    """公开测试异常退出2属于环境/启动错误，不把它当断言失败反复修代码。"""
    manifest = repair_manifest(tmp_path)
    manifest.task.verifier.argv = [sys.executable, '-c', 'raise SystemExit(2)']
    result = asyncio.run(run_task(manifest, tmp_path / 'runs', 'key', model_client=RepairModel(['bad']))).result
    assert result.validation_status == 'error' and result.repair_attempts == 0
    assert len(result.verification_attempts) == 1


def test_failed_repair_event_write_prevents_next_model_and_tool(tmp_path: Path, monkeypatch) -> None:
    """修复开始事件写入失败时不能执行下一次模型或命令，保持唯一写入者与真实磁盘失败。"""
    append = RunStore.append

    def failing_append(self, event_type, payload=None):
        """除修复开始外原样提交event_type/payload；该事件抛StorageError，不调度后续副作用。"""
        if event_type == 'repair_started':
            raise StorageError('test repair event write failure')
        return append(self, event_type, payload)

    monkeypatch.setattr(RunStore, 'append', failing_append)
    model = RepairModel(['bad', 'good'])
    with pytest.raises(StorageError):
        asyncio.run(run_task(repair_manifest(tmp_path), tmp_path / 'runs', 'key', model_client=model))
    assert len(model.requests) == 2 and (tmp_path / 'answer.txt').read_text() == 'bad'


def test_total_timeout_during_repair_does_not_reset_deadline(tmp_path: Path) -> None:
    """修复请求挂起仍受原任务2秒总时限，保留前次失败但不把其作为当前检查。"""
    workspace = tmp_path / 'work'
    workspace.mkdir()
    result = asyncio.run(run_task(repair_manifest(workspace, run_timeout_seconds=2.0), tmp_path / 'runs', 'key',
                                  model_client=RepairModel(['bad','good'], wait_repair=True))).result
    assert result.execution_status == 'timed_out' and result.stop_reason == 'run_timeout'
    assert result.repair_attempts == 1 and result.verification is None and result.validation_status == 'unverified'
    assert len(result.verification_attempts) == 1


def test_cancel_second_check_keeps_both_real_process_results(tmp_path: Path) -> None:
    """第二次公开检查真实阻塞时取消，保留首次失败及本次取消清理，不再调度修复。"""
    workspace = tmp_path / 'work'
    workspace.mkdir()
    signal = tmp_path / 'checking.flag'
    manifest = repair_manifest(workspace)
    manifest.task.verifier.argv = [sys.executable, '-c',
        "import os,time; from pathlib import Path; value=(Path(os.environ['HARNESS_WORKSPACE'])/'answer.txt').read_text(); "
        "print('checking '+value,flush=True); "
        + "(Path(" + repr(str(signal)) + ").write_text('started'),time.sleep(60)) if value=='good' else None; assert value=='good'"]

    async def scenario():
        """等待第二次真实检查写入专用信号后取消任务，返回完成清理的Runner结果。"""
        task = asyncio.create_task(run_task(manifest, tmp_path / 'runs', 'key', model_client=RepairModel(['bad','good'])))
        async with asyncio.timeout(10):
            while not signal.exists():
                await asyncio.sleep(.01)
        task.cancel()
        return await task

    result = asyncio.run(scenario()).result
    assert result.execution_status == 'completed' and result.validation_status == 'error'
    assert result.verification.cancelled and result.verification.cleanup_status == 'complete'
    assert [item.result.status for item in result.verification_attempts] == ['failed','error']
    assert result.repair_attempts == 1 and result.model_turns == 4


def test_cli_enables_public_test_repair_and_shows_each_check(tmp_path: Path, monkeypatch, capsys) -> None:
    """从CLI明确开启公开测试修复，正常退出并保存两次检查；独立验收无需改为公开默认。"""
    from agent_harness import cli
    workspace = tmp_path / 'work'
    workspace.mkdir()
    profile = tmp_path / 'harness.toml'
    profile.write_text('tools=["run_command"]\n', encoding='utf-8')
    verifier = tmp_path / 'checks.toml'
    verifier.write_text('kind="project_tests"\nargv='+json.dumps([sys.executable,'-c',CHECK_CODE])+'\n', encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-cli-repair')

    async def offline_run(manifest, runs_root, api_key, **kwargs):
        """保留CLI的实际配置和信号上下文，仅注入会修复真实文件的离线模型，返回Runner结果。"""
        return await run_task(manifest, runs_root, api_key, model_client=RepairModel(['bad','good']), **kwargs)

    monkeypatch.setattr(cli, 'run_task', offline_run)
    assert cli.main(['run','--task','生成文件','--workspace',str(workspace),'--config',str(profile),
                     '--verifier',str(verifier),'--runs-dir',str(tmp_path/'runs'),'--auto-repair','--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['repair_attempts'] == 1 and [a['result']['status'] for a in result['verification_attempts']] == ['failed','passed']
