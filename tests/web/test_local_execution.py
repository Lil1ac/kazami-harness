"""原目录默认执行、连续对话与逐轮固定证据的真实文件/Git/命令回归。"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class LocalModel:
    """使用实际文件工具和命令，在当前目录既有文件后追加最新用户要求。"""

    def __init__(self) -> None:
        """创建空命令输出记录，供断言真实进程使用的工作目录。"""
        self.command_directories: list[str] = []  # 每轮run_command真实打印的cwd。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按最新用户消息读、写、执行cwd命令，再回答；供应商离线，工具真实执行。"""
        last = max(index for index, item in enumerate(messages) if item['role'] == 'user')
        observations = [json.loads(item['content']) for item in messages[last + 1:] if item['role'] == 'tool']
        assert all(item['ok'] for item in observations), observations
        if not observations:
            name, arguments = 'read_file', {'path': 'result.txt'}
        elif len(observations) == 1:
            name, arguments = 'write_file', {'path': 'result.txt', 'content': observations[0]['data']['content'] + messages[last]['content'] + '\n', 'overwrite': True}
        elif len(observations) == 2:
            name, arguments = 'run_command', {'argv': [sys.executable, '-c', 'from pathlib import Path; print(Path.cwd())']}
        else:
            self.command_directories.append(observations[-1]['data']['stdout'].strip())
            return ModelResponse(message={'role': 'assistant', 'content': '已修改当前项目文件。'}, tool_calls=[], finish_reason='stop', response_model='offline-local')
        call = ToolCall(uuid4().hex, name, json.dumps(arguments, ensure_ascii=False))
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': call.call_id, 'type': 'function', 'function': {'name': name, 'arguments': call.arguments_json}}]}
        return ModelResponse(message=message, tool_calls=[call], finish_reason='tool_calls', response_model='offline-local')


