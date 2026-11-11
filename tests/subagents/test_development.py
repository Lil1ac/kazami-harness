"""真实HTTP、Git、审批与进程验证写入委派；模型离线，仅在测试目录产生副作用。"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.providers.base import ToolCall
from agent_harness.runtime.runner import REQUEST_CONTEXT
from agent_harness.schemas import RunConfig, RunLimits
from tests.helpers.models import configured_run
from agent_harness.web.api import create_app
from agent_harness.work.tasks import WorkStore
from tests.subagents.test_parallel import response
from tests.subagents.test_subagents import report
from tests.web.test_projects import project_config
from tests.web.test_planning_api import decision
from tests.web.job_helpers import submit_run, submit_followup
from tests.web.test_integrations_api import integration_finished, action


def tool(name: str, **arguments) -> ToolCall:
    """用name与arguments生成唯一调用，返回供应商格式；不执行任何工具。"""
    return ToolCall(str(uuid4()), name, json.dumps(arguments, ensure_ascii=False))


class DevelopmentModel:
    """先提交方案，再创建独立工作项并委派开发；子项仅在其工作树修改指定文件。"""

    def __init__(self, command: bool = False, outside: bool = False):
        """command=True以需审批命令写文件；outside=True模拟命令越过委派范围；不启动执行。"""
        self.command = command  # 是否覆盖真实进程审批路径。
        self.outside = outside  # 命令可越过文件工具范围，宿主必须在成果阶段发现。
        self.requests = []  # 各请求的实际运行编号，核对拒绝时零模型与预算归属。
        self.entered = set()  # 同时启动的开发子编号。
        self.gate = asyncio.Event()  # 两个独立子请求同时进入后才放行，证明真实并行。

    async def complete(self, messages, tools):
        """按当前真实Run上下文和messages回执返回下一动作；tools用于核对子项没有递归或审批权。"""
        context = REQUEST_CONTEXT.get()
        self.requests.append(str(context.manifest.run_id))
        calls = [message for message in messages if message['role'] == 'tool']
        if context.manifest.delegation is not None:
            assert not {'delegate_develop', 'delegate_readonly', 'task_update', 'submit_plan'}.intersection(item['function']['name'] for item in tools)
            name = json.loads(context.manifest.task.prompt)['研究任务']
            if not calls:
                self.entered.add(str(context.manifest.run_id))
                if len(self.entered) >= 2:
                    self.gate.set()
                await self.gate.wait()
                return response(calls=[tool('read_file', path=name)])
            if len(calls) == 1:
                result = json.loads(calls[-1]['content'])
                assert result['ok'], result
                if name == 'src/app.py':
                    assert '999' in result['data']['content']  # 必须包含父目录未提交代码。
                content = 'def add(a, b):\n    return a + b\n' if name == 'src/app.py' else '前端已实现\n'
                if self.command:
                    target = 'unexpected.txt' if self.outside else name
                    program = f'from pathlib import Path; Path({target!r}).write_text({content!r}, encoding="utf-8")'
                    return response(calls=[tool('run_command', argv=[sys.executable, '-c', program])])
                return response(calls=[tool('write_file', path=name, content=content, overwrite=True)])
            return response(report('完成指定文件修改，待父工作区集成。'))
        if context.manifest.planning.phase == 'planning':
            return response(calls=[tool('submit_plan', title='前后端协作开发', summary='分离开发并逐项集成',
                scope=['src/app.py', 'ui.txt'], steps=['交付前后端功能'], verification=['集成后核对代码和检查'])])
        # 工具消息本身没有name，从对应assistant调用读取真实工具名称。
        names = [call['function']['name'] for message in messages for call in message.get('tool_calls', [])]
        state = WorkStore(context.runs_root).snapshot(str(context.manifest.session_id))
        tasks = [item for item in state['items'] if item['subject'] in ['src/app.py', 'ui.txt']]
        if not tasks:
            return response(calls=[tool('task_create', subject=name, description=name, acceptance=['完成指定文件修改']) for name in ['src/app.py', 'ui.txt']])
        if not context.result.subagents:
            return response(calls=[tool('delegate_develop', title=item['subject'], objective=item['subject'], task_id=item['id'],
                write_scope=[item['subject']], background=True, max_model_turns=5, max_tool_calls=6) for item in tasks])
        if context.subagent_controller.active:
            return response(calls=[tool('wait_subagents', timeout_seconds=60)])
        return response('已收回子成果，等待用户审阅采纳；项目尚未完成最终验收。')


async def finished(client, run_id: str) -> dict:
    """等待run_id实际收尾并返回视图；最多60秒，覆盖两个工作树完整基线校验而非普通单任务超时。"""
    async with asyncio.timeout(60):
        while True:
            result = await client.get(f"/api/runs/{run_id}")
            assert result.status_code == 200, result.text
            view = result.json()
            if not view['active']:
                return view
            await asyncio.sleep(.03)


async def approved_run(client, execution_mode: str = "local", wait: bool = True) -> dict:
    """通过client提交本机规划并明确批准，返回开发轮真实终态；不绕过计划或动作审批API。"""
    first = await submit_run(client, json={'prompt': '规划后并行实现前后端', 'project_id': 'backend', 'execution_mode': execution_mode, 'mode': 'plan'})
    assert first.status_code == 202, first.text
    plan = await finished(client, first.json()['run_id'])
    assert plan['result']['stop_reason'] == 'plan_approval', plan['result']
    if execution_mode == 'isolated':
        workspace = Path(plan['workspace'])
        (workspace / 'src/app.py').write_text('def add(a, b):\n    return 999\n', encoding='utf-8')
        (workspace / 'ui.txt').write_text('父目录未跟踪前端\n', encoding='utf-8')
    result = await submit_followup(client, f"/api/runs/{plan['run_id']}/followups", json=decision(plan, 'approve'))
    assert result.status_code == 202, result.text
    return await finished(client, result.json()['run_id']) if wait else result.json()


def create_development_app(project_config, monkeypatch, model):
    """以测试project_config和离线model创建真实应用；禁用项目自动检查以单独测试人工集成复验。"""
    monkeypatch.setenv('HARNESS_TEST_MODEL_KEY', 'offline-development')
    repository = project_config.projects[0].path
    (repository / 'ui.txt').write_text('父目录未跟踪前端\n', encoding='utf-8')
    project_config.projects[0].test_argv = []
    config = configured_run(tools=['read_file', 'search_files', 'write_file', 'edit_file', 'run_command'],
                       limits=RunLimits(max_model_turns=30, max_tool_calls=80, run_timeout_seconds=180))
    return create_app(project_config, config, project_config.project_dir / 'records', model_client=model)


@pytest.mark.parametrize("execution_mode", ["local", "isolated"])
def test_parallel_development_dirty_baseline_and_parent_integration(project_config, monkeypatch, execution_mode):
    """双子真正并行且看到dirty起点；固定差异不重复带入旧改动，人工采纳保留暂存区和历史。"""
    model = DevelopmentModel()
    app = create_development_app(project_config, monkeypatch, model)

    async def scenario():
        """执行前端共用HTTP协议，返回None；只检查测试仓库和宿主持久证据。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client, execution_mode)
            assert view['result']['execution_status'] == 'completed', view['result']
            records = view['result']['subagents']
            assert len(records) == 2 and model.gate.is_set(), view['result']
            assert all(item['status'] == 'completed' and item['snapshot_status'] == 'ready' for item in records), records
            assert len({item['workspace'] for item in records}) == 2
            parent = Path(view['workspace'])
            assert '999' in (parent / 'src/app.py').read_text()
            assert (parent / 'ui.txt').read_text(encoding='utf-8') == '父目录未跟踪前端\n'
            from tests.web.test_projects import git
            index_path = Path(git(parent, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
            index = index_path.read_bytes()
            for record in records:
                assert record['changed_paths'] == [record['objective']]
                detail = await client.get(f"/api/runs/{view['run_id']}/subagents/{record['run_id']}")
                assert detail.status_code == 200, detail.text
                patch = await client.get(f"/api/runs/{view['run_id']}/subagents/{record['run_id']}/diff")
                assert patch.status_code == 200 and record['objective'] in patch.json()['content']
                from urllib.parse import urlencode
                from agent_harness.desktop.host import DesktopHost
                from agent_harness.desktop.storage import PreferenceStore
                from tests.desktop.test_plan_export_integration import NativeDownload
                preferences = PreferenceStore(project_config.project_dir / 'desktop')
                preferences.update(downloads_directory=project_config.project_dir / 'downloads')
                native = NativeDownload()
                host = DesktopHost(preferences, native, 'offline-token')
                host.manager = app.state.manager
                detail = await client.get(f"/api/runs/{view['run_id']}/subagents/{record['run_id']}")
                assert detail.status_code == 200, detail.text
                delivery = detail.json()
                assert delivery['project']['run_id'] == record['run_id']
                assert delivery['artifact_status'] == 'complete'
                assert delivery['artifact_warnings'] == []
                assert any(item['path'] == record['objective'] for item in delivery['artifacts'])
                url = f"/api/runs/{record['run_id']}/artifacts?" + urlencode({'location': 'workspace', 'path': record['objective'], 'parent_run_id': view['run_id']})
                download = await client.get(url)
                assert download.status_code == 200, download.text
                fixed = await host.artifact(url)
                assert fixed.read_bytes() == download.content
                await host.download(fixed)
                assert native.calls[-1][1]['directory'] == str(project_config.project_dir / 'downloads')
                patch_url = f"/api/runs/{record['run_id']}/patch?parent_run_id={view['run_id']}"
                whole = await client.get(patch_url)
                assert whole.status_code == 200 and (await host.artifact(patch_url)).read_bytes() == whole.content
                targets = await client.get(f"/api/runs/{record['run_id']}/integration-targets", params={'parent_run_id': view['run_id']})
                assert targets.status_code == 200, targets.text
                identifier = str(uuid4())
                result = await client.post(f"/api/runs/{record['run_id']}/integrations", json={'request_id': identifier, 'parent_run_id': view['run_id'], 'target_id': targets.json()['items'][0]['id']})
                assert result.status_code == 202, result.text
                value = await integration_finished(client, identifier)
                assert value['status'] == 'ready', value
                assert (await action(client, value, 'apply')).status_code == 202
                value = await integration_finished(client, identifier)
                assert value['status'] == 'applied', value
                discarded = await client.post(f"/api/runs/{view['run_id']}/subagents/{record['run_id']}/review", json={'decision': 'discard'})
                assert discarded.status_code == 200, discarded.text
                assert not Path(record['workspace']).exists()
                assert (await client.get(url)).content == download.content
                assert (await client.get(patch_url)).content == whole.content
            assert 'a + b' in (parent / 'src/app.py').read_text()
            assert (parent / 'ui.txt').read_text(encoding='utf-8') == '前端已实现\n'
            assert index_path.read_bytes() == index
            if execution_mode == 'local':
                assert (parent / 'untracked.txt').exists()
            else:
                assert '999' in (project_config.projects[0].path / 'src/app.py').read_text()
    asyncio.run(scenario())


@pytest.mark.parametrize('choice', ['approve', 'reject', 'cancel'])
def test_child_approval_durable_continuation_without_replay(project_config, monkeypatch, choice):
    """子命令审批持久化；approve接续原动作一次，reject/cancel零模型工具，兄弟审批仍可处理。"""
    model = DevelopmentModel(command=True)
    app = create_development_app(project_config, monkeypatch, model)

    async def scenario():
        """通过HTTP逐次处理子项决定，核对活动预算与旧事件字节不变，返回None。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            assert view['result']['execution_status'] == 'waiting_input', view['result']
            assert len(view['result']['subagents']) == 2
            before_requests = len(model.requests)
            old_path = project_config.project_dir / 'records' / view['run_id'] / 'events.jsonl'
            before = old_path.read_bytes()
            pending = view['result']['permissions']['pending']
            assert pending['child_run_id'] and pending['source_run_id'] == view['run_id']
            body = {'request_id': str(uuid4()), 'prompt': '处理子动作', 'action_decision': {'action_id': pending['id'], 'fingerprint': pending['fingerprint'], 'action': choice}}
            result = await submit_followup(client, f"/api/runs/{view['run_id']}/followups", json=body)
            assert result.status_code == 202, result.text
            following = await finished(client, result.json()['run_id'])
            assert following['result']['execution_status'] in {'waiting_input', 'completed', 'cancelled'}, following['result']
            if choice != 'approve':
                assert len(model.requests) == before_requests
                assert following['result']['tool_calls'] == following['result']['model_turns'] == 0
            else:
                assert following['result']['permissions']['spent_model_turns'] == view['result']['model_turns']
                actions = (await client.get(f"/api/runs/{following['run_id']}/permissions")).json()['items']
                assert next(item for item in actions if item['id'] == pending['id'])['status'] == 'executed'
                for _ in range(4):
                    if following['result']['execution_status'] != 'waiting_input':
                        break
                    next_action = following['result']['permissions']['pending']
                    assert next_action is not None, following['result']
                    approve = {'request_id': str(uuid4()), 'prompt': '批准剩余子动作', 'action_decision': {'action_id': next_action['id'], 'fingerprint': next_action['fingerprint'], 'action': 'approve'}}
                    accepted = await submit_followup(client, f"/api/runs/{following['run_id']}/followups", json=approve)
                    assert accepted.status_code == 202, accepted.text
                    following = await finished(client, accepted.json()['run_id'])
                assert following['result']['execution_status'] == 'completed', following['result']
                assert all(item['status'] == 'completed' for item in following['result']['subagents']), following['result']['subagents']
                actions = (await client.get(f"/api/runs/{following['run_id']}/permissions")).json()['items']
                assert sum(item['status'] == 'executed' for item in actions) == 2
            assert old_path.read_bytes() == before
            repeated = await submit_followup(client, f"/api/runs/{view['run_id']}/followups", json=body)
            assert repeated.status_code == 202 and repeated.json()['run_id'] == result.json()['run_id']
    asyncio.run(scenario())



def test_command_scope_violation_blocks_source_adoption(project_config, monkeypatch):
    """本机命令越过文件范围时真实差异包含越界路径，父目录保持且子成果不得采纳。"""
    model = DevelopmentModel(command=True, outside=True)
    app = create_development_app(project_config, monkeypatch, model)
    app.state.manager.run_config.permission_policy.command = 'allow'  # 仅测试宿主允许命令，不改变产品默认。

    async def scenario():
        """执行开发后核对宿主范围检查及HTTP拒绝，返回None。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            assert len(view['result']['subagents']) == 2
            for record in view['result']['subagents']:
                assert record['status'] == 'failed' and record['stop_reason'] == 'subagent_scope_violation', record
                assert record['scope_violations'] == ['unexpected.txt']
                result = await client.get(f"/api/runs/{record['run_id']}/integration-targets", params={'parent_run_id': view['run_id']})
                assert result.status_code == 409
            assert not (project_config.projects[0].path / 'unexpected.txt').exists()
    asyncio.run(scenario())


class BlockingDevelopmentModel(DevelopmentModel):
    """两个子项启动真实长命令，供HTTP验证单项停止与父取消的进程树清理。"""

    async def complete(self, messages, tools):
        """messages已有子读取回执时启动标记后等待30秒的进程；其他情况复用有界开发脚本。"""
        context = REQUEST_CONTEXT.get()
        if context.manifest.delegation is not None and any(message['role'] == 'tool' for message in messages):
            return response(calls=[tool('run_command', argv=[sys.executable, '-c', "from pathlib import Path; import time; Path('started.txt').write_text('ready'); time.sleep(30)"])])
        return await super().complete(messages, tools)


def test_cancel_writer_and_parent_drain_real_processes(project_config, monkeypatch):
    """单项取消不停止兄弟长命令；父取消收拢剩余进程并固定两份可回读的取消成果。"""
    model = BlockingDevelopmentModel(command=True)
    app = create_development_app(project_config, monkeypatch, model)
    app.state.manager.run_config.permission_policy.command = 'allow'

    async def scenario():
        """等待两个测试进程实际写出启动标记后取消，核对真实清理状态，返回None。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            accepted = await approved_run(client, wait=False)
            run_id = accepted['run_id']
            async with asyncio.timeout(60):
                while True:
                    view = (await client.get('/api/runs/' + run_id)).json()
                    records = view['result']['subagents']
                    if len(records) == 2 and all(Path(item['workspace'], 'started.txt').exists() for item in records):
                        break
                    await asyncio.sleep(.03)
            first = records[0]['run_id']
            result = await client.post(f'/api/runs/{run_id}/subagents/{first}/cancel')
            assert result.status_code == 200 and result.json()['subagent']['status'] == 'cancelled', result.text
            sibling = (await client.get(f"/api/runs/{run_id}/subagents/{records[1]['run_id']}")).json()['subagent']
            assert sibling['status'] == 'running'
            assert (await client.post(f'/api/runs/{run_id}/cancel')).status_code == 200
            view = await finished(client, run_id)
            assert view['result']['execution_status'] == 'cancelled'
            assert view['result']['cleanup_status'] == 'complete'
            assert all(item['status'] == 'cancelled' for item in view['result']['subagents'])
            for item in view['result']['subagents']:
                result = await client.get(f"/api/runs/{run_id}/subagents/{item['run_id']}")
                assert result.status_code == 200 and result.json()['subagent']['report']['partial']
    asyncio.run(scenario())


def test_restart_keeps_child_approval_and_reconciles_nested_receipt(project_config, monkeypatch):
    """重启只读保持待审批子项，明确批准后接续；重新对账从嵌套子回执确认结果，零自动重放。"""
    model = DevelopmentModel(command=True)
    app = create_development_app(project_config, monkeypatch, model)

    async def scenario():
        """关闭并重建真实宿主，核对请求次数、固定事件和单次动作归属，返回None。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            assert view['result']['execution_status'] == 'waiting_input'
        count = len(model.requests)
        restarted = create_development_app(project_config, monkeypatch, model)
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://localhost') as client:
            read = (await client.get('/api/runs/' + view['run_id'])).json()
            assert len(model.requests) == count and read['result']['permissions']['pending'] == view['result']['permissions']['pending']
            pending = read['result']['permissions']['pending']
            body = {'request_id': str(uuid4()), 'prompt': '批准子动作', 'action_decision': {'action_id': pending['id'], 'fingerprint': pending['fingerprint'], 'action': 'approve'}}
            result = await submit_followup(client, f"/api/runs/{view['run_id']}/followups", json=body)
            assert result.status_code == 202, result.text
            await client.post('/api/queue/resume')
            next_view = await finished(client, result.json()['run_id'])
            assert next_view['result']['execution_status'] in {'completed', 'waiting_input'}, next_view['result']
            from agent_harness.permissions.store import PermissionStore
            store = PermissionStore(restarted.state.manager.runs_root)
            value = store.get(pending['id'])
            assert value['status'] == 'executed'
            value['status'] = 'executing'  # 模拟回执已落盘、事务终态尚未提交时宿主失联。
            store.save(value)
            before = len(model.requests)
            store.reconcile()
            assert store.get(pending['id'])['status'] == 'executed'
            assert len(model.requests) == before
    asyncio.run(scenario())



def test_joint_candidate_passes_full_check_before_any_parent_write(project_config, monkeypatch):
    """单成果不能通过联合检查时，组合候选先通过再一次写回，两个工作项均关联真实采纳事实。"""
    app = create_development_app(project_config, monkeypatch, DevelopmentModel())

    async def scenario():
        """用真实候选进程验证前后端共同完成后才允许写回；返回None，保持父已有文件。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            records = view['result']['subagents']
            assert len(records) == 2 and all(item['status'] == 'completed' for item in records)
            parent = project_config.projects[0].path
            (parent / 'tests/joint.py').write_text("from pathlib import Path\nfrom app import add\nassert add(2, 3) == 5\nassert Path('ui.txt').read_text(encoding='utf-8') == '前端已实现\\n'\nprint('joint passed')\n", encoding='utf-8')
            project_config.projects[0].test_argv = [sys.executable, 'tests/joint.py']
            primary = records[0]['run_id']
            target = 'parent-' + view['run_id']
            single_id = str(uuid4())
            assert (await client.post(f'/api/runs/{primary}/integrations', json={'request_id': single_id, 'parent_run_id': view['run_id'], 'target_id': target})).status_code == 202
            single = await integration_finished(client, single_id)
            assert (await action(client, single, 'check_candidate')).status_code == 202
            single = await integration_finished(client, single_id)
            assert single['checks'][-1]['result']['status'] == 'failed', single
            assert '999' in (parent / 'src/app.py').read_text()
            identifier = str(uuid4())
            response = await client.post(f'/api/runs/{primary}/integrations', json={'request_id': identifier, 'parent_run_id': view['run_id'], 'target_id': target, 'source_run_ids': [item['run_id'] for item in records]})
            assert response.status_code == 202, response.text
            value = await integration_finished(client, identifier)
            assert value['status'] == 'ready' and len(value['source_run_ids']) == 2, value
            assert (await action(client, value, 'check_candidate')).status_code == 202
            value = await integration_finished(client, identifier)
            assert value['checks'][-1]['result']['status'] == 'passed', value
            assert '999' in (parent / 'src/app.py').read_text()  # 候选检查成功仍未写回父目录。
            assert (await action(client, value, 'apply')).status_code == 202
            value = await integration_finished(client, identifier)
            assert value['status'] == 'applied', value
            assert (await action(client, value, 'check_target')).status_code == 202
            value = await integration_finished(client, identifier)
            assert value['checks'][-1]['result']['status'] == 'passed'
            state = app.state.manager.work.snapshot(view['session_id']) if hasattr(app.state.manager, 'work') else WorkStore(app.state.manager.runs_root).snapshot(view['session_id'])
            items = [item for item in state['items'] if item.get('delegation_run_id')]
            assert len(items) == 2 and all(item['integration_status'] == 'applied' for item in items)
            assert all(item['status'] != 'completed' for item in items)  # 采纳不自动伪造工作项完成。
    asyncio.run(scenario())



def test_development_context_continuation_rejects_sibling_references(tmp_path):
    """开发审批跨父Run后仍可回读原上下文，兄弟与父模型的ctx引用明确拒绝。"""
    from agent_harness.context.archive import ContextArchive
    from agent_harness.execution.workspace import WorkspaceError
    from agent_harness.schemas import RunManifest, RunResult, TaskSpec, DelegationOrigin
    from agent_harness.storage.runs import RunStore
    root = tmp_path / 'runs'
    session, task_id = uuid4(), uuid4()
    old_parent = RunManifest(run_id=session, task=TaskSpec(prompt='父任务', workspace=tmp_path), config=configured_run())
    parent_store = RunStore.create(root, old_parent)
    old_child = RunManifest(run_id=uuid4(), session_id=session, task=old_parent.task, config=configured_run(), delegation=DelegationOrigin(parent_run_id=session, role='develop', task_id=task_id, write_scope=['.']))
    old_store = RunStore.create(parent_store.run_dir / 'subagents', old_child)
    reference = ContextArchive(old_store.run_dir, old_child).save('已捕获的原子任务输出')
    old_store.finalize(RunResult(run_id=old_child.run_id, execution_status='waiting_input'))
    new_parent = old_parent.model_copy(update={'run_id': uuid4(), 'session_id': session})
    next_store = RunStore.create(root, new_parent)
    next_child = old_child.model_copy(update={'run_id': uuid4(), 'delegation': DelegationOrigin(parent_run_id=new_parent.run_id, role='develop', task_id=task_id, write_scope=['.'], previous_parent_id=session, previous_run_id=old_child.run_id)})
    new_store = RunStore.create(next_store.run_dir / 'subagents', next_child)
    archive = ContextArchive(new_store.run_dir, next_child)
    assert archive.load(reference).decode() == '已捕获的原子任务输出'
    sibling = old_child.model_copy(update={'run_id': uuid4(), 'delegation': old_child.delegation.model_copy(update={'task_id': uuid4()})})
    sibling_store = RunStore.create(parent_store.run_dir / 'subagents', sibling)
    other = ContextArchive(sibling_store.run_dir, sibling).save('不属于此任务')
    sibling_store.finalize(RunResult(run_id=sibling.run_id, execution_status='completed'))
    with pytest.raises(WorkspaceError, match='兄弟'):
        archive.load(other)
    parent_ref = ContextArchive(parent_store.run_dir, old_parent).save('父模型私有历史')
    with pytest.raises(WorkspaceError):
        archive.load(parent_ref)



@pytest.mark.parametrize('name', ['write_file', 'edit_file', 'run_command'])
def test_developer_report_accepts_actual_mutation_receipts_without_relaxing_readonly(name):
    """开发报告可引用真实成功编辑/命令；同一回执不能作为只读报告证据，失败回执也不被采信。"""
    from agent_harness.schemas import RunEvent, RunResult
    from agent_harness.subagents.reports import finalize_report
    identifier = uuid4()
    event = RunEvent(run_id=identifier, seq=1, elapsed_ms=0, type='tool_result', payload={'call_id': 'actual', 'name': name, 'result': {'ok': True, 'data': {'path': 'api.py'}, 'truncated': False}})
    result = RunResult(run_id=identifier, execution_status='completed', final_answer=report('已修改，尚未验收。', ids=['actual']))
    developer = result.model_copy(deep=True)
    assert not finalize_report(developer, [event], role='develop').partial
    assert developer.execution_status == 'completed'
    readonly = result.model_copy(deep=True)
    assert finalize_report(readonly, [event]).partial
    assert readonly.stop_reason == 'subagent_report_evidence_invalid'
    failure = event.model_copy(deep=True)
    failure.payload['result']['ok'] = False
    assert finalize_report(result, [failure], role='develop').partial


@pytest.mark.parametrize("tool_name", ["enter_plan_mode", "submit_plan"])
def test_active_development_blocks_parent_plan_switch(tmp_path, tool_name):
    """活动开发仍依赖当前批准计划；tool_name指定切换操作，必须在变更计划前拒绝。"""
    from types import SimpleNamespace
    from agent_harness.tools.registry import ToolExecutor

    executor = ToolExecutor(tmp_path, tmp_path / "runs", configured_run())
    executor.subagents = SimpleNamespace(active=True, readers_active=False)
    result = asyncio.run(executor.execute(tool_name, "{}"))
    assert result["error"]["code"] == "subagent_developers_active"


@pytest.mark.parametrize("correction,budget", [("valid", 5), ("invalid", 5), ("tool", 5), ("valid", 3)])
def test_development_report_correction_is_bounded_and_cannot_repeat_writes(project_config, monkeypatch, correction, budget):
    """报告错误只在budget剩余时修正一次；correction模拟合法/仍错/违规工具，禁止重复执行文件操作。"""
    class ReportingModel(DevelopmentModel):
        """沿真实开发流程返回错误报告，随后按用例响应宿主纠正；不直接写文件。"""

        async def complete(self, messages, tools):
            """读取messages中的宿主反馈并返回模型响应；tools核对纠正轮没有执行能力。"""
            context = REQUEST_CONTEXT.get()
            if context.manifest.delegation is not None:
                calls = [item for item in messages if item['role'] == 'tool']
                repairing = any('宿主报告纠正' in str(item.get('content', '')) for item in messages if item['role'] == 'user')
                if repairing:
                    assert tools == []
                    if correction == 'valid':
                        return response(report('实际修改已完成，等待联合验收。'))
                    if correction == 'tool':
                        return response(calls=[tool('write_file', path='unexpected.txt', content='禁止执行')])
                    return response('{"role":"develop","changed":true}')
                if len(calls) == 2:
                    return response('{"role":"develop","changed":true}')
            reply = await super().complete(messages, tools)
            if context.manifest.delegation is None:
                for index, call in enumerate(reply.tool_calls):
                    if call.name == 'delegate_develop':
                        arguments = json.loads(call.arguments_json)
                        arguments['max_model_turns'] = budget
                        # ToolCall为不可变值对象，替换供应商消息及执行列表中的同一参数。
                        updated = json.dumps(arguments)
                        reply.tool_calls[index] = ToolCall(call.call_id, call.name, updated)
                        for raw in reply.message.get('tool_calls', []):
                            if raw['id'] == call.call_id:
                                raw['function']['arguments'] = updated
            return reply

    app = create_development_app(project_config, monkeypatch, ReportingModel())

    async def scenario():
        """执行实际HTTP和双工作树链路，返回None；每个子项只允许一次真实写入及有界报告修正。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            records = view['result']['subagents']
            expected = 'completed' if correction == 'valid' and budget > 3 else 'limit_exceeded' if correction == 'tool' else 'failed'
            assert len(records) == 2 and all(item['status'] == expected for item in records), records
            for record in records:
                detail = (await client.get(f"/api/runs/{view['run_id']}/subagents/{record['run_id']}")).json()
                events = detail['events']['items']
                assert sum(item['type'] == 'subagent_report_retry' for item in events) == (1 if budget > 3 else 0)
                assert sum(item['type'] == 'tool_started' and item['payload']['name'] == 'write_file' for item in events) == 1
                assert not (Path(record['workspace']) / 'unexpected.txt').exists()
                assert record['model_turns'] <= budget
    asyncio.run(scenario())
