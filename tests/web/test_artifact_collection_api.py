"""网页、原目录/Git隔离、多轮和重启的自动文件登记及固定历史下载回归。"""

from tests.web.job_helpers import submit_run, submit_followup

import asyncio
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.snapshots import FileSnapshotStore
from agent_harness.store import RunStore
from agent_harness.web.api import create_app
from tests.web.test_projects import project_config  # 复用真实Git和既有未提交文件的夹具，不复制实现。


class ReportModel:
    """按最新用户要求生成报告或下一份文件，实际调用命令，不连接供应商。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """messages最新要求决定first/second/third内容，tools为真实能力；返回命令或最终回答。"""
        last = max(index for index, item in enumerate(messages) if item['role'] == 'user')
        if any(item['role'] == 'tool' for item in messages[last + 1:]):
            return ModelResponse(message={'role': 'assistant', 'content': '文件已生成。'}, tool_calls=[], finish_reason='stop', response_model='offline-report')
        label = messages[last]['content']
        name = 'next.txt' if label == 'third' else 'report.txt'
        program = 'from pathlib import Path; Path(' + repr(name) + ').write_text(' + repr(label) + ',encoding="utf-8")'
        call = ToolCall(uuid4().hex, 'run_command', json.dumps({'argv': [sys.executable, '-c', program]}))
        return ModelResponse(message={'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json},
        }]}, tool_calls=[call], finish_reason='tool_calls', response_model='offline-report')


async def finished(client: httpx.AsyncClient, response: httpx.Response) -> dict:
    """核对response已接纳，在20秒内等待完整终态及收尾，返回真实视图；不修改文件。"""
    assert response.status_code == 202, response.text
    async with asyncio.timeout(20):
        while True:
            view = (await client.get('/api/runs/' + response.json()['run_id'])).json()
            if not view['active']:
                return view
            await asyncio.sleep(.01)


@pytest.mark.parametrize('mode', ['local', 'isolated'])
def test_generated_reports_survive_restart_and_only_current_turn_files_are_listed(project_config, monkeypatch, mode: str) -> None:
    """mode覆盖所选目录和工作树；三轮仅登记本轮变化，复用结束快照，旧报告和记录保持，返回None。"""
    config = project_config
    config.projects[0].test_argv = []
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-artifacts')
    captures = {}  # 每轮文件快照命名空间，核对没有重复结束复制。
    capture = FileSnapshotStore.capture

    def observed_capture(self, run_id, root):
        """记录run_id快照次数并在root执行真实采集，原样返回None。"""
        captures.setdefault(str(run_id), []).append(self.root.name)
        return capture(self, run_id, root)

    monkeypatch.setattr(FileSnapshotStore, 'capture', observed_capture)
    records = config.project_dir / 'records'

    async def scenario():
        """先跑首轮，重建服务后追加两轮；读取历史产物和磁盘结果，不用API投影补造终态。"""
        first = None
        for restart in range(2):
            app = create_app(config, RunConfig(tools=['run_command']), records, model_client=ReportModel())
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                    if restart == 0:
                        first = await finished(client, await submit_run(client, json={'project_id': 'backend', 'execution_mode': mode, 'prompt': 'first'}))
                        assert {item['path'] for item in first['result']['artifacts'] if item['location'] == 'workspace'} == {'report.txt'}
                        original = {name: hashlib.sha256((records / first['run_id'] / name).read_bytes()).hexdigest() for name in ('run.json', 'events.jsonl', 'summary.json')}
                    else:
                        parent = first
                        for prompt in ('second', 'third'):
                            current = await finished(client, await submit_followup(client, '/api/runs/' + parent['run_id'] + '/followups', json={'prompt': prompt, 'request_id': str(uuid4())}))
                            expected = {'next.txt'} if prompt == 'third' else {'report.txt'}
                            assert {item['path'] for item in current['result']['artifacts'] if item['location'] == 'workspace'} == expected
                            assert current['result']['artifact_status'] == 'complete'
                            assert current['workspace'] == first['workspace']
                            assert RunStore.read(records, current['run_id']).result.model_dump(mode='json') == current['result']
                            parent = current
                        preview = await client.get('/api/runs/' + first['run_id'] + '/artifacts', params={'location': 'workspace', 'path': 'report.txt', 'preview': True})
                        assert preview.status_code == 200 and preview.json()['content'] == 'first'
                        assert original == {name: hashlib.sha256((records / first['run_id'] / name).read_bytes()).hexdigest() for name in original}
                        assert len(captures) == 3
                        assert all(names == ['.local-before', '.project-files' if mode == 'isolated' else '.local-after'] for names in captures.values())
                        if mode == 'isolated':
                            decision = await client.post('/api/runs/' + current['run_id'] + '/review', json={'decision': 'discard'})
                            assert decision.status_code == 200, decision.text
                            assert not Path(current['workspace']).exists()
                            assert (await client.get('/api/runs/' + first['run_id'] + '/artifacts', params={'location': 'workspace', 'path': 'report.txt'})).content == b'first'
    asyncio.run(scenario())