@asynccontextmanager
async def client_for(config: WebConfig, model: LocalModel):
    """启动真实服务生命周期，产出同源HTTP客户端与调度器；退出等待任务清理。"""
    app = create_app(config, RunConfig(tools=['read_file', 'write_file', 'run_command']), config.project_dir / 'runs', model_client=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', trust_env=False) as client:
            yield client, app.state.manager


async def finish(client: httpx.AsyncClient, response: httpx.Response) -> dict:
    """核对接纳响应，在30秒内等待执行和差异收尾，返回本轮实际视图。"""
    assert response.status_code == 202, response.text
    async with asyncio.timeout(30):
        while True:
            view = (await client.get('/api/runs/' + response.json()['run_id'])).json()
            if not view['active']:
                assert view['result']['execution_status'] == 'completed', view
                return view
            await asyncio.sleep(.01)


def configuration(tmp_path: Path, kind: str) -> tuple[WebConfig, Path]:
    """建立真实普通/Git目录，Git额外保留已暂存与未暂存内容；返回宿主配置和目录。"""
    selected = tmp_path / '自己选的 项目'
    selected.mkdir()
    (selected / 'result.txt').write_text('existing\n', encoding='utf-8')
    presets = []
    if kind == 'project':
        for argv in (['init', '-q'], ['config', 'core.autocrlf', 'false'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'baseline']):
            subprocess.run(['git', *argv], cwd=selected, capture_output=True, check=True, timeout=15)
        (selected / 'staged.txt').write_text('用户已经暂存\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'staged.txt'], cwd=selected, capture_output=True, check=True)
        (selected / 'result.txt').write_text('用户未提交修改\n', encoding='utf-8')
        presets = [ProjectPreset(id='code', label='原目录项目', path=selected, test_argv=[sys.executable, '-c', 'from pathlib import Path; assert Path.cwd() == Path(r"{workspace}"); assert "first" in Path("result.txt").read_text(encoding="utf-8")'])]
    return WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / 'runtime/copies', frontend_dist=tmp_path / 'dist', projects=presets), selected


@pytest.mark.parametrize('kind', ['workspace', 'project'])
def test_registered_folder_defaults_local_and_continues_after_restart(tmp_path: Path, monkeypatch, kind: str):
    """所选目录用于真实工具/命令/测试；外部编辑可接续，旧产物/差异/记录保持原字节。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, kind)
    model = LocalModel()
    index = (selected / '.git/index').read_bytes() if kind == 'project' else None
    initial = (selected / 'result.txt').read_text(encoding='utf-8')

    async def scenario():
        """串行执行两轮、重启并执行第三轮；同时核对保留/丢弃不能作用于原目录。"""
        async with client_for(config, model) as (client, manager):
            identifier = manager.catalog.register(selected)
            source = {'project_id' if kind == 'project' else 'workspace_id': identifier}
            first = await finish(client, await client.post('/api/runs', json={'prompt': 'first', **source}))
            assert first['execution_mode'] == 'local' and Path(first['workspace']) == selected
            assert Path(first['source_path']) == selected
            assert first['project'] is None
            assert first['local_changes']['status'] == 'ready', first['local_changes']
            assert first['local_changes']['files'] == [{'path': 'result.txt', 'status': 'modified', 'binary': False}]
            assert model.command_directories == [str(selected)]
            assert first['result']['validation_status'] == ('passed' if kind == 'project' else 'unverified')
            assert (selected / 'result.txt').read_text(encoding='utf-8') == initial + 'first\n'
            first_content = (selected / 'result.txt').read_bytes().decode('utf-8')  # 固定原字节，保留Windows原有换行。
            evidence = {name: hashlib.sha256((manager.runs_root / first['run_id'] / name).read_bytes()).hexdigest() for name in ('run.json', 'events.jsonl', 'summary.json')}
            old_diff = (await client.get('/api/runs/' + first['run_id'] + '/diff')).json()
            assert '+first' in old_diff['content']
            (selected / 'result.txt').write_text(initial + 'first\n外部编辑\n', encoding='utf-8')
            second = await finish(client, await client.post('/api/runs/' + first['run_id'] + '/followups', json={'prompt': 'second', 'request_id': str(uuid4())}))
            assert second['workspace'] == first['workspace'] and second['execution_mode'] == 'local'
            assert (selected / 'result.txt').read_text(encoding='utf-8') == initial + 'first\n外部编辑\nsecond\n'
            artifact = await client.get('/api/runs/' + first['run_id'] + '/artifacts', params={'location': 'workspace', 'path': 'result.txt', 'preview': True})
            assert artifact.status_code == 200, artifact.text
            assert artifact.json()['content'] == first_content
            assert (await client.get('/api/runs/' + first['run_id'] + '/diff')).json() == old_diff
            assert evidence == {name: hashlib.sha256((manager.runs_root / first['run_id'] / name).read_bytes()).hexdigest() for name in evidence}
            assert (await client.post('/api/runs/' + second['run_id'] + '/review', json={'decision': 'discard'})).status_code == 404
            assert selected.exists() and not config.worktrees_root.exists() and not manager.projects.worktrees_root.exists()
        async with client_for(config, model) as (client, manager):
            assert (await client.get('/api/runs/' + second['run_id'] + '/conversation')).json()['can_continue']
            third = await finish(client, await client.post('/api/runs/' + second['run_id'] + '/followups', json={'prompt': 'third', 'request_id': str(uuid4())}))
            assert third['turn_index'] == 3 and third['workspace'] == str(selected)
            assert model.command_directories == [str(selected)] * 3
            assert (selected / 'result.txt').read_text(encoding='utf-8').endswith('second\nthird\n')
            listing = (await client.get('/api/conversations', params={'project_id': identifier})).json()
            assert listing['total'] == 1 and listing['items'][0]['turn_count'] == 3
            assert listing['items'][0]['source']['execution_mode'] == 'local'
    asyncio.run(scenario())
    if index is not None:
        assert (selected / '.git/index').read_bytes() == index  # 不自动暂存，也不把用户已暂存内容撤销。
        assert (selected / 'staged.txt').read_text(encoding='utf-8') == '用户已经暂存\n'
        assert subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=selected, capture_output=True, check=True).stdout.strip() == b'1'


@pytest.mark.parametrize('kind', ['workspace', 'project'])
def test_configured_directory_defaults_local_without_catalog_registration(tmp_path: Path, monkeypatch, kind: str):
    """TOML预设与页面选择采用同一目录规则；kind区分普通/Git来源，省略模式不创建副本。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, kind)
    if kind == 'workspace':
        config.workspaces = [WebWorkspace(id='files', label='指定文件夹', path=selected)]

    async def scenario():
        """提交未登记为最近项目的预设，返回后核对实际cwd及原目录中的修改。"""
        model = LocalModel()
        async with client_for(config, model) as (client, manager):
            source = {'project_id': 'code'} if kind == 'project' else {'workspace_id': 'files'}
            first = await finish(client, await client.post('/api/runs', json={'prompt': 'first', **source}))
            assert first['execution_mode'] == 'local' and Path(first['workspace']) == selected
            assert model.command_directories == [str(selected)]
            assert 'first' in (selected / 'result.txt').read_text(encoding='utf-8')
            assert not config.worktrees_root.exists() and not manager.projects.worktrees_root.exists()
    asyncio.run(scenario())


