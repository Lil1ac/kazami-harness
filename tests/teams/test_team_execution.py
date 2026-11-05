"""用离线模型、真实HTTP和Git验证团队协作；不把模型报告当作成果或最终检查。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4
import httpx
import pytest

from agent_harness.runtime.runner import REQUEST_CONTEXT
from agent_harness.teams.store import TeamStore
from agent_harness.work.tasks import WorkStore
from tests.subagents.test_parallel import response
from tests.subagents.test_subagents import report
from tests.subagents.test_development import tool, create_development_app, approved_run as initial_approved_run
from tests.web.test_projects import project_config, git
from tests.web.test_integrations_api import integration_finished, action


def call_tool(tool_name: str, **arguments):
    """将tool_name和业务arguments编码为唯一模型调用；返回ToolCall，不实际执行。"""
    from agent_harness.providers.base import ToolCall
    return ToolCall(str(uuid4()), tool_name, json.dumps(arguments, ensure_ascii=False))


async def finished(client, run_id: str) -> dict:
    """等待run_id团队真实收尾并返回视图，最多180秒；覆盖多段Git汇集和原动作接续。"""
    async with asyncio.timeout(180):
        while True:
            result = await client.get(f"/api/runs/{run_id}")
            assert result.status_code == 200, result.text
            view = result.json()
            if not view['active']:
                return view
            await asyncio.sleep(.05)


async def approved_run(client, execution_mode='local', wait=True):
    """通过client明确批准计划；wait=False返回已启动视图，True等待团队完整收尾，不放宽运行预算。"""
    view = await initial_approved_run(client, execution_mode, wait=False)
    return await finished(client, view['run_id']) if wait else view


class TeamModel:
    """负责人分配有序依赖，两个成员互通消息并复用稳定身份；开发始终在独立工作树。"""

    def __init__(self, *, gated: bool = False):
        """gated=True使首个成员停在模型边界供取消测试；默认完整执行，不启动请求。"""
        self.gated = gated  # 取消场景等待真实子上下文启动。
        self.entered = asyncio.Event()  # 子模型已进入的可观察事实。
        self.release = asyncio.Event()  # 仅取消测试显式释放。
        self.child_inputs = []  # 固定输入用于核对消息来源和身份。
        self.requests = 0  # 空闲不应持续增加。

    async def complete(self, messages, tools):
        """依据当前可信运行和工具回执返回下一动作；失败立即断言，不能用最终摘要掩盖接线错误。"""
        self.requests += 1
        context = REQUEST_CONTEXT.get()
        calls = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        from agent_harness.storage.runs import RunStore
        current = RunStore.read(context.store.run_dir.parent, str(context.manifest.run_id))
        actual = [e.payload['result'] for e in current.events if e.type == 'tool_result']
        assert all(value['ok'] or (value.get('error') or {}).get('code') == 'approval_required' for value in actual), actual
        state = TeamStore(context.runs_root).get(str(context.manifest.session_id))
        if context.manifest.delegation:
            assert context.manifest.delegation.team_member_id
            assert not {'team_dispatch', 'team_offer_task', 'delegate_develop'}.intersection(t['function']['name'] for t in tools)
            self.entered.set()
            if self.gated:
                await self.release.wait()
            self.child_inputs.append(messages)
            objective = json.loads(context.manifest.task.prompt)['研究任务']
            if objective.startswith('backend'):
                if not calls:
                    recipient = state['members'][1]['id']
                    return response(calls=[call_tool('write_file', path='src/app.py', content='def add(a, b):\n    return a + b\n', overwrite=True),
                                           call_tool('team_send_message', recipient=recipient, content='后端成果即将固定；请核对依赖基线。')])
            elif objective.startswith('frontend'):
                if not calls:
                    assert any('宿主团队消息' in str(m.get('content')) and '不是用户授权' in str(m.get('content')) for m in messages)
                    return response(calls=[call_tool('read_file', path='src/app.py')])
                if len(calls) == 1:
                    assert 'a + b' in calls[0]['data']['content']
                    return response(calls=[call_tool('write_file', path='ui.txt', content='前端使用已固定后端\n', overwrite=True)])
            else:
                if not calls:
                    return response(calls=[call_tool('read_file', path='ui.txt')])
                if len(calls) == 1:
                    assert '已固定后端' in calls[0]['data']['content']
                    return response(calls=[call_tool('write_file', path='NOTES.txt', content='依赖交接已核验\n')])
            return response(report('本项已固定，整体成果仍须审阅检查。'))
        if context.manifest.planning.phase == 'planning':
            return response(calls=[call_tool('submit_plan', title='团队依赖协作', summary='后端、前端及联动交接', scope=['src/app.py', 'ui.txt', 'NOTES.txt'], steps=['交付团队功能'], verification=['候选及父目录分别检查'])])
        if state is None:
            return response(calls=[call_tool('team_create', title='前后端团队')])
        if not state['members']:
            return response(calls=[call_tool('team_add_member', name='后端', write_scope=['src/app.py', 'NOTES.txt']), call_tool('team_add_member', name='前端', write_scope=['ui.txt'])])
        tasks = WorkStore(context.runs_root).snapshot(str(context.manifest.session_id))['items']
        items = {t['subject']: t for t in tasks}
        for subject, dependency in [('backend', None), ('frontend', 'backend'), ('handoff', 'frontend')]:
            if subject not in items:
                return response(calls=[call_tool('task_create', subject=subject, description='团队 ' + subject, acceptance=['固定实际源码及交接证据'], depends_on=[items[dependency]['id']] if dependency else [])])
        if len(state['tasks']) < 3:
            return response(calls=[call_tool('team_offer_task', task_id=items[name]['id'], objective=name,
                write_scope=[path], member_id=state['members'][index]['id']) for name, path, index in
                [('backend', 'src/app.py', 0), ('frontend', 'ui.txt', 1), ('handoff', 'NOTES.txt', 0)] if not any(t['task_id'] == items[name]['id'] for t in state['tasks'])])
        if context.subagent_controller.active:
            return response(calls=[call_tool('wait_subagents', timeout_seconds=60)])
        if not all(t['state'] == 'ready' for t in state['tasks']):
            return response(calls=[call_tool('team_dispatch')])
        return response('团队已汇集，待人工采纳和父目录复验。')


def test_user_pause_resumes_team_member_without_reclaim_or_budget_reset(project_config, monkeypatch):
    """用户安全暂停后明确恢复原成员、工作树及剩余预算；旧未执行参数不被宿主重放。"""
    class PauseAwareModel(TeamModel):
        """在原团队离线模型上识别未执行回执，恢复后重新决策原工作项。"""

        async def complete(self, messages, tools):
            """仅在可信接续子段过滤模型已知的未执行回执供脚本选择；返回真实工具调用，不伪造结果。"""
            context = REQUEST_CONTEXT.get()
            if context.manifest.delegation and context.manifest.delegation.previous_run_id:
                messages = [item for item in messages if not (item.get('role') == 'tool' and
                            (json.loads(item['content']).get('error') or {}).get('code') == 'runtime_input_requires_review')]
            return await super().complete(messages, tools)

    async def scenario():
        """从 HTTP 请求暂停、等待真实成员收尾并恢复；返回 None，核对稳定身份和旧日志。"""
        model = PauseAwareModel(gated=True)
        app = create_development_app(project_config, monkeypatch, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            started = await approved_run(client, wait=False)
            await asyncio.wait_for(model.entered.wait(), 45)
            state = (await client.get(f"/api/runs/{started['run_id']}/team")).json()['team']
            member = state['members'][0]['id']
            paused = await client.post(f"/api/runs/{started['run_id']}/pause", json={'request_id': str(uuid4())})
            assert paused.status_code == 202, paused.text
            model.release.set()
            previous = await finished(client, started['run_id'])
            assert previous['result']['stop_reason'] == 'user_paused', previous['result']
            before = (app.state.manager.runs_root / started['run_id'] / 'events.jsonl').read_bytes()
            from tests.web.job_helpers import submit_followup
            result = await submit_followup(client, f"/api/runs/{started['run_id']}/followups", json={'request_id': str(uuid4()), 'prompt': '恢复暂停前团队工作', 'resume': True})
            assert result.status_code == 202, result.text
            latest = await finished(client, result.json()['run_id'])
            assert latest['result']['execution_status'] == 'completed', latest['result']
            current = (await client.get(f"/api/runs/{latest['run_id']}/team")).json()['team']
            assert current['stage_ready'] and current['members'][0]['id'] == member
            assert latest['result']['permissions']['spent_model_turns'] == previous['result']['model_turns']
            assert (app.state.manager.runs_root / started['run_id'] / 'events.jsonl').read_bytes() == before
    asyncio.run(scenario())


def test_dependency_versions_messages_reuse_and_adoption(project_config, monkeypatch):
    """下游确实读取未采纳上游源码，成员跨任务复用，消息只交付一次，最终整体采纳保留真实索引。"""
    model = TeamModel()
    app = create_development_app(project_config, monkeypatch, model)
    import sys
    project_config.projects[0].test_argv = [sys.executable, 'tests/check.py']
    app.state.manager.run_config.permission_policy.verification = 'allow'

    async def scenario():
        """通过桌面和Web共用的真实HTTP核对完整协作及C13检查链路，返回None。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            assert view['result']['execution_status'] == 'completed', view['result']
            response = await client.get(f"/api/runs/{view['run_id']}/team")
            assert response.status_code == 200, response.text
            state = response.json()['team']
            assert state['stage_ready'], state
            assert [len(m['runs']) for m in state['members']] == [2, 1]
            assert len({r['team_member_id'] for r in view['result']['subagents']}) == 2
            assert all(t['state'] == 'ready' for t in state['tasks'])
            assert len(state['tasks'][1]['dependency_versions']) == 1
            assert '999' in (Path(view['workspace']) / 'src/app.py').read_text()
            assert all(t['status'] != 'completed' for t in response.json()['tasks']['items'])
            manager = app.state.manager
            stage_id = state['stage_run_id']
            stage = manager.projects.get(stage_id)
            assert 'a + b' in (Path(stage.worktree) / 'src/app.py').read_text()
            assert (Path(stage.worktree) / 'NOTES.txt').exists()
            # 单成员补丁不能代替团队汇集成果，防止依赖遗漏。
            single = view['result']['subagents'][0]['run_id']
            rejected = await client.get(f"/api/runs/{single}/integration-targets", params={'parent_run_id': view['run_id']})
            assert rejected.status_code == 409, rejected.text
            index_path = Path(git(Path(view['workspace']), 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
            index = index_path.read_bytes()
            created = await client.post(f"/api/runs/{stage_id}/integrations", json={'request_id': str(uuid4()), 'target_id': 'parent-' + view['run_id'], 'parent_run_id': view['run_id']})
            assert created.status_code == 202, created.text
            value = await integration_finished(client, created.json()['id'])
            assert value['status'] == 'ready', value
            assert len(value['team_source_run_ids']) == 3
            assert (await action(client, value, 'check_candidate')).status_code == 202
            value = await integration_finished(client, value['id'], 'check_candidate')
            assert value['checks'][-1]['result']['status'] == 'passed', value
            applied = await action(client, value, 'apply')
            assert applied.status_code == 202, applied.text
            value = await integration_finished(client, value['id'])
            assert value['status'] == 'applied', value
            assert (await action(client, value, 'check_target')).status_code == 202
            value = await integration_finished(client, value['id'], 'check_target')
            assert value['checks'][-1]['result']['status'] == 'passed', value
            work = (await client.get(f"/api/runs/{view['run_id']}/team")).json()['tasks']['items']
            assert all(t.get('integration_status') == 'applied' for t in work if t.get('delegation_run_id'))
            assert 'a + b' in (Path(view['workspace']) / 'src/app.py').read_text()
            assert index_path.read_bytes() == index
            count = model.requests
            await asyncio.sleep(.05)
            assert model.requests == count  # 空闲不调用模型。
            assert (await client.get(f"/api/runs/{view['run_id']}/team", params={'historical': True})).json()['team']['stage_run_id'] == stage_id
            before_patch = await client.get(f"/api/runs/{stage_id}/patch", params={'parent_run_id': view['run_id']})
            assert before_patch.status_code == 200, before_patch.text
            from tests.web.job_helpers import submit_followup
            continued = await submit_followup(client, f"/api/runs/{view['run_id']}/followups", json={'request_id': str(uuid4()), 'prompt': '只核对团队事实，不修改源码'})
            assert continued.status_code == 202, continued.text
            following = await finished(client, continued.json()['run_id'])
            assert following['result']['execution_status'] == 'completed', following['result']
            assert (await client.get(f"/api/runs/{stage_id}/patch", params={'parent_run_id': view['run_id']})).content == before_patch.content
            assert (await client.get(f"/api/runs/{stage_id}/integration-targets", params={'parent_run_id': view['run_id']})).status_code == 409

    asyncio.run(scenario())


class ApprovalTeamModel(TeamModel):
    """让后端成员以需人工审批的真实命令修改源码，其他协作路径保持一致。"""

    async def complete(self, messages, tools):
        """后端首段请求真实命令，接续段只汇报已执行事实；其余输入沿用团队脚本。"""
        import sys
        context = REQUEST_CONTEXT.get()
        if context.manifest.delegation and json.loads(context.manifest.task.prompt)['研究任务'].startswith('backend'):
            calls = [m for m in messages if m['role'] == 'tool']
            if not calls:
                program = "import app; assert app.add(2, 3) == 5"
                from agent_harness.providers.base import ToolCall
                writing = ToolCall('original-backend-write', 'write_file', json.dumps({'path': 'src/app.py', 'content': 'def add(a, b):\n    return a + b\n', 'overwrite': True}))
                return response(calls=[writing, call_tool('run_command', argv=[sys.executable, '-c', program])])
            return response(report('已执行批准的后端命令，待团队集成。', ids=['original-backend-write']))
        # 审批模型没有发送首段成员消息，前端仍需从真实依赖基线读取后端。
        if context.manifest.delegation and json.loads(context.manifest.task.prompt)['研究任务'].startswith('frontend') and not any(m['role'] == 'tool' for m in messages):
            return response(calls=[call_tool('read_file', path='src/app.py')])
        return await super().complete(messages, tools)


@pytest.mark.parametrize('choice', ['approve', 'reject'])
def test_team_approval_continuation_preserves_member_and_budget(project_config, monkeypatch, choice):
    """跨父轮审批保留成员与原动作归属；批准执行一次，拒绝零模型，旧段证据不覆盖。"""
    from tests.web.job_helpers import submit_followup
    model = ApprovalTeamModel()
    app = create_development_app(project_config, monkeypatch, model)
    async def scenario():
        """通过真实审批HTTP接续成员并核对稳定身份及预算，不在测试内直接批准控制器。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            assert view['result']['execution_status'] == 'waiting_input', view['result']
            old_state = (await client.get(f"/api/runs/{view['run_id']}/team")).json()['team']
            old_member = old_state['members'][0]['id']
            pending = view['result']['permissions']['pending']
            before = (app.state.manager.runs_root / view['run_id'] / 'events.jsonl').read_bytes()
            requests = model.requests
            result = await submit_followup(client, f"/api/runs/{view['run_id']}/followups", json={'request_id': str(uuid4()), 'prompt': '处理团队原动作', 'action_decision': {'action_id': pending['id'], 'fingerprint': pending['fingerprint'], 'action': choice}})
            assert result.status_code == 202, result.text
            following = await finished(client, result.json()['run_id'])
            state = (await client.get(f"/api/runs/{following['run_id']}/team")).json()['team']
            assert state['members'][0]['id'] == old_member
            if choice == 'approve':
                assert following['result']['execution_status'] == 'completed', following['result']
                assert state['stage_ready'], state
                resumed = next(r for r in following['result']['subagents'] if r['team_member_id'] == old_member and r['task_id'] == state['tasks'][0]['task_id'])
                old_child = view['result']['subagents'][0]['run_id']
                assert next(e for e in resumed['report']['evidence'] if e['tool_call_id'] == 'original-backend-write')['run_id'] == old_child
                assert len(state['members'][0]['runs']) == 3
                assert following['result']['permissions']['spent_model_turns'] == view['result']['model_turns']
                actions = (await client.get(f"/api/runs/{following['run_id']}/permissions")).json()['items']
                assert next(item for item in actions if item['id'] == pending['id'])['status'] == 'executed'
            else:
                assert following['result']['execution_status'] == 'cancelled'
                assert model.requests == requests
                assert state['tasks'][0]['state'] == 'failed'
            assert (app.state.manager.runs_root / view['run_id'] / 'events.jsonl').read_bytes() == before
    asyncio.run(scenario())


def test_stop_active_team_drains_and_keeps_history(project_config, monkeypatch):
    """成员处于模型等待时停止团队，真实子段封存且零父目录写入，之后可明确回收工作树。"""
    model = TeamModel(gated=True)
    app = create_development_app(project_config, monkeypatch, model)
    async def scenario():
        """使用用户团队停止接口，等待真实收尾，再明确丢弃；重复读取不重启执行。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client, wait=False)
            await asyncio.wait_for(model.entered.wait(), 45)
            state = (await client.get(f"/api/runs/{view['run_id']}/team")).json()['team']
            stop_request = {'request_id': str(uuid4()), 'expected_revision': state['revision'], 'action': 'stop'}
            stopped = await client.post(f"/api/runs/{view['run_id']}/team/actions", json=stop_request)
            assert stopped.status_code == 200, stopped.text
            state = stopped.json()['team']
            assert state['status'] == 'stopped'
            assert all(m['active_run_id'] is None for m in state['members'])
            model.release.set()
            final = await finished(client, view['run_id'])
            assert final['result']['stop_reason'] == 'team_stopped', final['result']
            repeated = await client.post(f"/api/runs/{view['run_id']}/team/actions", json=stop_request)
            assert repeated.status_code == 200 and repeated.json()['team']['status'] == 'stopped', repeated.text

            assert '999' in (project_config.projects[0].path / 'src/app.py').read_text()
            state = (await client.get(f"/api/runs/{view['run_id']}/team")).json()['team']
            result = await client.post(f"/api/runs/{view['run_id']}/team/actions", json={'request_id': str(uuid4()), 'expected_revision': state['revision'], 'action': 'discard'})
            assert result.status_code == 200, result.text
            assert result.json()['team']['cleanup_status'] == 'complete'
            assert (await client.get(f"/api/runs/{view['run_id']}/team")).json()['team']['messages'] == state['messages']
    asyncio.run(scenario())


class ConflictingTeamModel(TeamModel):
    """两个成员同时基于同一固定起点改写相同文件，验证真实并行与冲突阻断。"""

    def __init__(self):
        """初始化双成员启动门闩和真实编号集合，返回None；不启动执行。"""
        super().__init__()
        self.children = set()  # 必须两个子请求同时到达才交付文件修改。
        self.both = asyncio.Event()  # 证明两个活动槽确实并行。

    async def complete(self, messages, tools):
        """成员改写同一路径；负责人将任务改为独立且同范围，冲突后只汇报待人工处理。"""
        context = REQUEST_CONTEXT.get()
        if context.manifest.delegation:
            calls = [m for m in messages if m['role'] == 'tool']
            if not calls:
                self.children.add(str(context.manifest.run_id))
                if len(self.children) == 2:
                    self.both.set()
                await self.both.wait()
                return response(calls=[call_tool('write_file', path='src/app.py', overwrite=True, content='def add(a, b):\n    return ' + ('1' if context.manifest.delegation.team_member_id.endswith('0') else str(len(self.children))) + '\n# ' + context.manifest.delegation.team_member_id + '\n')])
            return response(report('本成员独立修改已固定，仍须核对团队冲突。'))
        state = TeamStore(context.runs_root).get(str(context.manifest.session_id))
        if state and state['error']:
            return response('团队成果存在明确合并冲突，等待重新分配处理。')
        result = await super().complete(messages, tools)
        transformed = []
        for call in result.tool_calls:
            value = json.loads(call.arguments_json)
            if call.name == 'team_add_member':
                value['write_scope'] = ['.']
            elif call.name == 'task_create':
                value['depends_on'] = []
            elif call.name == 'team_offer_task':
                value['write_scope'] = ['src/app.py']
            transformed.append(call_tool(call.name, **value))
        # ToolCall不可变时用新响应固定修改过的参数，避免测试替换宿主数据。
        return response(calls=transformed) if transformed else result


def test_parallel_conflict_blocks_downstream_and_parent_write(project_config, monkeypatch):
    """真实双槽同时修改产生显式冲突；汇集不就绪，不把未采纳冲突写入父目录。"""
    model = ConflictingTeamModel()
    app = create_development_app(project_config, monkeypatch, model)
    async def scenario():
        """用真实Git合并与HTTP验证冲突事实，返回None；不自动选择任何一方覆盖。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            view = await approved_run(client)
            assert model.both.is_set(), view['result']
            state = (await client.get(f"/api/runs/{view['run_id']}/team")).json()['team']
            assert not state['stage_ready']
            assert '冲突' in state['error']
            assert view['result']['execution_status'] == 'waiting_input'
            assert view['result']['stop_reason'] == 'team_attention_required'
            assert '999' in (project_config.projects[0].path / 'src/app.py').read_text()
    asyncio.run(scenario())
