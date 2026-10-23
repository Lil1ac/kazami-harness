"""真实 ASGI、Runner、文件与 Git 验证多轮交互、幂等和逐轮证据。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import pytest

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.sessions import SessionStore, payload_digest
from agent_harness.snapshots import FileSnapshotStore
from agent_harness.store import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from agent_harness.web.schemas import FollowupRequest


class EditingModel:
    """依据当前用户要求提出真实读写工具调用，捕获历史而不访问网络。"""

    def __init__(self, project: bool = False, gated: bool = False) -> None:
        """project选择代码文件，gated阻塞首轮以测忙碌与取消；初始化空请求历史。"""
        self.project = project  # 代码模式读写app.py，普通模式读写result.txt。
        self.gated = gated  # 首轮可控阻塞，不靠固定睡眠猜测状态。
        self.entered = asyncio.Event()  # 模型真实进入阻塞点后置位。
        self.requests: list[list[dict]] = []  # 每次实际模型调用收到的完整消息副本。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按最后user定位本轮，读取前轮文件后追加修改，返回工具调用或最终回答。"""
        self.requests.append(deepcopy(messages))
        user_index = max(index for index, item in enumerate(messages) if item['role'] == 'user')
        prompt = messages[user_index]['content']
        observations = [json.loads(item['content']) for item in messages[user_index + 1:] if item['role'] == 'tool']
        if self.gated and prompt == 'first':
            self.entered.set()
            await asyncio.Event().wait()
        path = 'app.py' if self.project else 'result.txt'
        calls = []
        if prompt == 'first' and not observations:
            content = 'def add(a, b):\n    """返回两数之和。"""\n    return a + b\n' if self.project else 'first\n'
            calls = [ToolCall('write-' + str(len(self.requests)), 'write_file', json.dumps({'path': path, 'content': content, 'overwrite': True}, ensure_ascii=False))]
        elif prompt != 'first' and not observations:
            calls = [ToolCall('read-' + str(len(self.requests)), 'read_file', json.dumps({'path': path}))]
        elif prompt != 'first' and len(observations) == 1:
            assert observations[0]['ok'], '后续运行没有继承前轮文件'
            old = observations[0]['data']['content']
            assert 'return a + b' in old if self.project else old.startswith('first\n')
            content = old + ('# ' if self.project else '') + prompt + '\n'
            calls = [ToolCall('append-' + str(len(self.requests)), 'write_file', json.dumps({'path': path, 'content': content, 'overwrite': True}, ensure_ascii=False))]
        message = {'role': 'assistant', 'content': None if calls else f'本轮完成：{prompt}', 'reasoning_content': '必须保留的历史字段'}
        if calls:
            message['tool_calls'] = [{'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason='tool_calls' if calls else 'stop', response_model='offline-conversation')


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> WebConfig:
    """创建隔离普通工作区并设置离线凭据，返回真实应用使用的宿主配置。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-conversation-key')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    return WebConfig(project_dir=tmp_path, runtime_root=tmp_path / 'runtime/copies', frontend_dist=tmp_path / 'dist', workspaces=[WebWorkspace(id='files', label='文件', path=workspace)])


@asynccontextmanager
async def client_for(config: WebConfig, model: EditingModel):
    """启动真实应用生命周期并产出HTTP客户端；退出时等待后台任务清理。"""
    app = create_app(config, RunConfig(tools=['read_file', 'write_file']), config.project_dir / 'records', model_client=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', trust_env=False) as client:
            yield client, app


async def finished(client: httpx.AsyncClient, identifier: str) -> dict:
    """最多15秒等待identifier结束执行与快照收尾，返回最终真实视图。"""
    async with asyncio.timeout(15):
        while True:
            response = await client.get(f'/api/runs/{identifier}')
            assert response.status_code == 200, response.text
            view = response.json()
            if not view['active']:
                return view
            await asyncio.sleep(0.01)


async def initial(client: httpx.AsyncClient, project: bool = False) -> dict:
    """提交first要求并等收尾；project选择Git登记项，否则使用普通工作区。"""
    response = await client.post('/api/runs', json={'prompt': 'first', **({'project_id': 'code'} if project else {'workspace_id': 'files'})})
    assert response.status_code == 202, response.text
    return await finished(client, response.json()['run_id'])


async def follow(client: httpx.AsyncClient, parent: dict, prompt: str = 'second', **extra) -> dict:
    """以parent为父提交新prompt，合并extra并等待结束；要求后端接纳为202。"""
    body = {'prompt': prompt, 'request_id': str(uuid4()), **extra}
    response = await client.post(f'/api/runs/{parent["run_id"]}/followups', json=body)
    assert response.status_code == 202, response.text
    return await finished(client, response.json()['run_id'])


def evidence(root: Path, identifier: str) -> dict[str, str]:
    """返回identifier原始三份运行记录的SHA256，供确认追加不会改写父证据。"""
    return {name: hashlib.sha256((root / identifier / name).read_bytes()).hexdigest() for name in ('run.json', 'events.jsonl', 'summary.json')}


def test_file_followup_inherits_history_and_code_without_rewriting_parent(config: WebConfig):
    """完成两轮真实文件修改，验证父文件/事件不变、消息不重复以及请求幂等与线性关系。"""
    async def scenario():
        """顺序提交与重复请求并比较父子结果；所有执行使用真实文件工具。"""
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            first = await initial(client)
            before = evidence(app.state.manager.runs_root, first['run_id'])
            request_id = str(uuid4())
            second = await follow(client, first, request_id=request_id)
            assert second['parent_run_id'] == first['run_id'] and second['turn_index'] == 2
            assert second['session_id'] == first['run_id']
            assert Path(first['workspace']) != Path(second['workspace'])
            assert (Path(first['workspace']) / 'result.txt').read_text(encoding='utf-8') == 'first\n'
            assert (Path(second['workspace']) / 'result.txt').read_text(encoding='utf-8') == 'first\nsecond\n'
            assert evidence(app.state.manager.runs_root, first['run_id']) == before
            messages = model.requests[-1]
            assert sum(item['role'] == 'system' for item in messages) == 1
            assert [item['content'] for item in messages if item['role'] == 'user'] == ['first', 'second']
            assert any(item.get('reasoning_content') == '必须保留的历史字段' for item in messages)
            count = len(model.requests)
            again = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': request_id, 'prompt': 'second'})
            assert again.status_code == 202 and again.json()['run_id'] == second['run_id']
            assert len(model.requests) == count
            conflict = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': request_id, 'prompt': 'different'})
            assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'request_conflict'
            stale = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': str(uuid4()), 'prompt': 'third'})
            assert stale.status_code == 409 and stale.json()['error']['code'] == 'session_stale_parent'
            conversation = (await client.get(f'/api/runs/{first["run_id"]}/conversation')).json()
            assert len(conversation['turns']) == 2 and conversation['latest_run_id'] == second['run_id'] and conversation['can_continue']
    asyncio.run(scenario())


def test_session_survives_restart_and_does_not_duplicate_inherited_history(config: WebConfig):
    """跨真实应用重启读取两轮会话并追加第三轮，重试原请求不调用新模型。"""
    async def scenario():
        """关闭首应用后用新模型实例继续同一记录，检查三条user没有重复增长。"""
        request_id = str(uuid4())
        async with client_for(config, EditingModel()) as (client, app):
            first = await initial(client)
            second = await follow(client, first, request_id=request_id)
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            again = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': request_id, 'prompt': 'second'})
            assert again.status_code == 202 and again.json()['run_id'] == second['run_id'] and model.requests == []
            third = await follow(client, second, 'third')
            assert third['turn_index'] == 3
            assert (Path(third['workspace']) / 'result.txt').read_text(encoding='utf-8') == 'first\nsecond\nthird\n'
            assert [item['content'] for item in model.requests[-1] if item['role'] == 'user'] == ['first', 'second', 'third']
            view = (await client.get(f'/api/runs/{third["run_id"]}/conversation')).json()
            assert len(view['turns']) == 3
    asyncio.run(scenario())


def test_changed_file_workspace_is_rejected_before_new_model(config: WebConfig):
    """父工作区被外部修改后拒绝继承，不调用模型且不改旧运行证据。"""
    async def scenario():
        """更改实际父文件后提交，比较模型次数与原始日志摘要。"""
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            first = await initial(client)
            before = evidence(app.state.manager.runs_root, first['run_id'])
            (Path(first['workspace']) / 'result.txt').write_text('external')
            count = len(model.requests)
            response = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': str(uuid4()), 'prompt': 'second'})
            assert response.status_code == 409 and response.json()['error']['code'] == 'workspace_snapshot_stale'
            assert len(model.requests) == count and evidence(app.state.manager.runs_root, first['run_id']) == before
    asyncio.run(scenario())


def test_followup_budget_rejects_before_model_and_preserves_files(config: WebConfig):
    """继承历史后的输入仍受本轮预算约束，超限0模型/工具且父结果与派生文件保留。"""
    async def scenario():
        """将新轮预算降至1字节，读取真实结果及快照确认未发请求。"""
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            first = await initial(client)
            count = len(model.requests)
            second = await follow(client, first, config={'context': {'max_input_bytes': 1}})
            assert second['result']['execution_status'] == 'limit_exceeded'
            assert second['result']['stop_reason'] == 'max_model_input_bytes'
            assert second['result']['model_turns'] == second['result']['tool_calls'] == second['result']['http_attempts'] == 0
            assert len(model.requests) == count
            assert (Path(second['workspace']) / 'result.txt').read_text(encoding='utf-8') == 'first\n'
            history = RunStore.read(app.state.manager.runs_root, second['run_id'])
            assert any(event.type == 'history_loaded' for event in history.events)
    asyncio.run(scenario())


def test_snapshot_quota_does_not_change_completed_run(config: WebConfig, monkeypatch):
    """普通工作区超出快照上限仍保留已完成结果，只把继续能力标为不可用。"""
    monkeypatch.setattr(FileSnapshotStore, 'MAX_BYTES', 64)
    (config.workspaces[0].path / 'large.bin').write_bytes(b'x' * 65)
    async def scenario():
        """等待快照收尾后读取会话可继续原因，不把快照限制伪装为执行失败。"""
        async with client_for(config, EditingModel()) as (client, app):
            first = await initial(client)
            assert first['result']['execution_status'] == 'completed'
            conversation = (await client.get(f'/api/runs/{first["run_id"]}/conversation')).json()
            assert not conversation['can_continue'] and conversation['blocked_code'] == 'workspace_snapshot_limit'
    asyncio.run(scenario())


def test_busy_and_concurrent_duplicate_submission(config: WebConfig):
    """相同追加请求同时发送只运行一次，运行中其他要求被拒绝，保留单槽语义。"""
    async def scenario():
        """用真实并发HTTP请求竞争准备流程，检查返回同一编号且只新增一个运行。"""
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            first = await initial(client)
            body = {'request_id': str(uuid4()), 'prompt': 'second'}
            results = await asyncio.gather(*[client.post(f'/api/runs/{first["run_id"]}/followups', json=body) for _ in range(2)])
            assert all(result.status_code == 202 for result in results), [result.text for result in results]
            assert results[0].json()['run_id'] == results[1].json()['run_id']
            await finished(client, results[0].json()['run_id'])
            assert len([path for path in app.state.manager.runs_root.iterdir() if path.name[0] != '.']) == 2
        blocking = EditingModel(gated=True)
        async with client_for(config, blocking) as (client, app):
            response = await client.post('/api/runs', json={'prompt': 'first', 'workspace_id': 'files'})
            identifier = response.json()['run_id']
            await asyncio.wait_for(blocking.entered.wait(), 5)
            busy = await client.post(f'/api/runs/{identifier}/followups', json={'request_id': str(uuid4()), 'prompt': 'second'})
            assert busy.status_code == 409 and busy.json()['error']['code'] == 'run_busy'
            await client.post(f'/api/runs/{identifier}/cancel')
            await finished(client, identifier)
    asyncio.run(scenario())


@pytest.mark.parametrize('committed', [False, True])
def test_uncertain_submission_is_not_replayed_after_restart(config: WebConfig, committed: bool):
    """准备或提交后崩溃遗留请求无对应运行时，重新读取/提交均不重放模型。"""
    async def scenario():
        """构造持久化意图后重新启动应用，确认提示待核对且模型请求为空。"""
        async with client_for(config, EditingModel()) as (client, app):
            first = await initial(client)
            request = FollowupRequest(request_id=uuid4(), prompt='second')
            digest = payload_digest({'parent_run_id': first['run_id'], **request.model_dump(mode='json', exclude={'request_id'})})
            store = SessionStore(app.state.manager.runs_root)
            store.begin(UUID(first['run_id']), request.request_id, UUID(first['run_id']), uuid4(), digest, request.prompt)
            if committed:
                store.commit(UUID(first['run_id']), request.request_id)
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            view = (await client.get(f'/api/runs/{first["run_id"]}/conversation')).json()
            assert not view['can_continue']
            response = await client.post(f'/api/runs/{first["run_id"]}/followups', json=request.model_dump(mode='json'))
            assert response.status_code == 409 and response.json()['error']['code'] == 'request_uncertain'
            assert model.requests == []
    asyncio.run(scenario())


def git(root: Path, *args: str) -> bytes:
    """在临时root运行固定Git参数并返回stdout；不使用shell，失败即终止测试。"""
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, timeout=10).stdout


@pytest.fixture
def project_config(config: WebConfig) -> WebConfig:
    """创建真实Git基线和独立宿主测试，同时保留源目录未提交说明供检查。"""
    root = config.project_dir / 'source'
    root.mkdir()
    (root / 'app.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    (root / 'AGENTS.md').write_text('函数提供中文注释。', encoding='utf-8')
    (root / 'check.py').write_text('from app import add\nassert add(2,3)==5\nprint("actual project test passed")\n', encoding='utf-8')
    (root / '.gitignore').write_text('__pycache__/\n', encoding='utf-8')
    git(root, 'init', '-q')
    git(root, 'config', 'core.autocrlf', 'false')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=HarnessTest', '-c', 'user.email=test@localhost', 'commit', '-qm', 'baseline')
    (root / 'local.txt').write_text('保留的未提交说明', encoding='utf-8')
    config.projects = [ProjectPreset(id='code', label='代码', path=root, test_argv=[sys.executable, 'check.py'])]
    return config


@pytest.mark.parametrize('autocrlf', ['false', 'true'])
def test_project_followup_isolated_with_fresh_tests_and_review(project_config: WebConfig, autocrlf: str):
    """两轮真实Git改码各自测试与审阅，继承完整补丁且保留父证据和源目录。"""
    git(project_config.projects[0].path, 'config', 'core.autocrlf', autocrlf)
    async def scenario():
        """保留首轮再追加，比较父子补丁与真实代码，之后丢弃父轮不影响子工作树。"""
        model = EditingModel(project=True)
        source = project_config.projects[0].path
        before_source = git(source, 'status', '--porcelain=v1')
        async with client_for(project_config, model) as (client, app):
            first = await initial(client, project=True)
            assert first['result']['validation_status'] == 'passed', first
            before = evidence(app.state.manager.runs_root, first['run_id'])
            patch_before = (await client.get(f'/api/runs/{first["run_id"]}/patch')).content
            kept = await client.post(f'/api/runs/{first["run_id"]}/review', json={'decision': 'keep'})
            assert kept.status_code == 200
            second = await follow(client, first)
            assert second['result']['validation_status'] == 'passed', second
            assert second['project']['status'] == 'pending' and second['project']['snapshot_status'] == 'ready'
            parent = (await client.get(f'/api/runs/{first["run_id"]}')).json()
            assert parent['project']['status'] == 'kept'
            assert evidence(app.state.manager.runs_root, first['run_id']) == before
            assert (await client.get(f'/api/runs/{first["run_id"]}/patch')).content == patch_before
            assert b'second' not in patch_before and b'second' in (await client.get(f'/api/runs/{second["run_id"]}/patch')).content
            assert '# second' not in (Path(first['workspace']) / 'app.py').read_text(encoding='utf-8')
            assert '# second' in (Path(second['workspace']) / 'app.py').read_text(encoding='utf-8')
            assert first['workspace'] != second['workspace']
            discarded = await client.post(f'/api/runs/{first["run_id"]}/review', json={'decision': 'discard'})
            assert discarded.status_code == 200 and Path(second['workspace']).is_dir()
            assert git(source, 'status', '--porcelain=v1') == before_source
            assert 'a - b' in (source / 'app.py').read_text(encoding='utf-8')
    asyncio.run(scenario())


def test_changed_or_discarded_project_blocks_followup(project_config: WebConfig):
    """父Git代码变化或最新工作树已丢弃时，追加在新模型调用前被拒绝。"""
    async def scenario():
        """修改父工作树触发快照过期，恢复后丢弃，再检查明确拒绝原因。"""
        model = EditingModel(project=True)
        async with client_for(project_config, model) as (client, app):
            first = await initial(client, project=True)
            path = Path(first['workspace']) / 'app.py'
            original = path.read_bytes()
            path.write_bytes(original + b'# external\n')
            count = len(model.requests)
            response = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': str(uuid4()), 'prompt': 'second'})
            assert response.status_code == 409 and response.json()['error']['code'] == 'project_snapshot_stale'
            assert len(model.requests) == count
            path.write_bytes(original)
            response = await client.post(f'/api/runs/{first["run_id"]}/review', json={'decision': 'discard'})
            assert response.status_code == 200
            response = await client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id': str(uuid4()), 'prompt': 'second'})
            assert response.status_code == 409 and response.json()['error']['code'] == 'project_already_decided'
            assert len(model.requests) == count
    asyncio.run(scenario())


def test_disconnected_followup_preparation_keeps_one_background_run(config: WebConfig, monkeypatch):
    """HTTP调用方断开不取消追加准备，重连同请求仍只执行一次。"""
    import threading
    entered, release = threading.Event(), threading.Event()
    restore = FileSnapshotStore.restore

    def gated_restore(self, run_id, workspace, destination):
        """在实际复制前等待受控释放，模拟慢工作区准备；等待最多五秒。"""
        entered.set()
        assert release.wait(5)
        return restore(self, run_id, workspace, destination)
    monkeypatch.setattr(FileSnapshotStore, 'restore', gated_restore)

    async def scenario():
        """取消HTTP等待后释放真实准备，检查会话唯一新轮与幂等重连。"""
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            first = await initial(client)
            body = {'request_id':str(uuid4()), 'prompt':'second'}
            pending = asyncio.create_task(client.post(f'/api/runs/{first["run_id"]}/followups', json=body))
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            release.set()
            created = await asyncio.wait_for(asyncio.shield(app.state.manager._submission), 10)
            second = await finished(client, str(created.run_id))
            before = len(model.requests)
            again = await client.post(f'/api/runs/{first["run_id"]}/followups', json=body)
            assert again.status_code == 202 and again.json()['run_id'] == second['run_id']
            assert len(model.requests) == before
            assert (Path(second['workspace']) / 'result.txt').read_text(encoding='utf-8') == 'first\nsecond\n'
    asyncio.run(scenario())


def test_shutdown_handles_failed_followup_preparation(config: WebConfig, monkeypatch):
    """服务关闭等待中的追加准备失败时仍正常收尾，不能遗留执行或抛出未处理会话异常。"""
    import threading
    from agent_harness.snapshots import SnapshotError
    entered, release = threading.Event(), threading.Event()

    def failing_restore(self, run_id, workspace, destination):
        """受控等待后报告来源变化，验证关停路径确实观察到准备异常。"""
        entered.set()
        assert release.wait(5)
        raise SnapshotError('workspace_snapshot_stale', '工作区发生变化。')
    monkeypatch.setattr(FileSnapshotStore, 'restore', failing_restore)

    async def scenario():
        """先建立可信首轮，再于准备中发起关停，确认没有后续模型调用。"""
        model = EditingModel()
        async with client_for(config, model) as (client, app):
            first = await initial(client)
            count = len(model.requests)
            pending = asyncio.create_task(client.post(f'/api/runs/{first["run_id"]}/followups', json={'request_id':str(uuid4()),'prompt':'second'}))
            assert await asyncio.to_thread(entered.wait, 5)
            shutdown = asyncio.create_task(app.state.manager.shutdown())
            await asyncio.sleep(0)
            release.set()
            response = await pending
            assert response.status_code == 409
            await asyncio.wait_for(shutdown, 5)
            assert app.state.manager.busy_run_id is None and len(model.requests) == count
    asyncio.run(scenario())