def test_personal_folder_rejects_hidden_copy_execution(tmp_path: Path, monkeypatch):
    """个人文件夹不提供副本执行；显式isolated请求应在模型与目录创建之前失败。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, 'workspace')

    async def scenario():
        """登记真实目录后提交隔离请求，核对原文件、执行次数及副本根目录均未变化。"""
        model = LocalModel()
        async with client_for(config, model) as (client, manager):
            identifier = manager.catalog.register(selected)
            response = await client.post('/api/runs', json={'prompt': 'first', 'workspace_id': identifier, 'execution_mode': 'isolated'})
            assert response.status_code == 400 and response.json()['error']['code'] == 'workspace_copy_not_supported'
            assert not model.command_directories and not config.worktrees_root.exists()
            assert (selected / 'result.txt').read_text(encoding='utf-8') == 'existing\n'
    asyncio.run(scenario())


def test_explicit_worktree_does_not_modify_selected_directory(tmp_path: Path, monkeypatch):
    """只有明确创建Git工作树才更换执行目录；不改写所选仓库的当前文件。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, 'project')
    before = (selected / 'result.txt').read_bytes()

    async def scenario():
        """登记后显式请求isolated，核对来源及实际目录和已保存差异。"""
        async with client_for(config, LocalModel()) as (client, manager):
            identifier = manager.catalog.register(selected)
            first = await finish(client, await client.post('/api/runs', json={'prompt': 'first', 'execution_mode': 'isolated', 'project_id': identifier}))
            assert first['execution_mode'] == 'isolated' and Path(first['workspace']) != selected
            assert first['local_changes'] is None
            assert (selected / 'result.txt').read_bytes() == before
    asyncio.run(scenario())


