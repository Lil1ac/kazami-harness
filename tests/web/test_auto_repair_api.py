"""网页开启自动修复、Git/原目录、重启接续与逐次固定证据的真实API回归。"""

from tests.web.job_helpers import submit_run, submit_followup

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import sys

import httpx
import pytest

from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from tests.test_auto_repair import CHECK_CODE, RepairModel
from tests.web.test_projects import project_config  # 复用真实Git及用户dirty文件夹具。


async def finished(client: httpx.AsyncClient, response: httpx.Response) -> dict:
    """核对response已接纳并在20秒内等待唯一任务收尾，返回真实视图，不修改任何源码。"""
    assert response.status_code == 202, response.text
    async with asyncio.timeout(20):
        while True:
            view = (await client.get('/api/runs/' + response.json()['run_id'])).json()
            if not view['active']:
                return view
            await asyncio.sleep(.01)


@pytest.mark.parametrize('mode', ['local', 'isolated'])
def test_public_repair_and_followup_choice_survive_restart(project_config, monkeypatch, mode: str) -> None:
    """mode覆盖原目录与会话工作树；首次修复通过、重启关闭再开启，旧检查及文件字节保持。"""
    config = project_config
    config.projects[0].test_argv = [sys.executable, '-c', CHECK_CODE]
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-repair')
    records = config.project_dir / 'records'
    original_index = (config.projects[0].path / '.git/index').read_bytes()
    original_source = (config.projects[0].path / 'src/app.py').read_bytes()

    async def scenario():
        """两次独立服务生命周期共运行三轮，公开开关与预算分别冻结，下载仍指向所属旧轮。"""
        for restart in range(2):
            model = RepairModel(['bad', 'good'])
            app = create_app(config, RunConfig(tools=['run_command']), records, model_client=model)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                    if not restart:
                        first = await finished(client, await submit_run(client, json={
                            'project_id':'backend', 'execution_mode':mode, 'prompt':'生成首轮', 'config':{'auto_repair':True},
                        }))
                        assert first['result']['validation_status'] == 'passed' and first['result']['repair_attempts'] == 1
                        assert first['verification_kind'] == 'project_tests' and first['config']['auto_repair']
                        assert [a['result']['status'] for a in first['result']['verification_attempts']] == ['failed', 'passed']
                        fixed = {name:hashlib.sha256((records/first['run_id']/name).read_bytes()).hexdigest() for name in ('run.json','summary.json','events.jsonl')}
                        references = first['result']['verification_attempts'][0]['result']['artifacts']
                        old_streams = {a['path']:(records/first['run_id']/a['path']).read_bytes() for a in references}
                    else:
                        second = await finished(client, await submit_followup(client, '/api/runs/'+first['run_id']+'/followups', json={
                            'request_id':str(uuid4()), 'prompt':'这轮关闭自动修复', 'config':{'auto_repair':False},
                        }))
                        assert second['result']['validation_status'] == 'failed' and second['result']['repair_attempts'] == 0
                        third = await finished(client, await submit_followup(client, '/api/runs/'+second['run_id']+'/followups', json={
                            'request_id':str(uuid4()), 'prompt':'重新开启修复', 'config':{'auto_repair':True},
                        }))
                        assert third['result']['validation_status'] == 'passed' and third['result']['repair_attempts'] == 1
                        assert first['workspace'] == second['workspace'] == third['workspace']
                        assert fixed == {name:hashlib.sha256((records/first['run_id']/name).read_bytes()).hexdigest() for name in fixed}
                        for name, raw in old_streams.items():
                            response = await client.get('/api/runs/'+first['run_id']+'/artifacts', params={'location':'run','path':name})
                            assert response.status_code == 200 and response.content == raw
                        original = await client.get('/api/runs/'+first['run_id']+'/artifacts', params={'location':'workspace','path':'answer.txt'})
                        assert original.status_code == 200 and original.content == b'good'
                        if mode == 'isolated':
                            decision = await client.post('/api/runs/'+third['run_id']+'/review', json={'decision':'discard'})
                            assert decision.status_code == 200 and not Path(third['workspace']).exists()
                            assert (await client.get('/api/runs/'+first['run_id']+'/artifacts', params={'location':'workspace','path':'answer.txt'})).content == b'good'
    asyncio.run(scenario())
    assert (config.projects[0].path / '.git/index').read_bytes() == original_index
    assert (config.projects[0].path / 'src/app.py').read_bytes() == original_source


def test_auto_repair_without_public_tests_rejected_before_model(project_config, monkeypatch) -> None:
    """未配公开项目测试时明确拒绝，不创建Runner记录或调用模型。"""
    config = project_config
    config.projects[0].test_argv = []
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-repair')
    model = RepairModel(['bad'])

    async def scenario():
        """从受限任务API请求无测试修复，核对配置错误及目录无半启动运行。"""
        records = config.project_dir / 'records'
        app = create_app(config, RunConfig(tools=['run_command']), records, model_client=model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                response = await submit_run(client, json={'project_id':'backend','prompt':'错误请求','config':{'auto_repair':True}})
                assert response.status_code == 400 and '公开项目测试' in response.json()['error']['message']
                assert not list(records.glob('*/run.json')) and not model.requests
    asyncio.run(scenario())


def test_interrupted_repair_keeps_check_history_and_can_continue_without_replay(project_config, monkeypatch) -> None:
    """修复阶段丢失终态后重启，公开检查及流仍可读；只有用户追加要求才启动新运行。"""
    config = project_config
    config.projects[0].test_argv = [sys.executable,'-c',CHECK_CODE]
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-repair')
    records = config.project_dir / 'records'

    async def scenario():
        """固定真实挂起时事件前缀，正常关闭后模拟崩溃尾部缺失，随后读取并明确追加。"""
        gated = RepairModel(['bad','good'], wait_repair=True)
        app = create_app(config, RunConfig(tools=['run_command']), records, model_client=gated)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                response = await submit_run(client, json={'project_id':'backend','prompt':'首轮修复','config':{'auto_repair':True}})
                assert response.status_code == 202
                await asyncio.wait_for(gated.entered.wait(), 10)
                identifier = response.json()['run_id']
                directory = records / identifier
                prefix = (directory/'events.jsonl').read_bytes()
        # 仅操作本测试专属记录；实际文件始终停在原始bad版本，未伪造工具或检查结果。
        (directory/'events.jsonl').write_bytes(prefix)
        (directory/'summary.json').unlink()
        original = hashlib.sha256(prefix).hexdigest()
        model = RepairModel(['bad','good'])
        restarted = create_app(config, RunConfig(tools=['run_command']), records, model_client=model)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://localhost') as client:
                view = (await client.get('/api/runs/'+identifier)).json()
                assert view['interrupted'] and view['result']['repair_attempts'] == 1
                assert view['result']['validation_status'] == 'unverified' and view['result']['verification'] is None
                assert len(view['result']['verification_attempts']) == 1 and not model.requests
                for reference in view['result']['verification_attempts'][0]['result']['artifacts']:
                    raw = await client.get('/api/runs/'+identifier+'/artifacts', params={'location':'run','path':reference['path']})
                    assert raw.status_code == 200
                follow = await finished(client, await submit_followup(client, '/api/runs/'+identifier+'/followups', json={
                    'request_id':str(uuid4()), 'prompt':'核对后继续修复',
                }))
                assert follow['result']['validation_status'] == 'passed' and follow['config']['auto_repair']
                assert hashlib.sha256((directory/'events.jsonl').read_bytes()).hexdigest() == original
    asyncio.run(scenario())