def test_replaced_directory_is_not_silently_used_by_followup(tmp_path: Path, monkeypatch):
    """同名目录被替换后拒绝接续，不把原会话的写操作发送到新目录。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, 'workspace')
    model = LocalModel()

    async def scenario():
        """首轮结束后替换目录对象，验证继续条件和提交均拒绝，模型调用数保持。"""
        async with client_for(config, model) as (client, manager):
            identifier = manager.catalog.register(selected)
            first = await finish(client, await client.post('/api/runs', json={'prompt': 'first', 'workspace_id': identifier}))
            selected.rename(tmp_path / '原文件保持')
            selected.mkdir()
            (selected / 'result.txt').write_text('无关目录\n', encoding='utf-8')
            view = (await client.get('/api/runs/' + first['run_id'] + '/conversation')).json()
            assert not view['can_continue'] and view['blocked_code'] == 'local_workspace_changed'
            response = await client.post('/api/runs/' + first['run_id'] + '/followups', json={'prompt': 'second', 'request_id': str(uuid4())})
            assert response.status_code == 409 and response.json()['error']['code'] == 'local_workspace_changed'
            assert model.command_directories == [str(selected)]
            assert (selected / 'result.txt').read_text(encoding='utf-8') == '无关目录\n'
    asyncio.run(scenario())


def test_local_report_limit_does_not_force_a_copy_or_block_continuation(tmp_path: Path, monkeypatch):
    """差异采集超限时执行仍在原目录，报告明确失败，后续消息不强制依赖完整副本。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, 'workspace')
    (selected / 'large.bin').write_bytes(b'x' * 256)

    async def scenario():
        """缩小仅本用例的报告预算，验证连续运行和旧产物缺失时的受控反馈。"""
        async with client_for(config, LocalModel()) as (client, manager):
            manager.local_changes.before.MAX_BYTES = 128
            manager.local_changes.after.MAX_BYTES = 128
            identifier = manager.catalog.register(selected)
            first = await finish(client, await client.post('/api/runs', json={'prompt': 'first', 'workspace_id': identifier}))
            assert first['local_changes']['status'] == 'error'
            assert not first['local_changes']['patch_available']
            assert Path(first['workspace']) == selected
            assert (await client.get('/api/runs/' + first['run_id'] + '/conversation')).json()['can_continue']
            artifact = await client.get('/api/runs/' + first['run_id'] + '/artifacts', params={'location': 'workspace', 'path': 'result.txt', 'preview': True})
            assert first['result']['artifact_status'] == 'incomplete' and first['result']['artifact_warnings']
            assert artifact.status_code == 404 and artifact.json()['error']['code'] == 'artifact_not_found'
            second = await finish(client, await client.post('/api/runs/' + first['run_id'] + '/followups', json={'prompt': 'second', 'request_id': str(uuid4())}))
            assert Path(second['workspace']) == selected and second['local_changes']['status'] == 'error'
            assert (selected / 'result.txt').read_text(encoding='utf-8') == 'existing\nfirst\nsecond\n'
    asyncio.run(scenario())


def test_execution_mode_validation_and_saved_diff_integrity(tmp_path: Path, monkeypatch):
    """执行方式只允许枚举且不能在续轮更换；报告被改写后拒绝下载而非返回错误证据。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-local')
    config, selected = configuration(tmp_path, 'project')

    async def scenario():
        """验证拒绝请求不触发运行；完成后修改专属差异文件模拟损坏。"""
        model = LocalModel()
        async with client_for(config, model) as (client, manager):
            identifier = manager.catalog.register(selected)
            for extra in ({'execution_mode': 'other'}, {'execution_mode': 'local', 'base_ref': 'main'}):
                response = await client.post('/api/runs', json={'prompt': 'first', 'project_id': identifier, **extra})
                assert response.status_code == 422
            assert not model.command_directories
            first = await finish(client, await client.post('/api/runs', json={'prompt': 'first', 'project_id': identifier}))
            response = await client.post('/api/runs/' + first['run_id'] + '/followups', json={'prompt': 'second', 'request_id': str(uuid4()), 'execution_mode': 'isolated'})
            assert response.status_code == 422
            assert model.command_directories == [str(selected)]
            patch = manager.local_changes.patch_path(__import__('uuid').UUID(first['run_id']))
            patch.write_bytes(b'incorrect evidence')
            response = await client.get('/api/runs/' + first['run_id'] + '/diff')
            assert response.status_code == 409 and response.json()['error']['code'] == 'local_changes_unreadable'
    asyncio.run(scenario())
