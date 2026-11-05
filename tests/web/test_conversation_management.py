"""真实HTTP、队列和文件证据验证会话管理，原项目文件不因整理或删除被改写。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from test_conversation_index import NavigationModel, config_for, follow, service, start
from test_project_catalog import finish


async def metadata(client, identifier: str) -> dict:
    """读取identifier会话的管理事实并返回JSON；读取不发起模型执行。"""
    response = await client.get('/api/conversations/' + identifier)
    assert response.status_code == 200, response.text
    return response.json()


async def change(client, identifier: str, action: str, **fields):
    """用当前版本对identifier发起action；fields可覆盖版本/请求编号，返回真实HTTP响应。"""
    body = {'request_id': str(uuid4()), 'expected_revision': (await metadata(client, identifier))['revision'], 'action': action, **fields}
    return await client.patch('/api/conversations/' + identifier, json=body)


def test_rename_pin_versions_and_idempotent_receipts(tmp_path: Path, monkeypatch):
    """标题及置顶跨入口统一，整理不修改消息/活动时间；过期和重复请求均有明确语义。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """生成两个真实会话后检查管理事务及不可变事件，返回None。"""
        model = NavigationModel()
        async with service(config_for(tmp_path), model) as (client, manager):
            first = await start(client, 'alpha', '首条原始需求')
            second = await start(client, 'alpha', '另一个需求')
            identifier = first['session_id']
            events = manager.runs_root / identifier / 'events.jsonl'
            before = events.read_bytes()
            initial = (await client.get('/api/conversations')).json()['items']
            original_time = next(item['updated_at'] for item in initial if item['session_id'] == identifier)
            request = {'request_id': str(uuid4()), 'expected_revision': 0, 'action': 'rename', 'title': '  导出修复  '}
            saved = await client.patch('/api/conversations/' + identifier, json=request)
            assert saved.status_code == 200, saved.text
            assert saved.json()['title'] == '导出修复' and saved.json()['revision'] == 1
            assert (await client.patch('/api/conversations/' + identifier, json=request)).json() == saved.json()
            assert (await client.patch('/api/conversations/' + identifier, json={**request, 'title': '别的名称'})).status_code == 409
            assert (await change(client, identifier, 'rename', title='覆盖', expected_revision=0)).status_code == 409
            assert (await change(client, identifier, 'rename', title='  ')).status_code == 422
            assert (await change(client, identifier, 'pin')).status_code == 200
            rows = (await client.get('/api/conversations?q=导出')).json()['items']
            assert len(rows) == 1 and rows[0]['title'] == '导出修复'
            assert rows[0]['updated_at'] == original_time
            assert (await client.get('/api/conversations')).json()['items'][0]['session_id'] == identifier
            view = (await client.get('/api/runs/' + identifier + '/conversation')).json()
            assert view['title'] == '导出修复' and view['turns'][0]['prompt'] == '首条原始需求'
            assert (await change(client, identifier, 'rename', title=None)).json()['title'] == '首条原始需求'
            assert events.read_bytes() == before
            assert model.calls == 4
    asyncio.run(scenario())


def test_input_origin_and_corrupt_metadata_are_controlled(tmp_path: Path, monkeypatch):
    """非法输入及跨源拒绝；元数据损坏时返回受控错误并保留原记录，不重建默认值。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """经真实HTTP校验输入，再注入数据库字段损坏，确认不会覆盖原文件。"""
        async with service(config_for(tmp_path), NavigationModel()) as (client, manager):
            first = await start(client, 'alpha', '边界需求')
            identifier = first['session_id']
            request = {'request_id': str(uuid4()), 'expected_revision': 0, 'action': 'rename', 'title': '新名'}
            assert (await client.patch('/api/conversations/' + identifier, json=request, headers={'Origin': 'https://untrusted.example'})).status_code == 403
            assert (await client.get('/api/conversations/not-a-uuid')).status_code == 422
            assert (await change(client, identifier, 'rename', title='x' * 201)).status_code == 422
            assert (await change(client, identifier, 'rename', title='带\n换行')).status_code == 422
            assert (await client.patch('/api/conversations/' + identifier, json=request)).status_code == 200
            with manager.management.store.transaction() as db:
                raw = json.loads(db.execute('SELECT body FROM conversations WHERE id=?', (identifier,)).fetchone()[0])
                raw['title_override'] = 42
                broken = json.dumps(raw)
                db.execute('UPDATE conversations SET body=? WHERE id=?', (broken, identifier))
            response = await client.get('/api/conversations/' + identifier)
            assert response.status_code == 503
            assert response.json()['error']['code'] == 'conversation_metadata_unreadable'
            with manager.management.store.transaction() as db:
                assert db.execute('SELECT body FROM conversations WHERE id=?', (identifier,)).fetchone()[0] == broken
    asyncio.run(scenario())


def test_nested_link_blocks_entire_frozen_cleanup(tmp_path: Path):
    """冻结目录含链接时整次清理拒绝，用户文件及会话JSON保持，避免先删后发现越界。"""
    from types import SimpleNamespace
    from agent_harness.web.conversation_management import ConversationManager
    root = tmp_path / 'runs'
    identifier = str(uuid4())
    (root / '.sessions').mkdir(parents=True)
    record_path = root / '.sessions' / (identifier + '.json')
    record_path.write_text('preserved', encoding='utf-8')
    directory = root / identifier
    directory.mkdir()
    external = tmp_path / 'user-files'
    external.mkdir()
    (external / 'source.txt').write_text('user code', encoding='utf-8')
    try:
        (directory / 'linked').symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('当前Windows用户不支持创建符号链接')
    manager = ConversationManager(SimpleNamespace(runs_root=root.resolve()))
    with pytest.raises(OSError):
        manager._clean_files({'session_id': identifier, 'purge_ids': [identifier]})
    assert record_path.read_text(encoding='utf-8') == 'preserved'
    assert (external / 'source.txt').read_text(encoding='utf-8') == 'user code'


def test_desktop_deleted_artifact_never_reaches_native_bridge(tmp_path: Path):
    """回收站产物的桌面下载和定位也返回410，不能经原生桥绕过正文访问限制。"""
    import httpx
    from agent_harness.desktop.host import DesktopHost
    from agent_harness.desktop.storage import PreferenceStore
    from agent_harness.schemas import RunConfig
    from agent_harness.web.api import create_app
    from tests.desktop.test_plan_export_integration import NativeDownload
    native = NativeDownload()
    host = DesktopHost(PreferenceStore(tmp_path / 'desktop'), native, 'management-token', 'offline-management')
    app = create_app(config_for(tmp_path), RunConfig(tools=['write_file']), tmp_path / 'runs', model_client=NavigationModel(), desktop=host)

    async def scenario():
        """带真实桌面认证创建任务并删除，验证URL校验前置于所有原生文件操作。"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', headers={'Authorization': 'Bearer management-token'}) as client:
            first = await start(client, 'alpha', '桌面访问边界')
            await change(client, first['session_id'], 'trash')
            url = '/api/runs/' + first['run_id'] + '/artifacts?location=workspace&path=app.py'
            for action in ('download', 'reveal'):
                response = await client.post('/api/desktop/' + action, json={'url': url})
                assert response.status_code == 410, response.text
                assert response.json()['error']['details']['session_id'] == first['session_id']
            assert not native.calls
    asyncio.run(scenario())


def test_move_changes_group_but_continuation_keeps_source(tmp_path: Path, monkeypatch):
    """移动与未分组改变导航，续轮仍在原目录写文件，重启后管理身份保持。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')
    config = config_for(tmp_path)

    async def scenario():
        """移动后真实接续并重启，核对原始来源、分组和文件写入结果，返回None。"""
        async with service(config, NavigationModel()) as (client, manager):
            first = await start(client, 'alpha', '原目录要求')
            identifier = first['session_id']
            prior = await follow(client, first, '移动前追加要求')
            prior = await finish(client, prior['run_id'])
            untouched_jobs = (await client.get('/api/jobs')).json()['items']
            assert all(job['conversation_title'] == '原目录要求' for job in untouched_jobs)
            moved = await change(client, identifier, 'move', project_id='beta')
            assert moved.status_code == 200 and moved.json()['assigned_project_id'] == 'beta'
            assert (await client.get('/api/conversations?project_id=alpha')).json()['total'] == 0
            grouped = (await client.get('/api/conversations?project_id=beta')).json()['items'][0]
            assert grouped['source']['id'] == 'alpha'
            second = await follow(client, prior, '继续原目录')
            second = await finish(client, second['run_id'])
            assert second['workspace'] == str(tmp_path / 'alpha')
            assert (tmp_path / 'alpha/app.py').read_text(encoding='utf-8') == 'value = 2\n'
            assert not (tmp_path / 'beta/app.py').exists()
            assert (await change(client, identifier, 'move', project_id=None)).status_code == 200
            assert (await client.get('/api/conversations?ungrouped=true')).json()['total'] == 1
        async with service(config, NavigationModel()) as (client, manager):
            assert (await metadata(client, identifier))['assigned_project_id'] is None
            view = (await client.get('/api/runs/' + second['run_id'] + '/conversation')).json()
            assert view['source']['id'] == 'alpha'
    asyncio.run(scenario())


def test_archive_trash_restore_and_access_guards(tmp_path: Path, monkeypatch):
    """归档和回收站冻结写入；所有旧运行内容入口遵守访问状态，恢复不自动执行。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """验证分类查询、旧链接、计划接续和任务写入口，恢复保留原归档状态。"""
        model = NavigationModel()
        async with service(config_for(tmp_path), model) as (client, manager):
            first = await start(client, 'alpha', '待整理')
            identifier = first['session_id']
            assert (await change(client, identifier, 'archive')).status_code == 200
            assert (await client.get('/api/conversations')).json()['total'] == 0
            assert (await client.get('/api/conversations?state=archived')).json()['total'] == 1
            view = (await client.get('/api/runs/' + identifier + '/conversation')).json()
            assert not view['can_continue'] and view['blocked_code'] == 'conversation_inactive'
            request = {'request_id': str(uuid4()), 'parent_run_id': identifier, 'followup': {'request_id': str(uuid4()), 'prompt': '不应运行'}}
            request['followup']['request_id'] = request['request_id']
            assert (await client.post('/api/jobs', json=request)).status_code == 409
            assert (await client.post('/api/runs/' + identifier + '/tasks', json={'request_id': str(uuid4()), 'subject': '不应创建'})).status_code == 409
            assert (await change(client, identifier, 'trash')).status_code == 200
            assert (await client.get('/api/conversations?state=trashed')).json()['total'] == 1
            for suffix in ('', '/conversation', '/events', '/stream', '/tasks', '/memories', '/patch', '/artifacts?location=workspace&path=app.py'):
                response = await client.get('/api/runs/' + identifier + suffix)
                assert response.status_code == 410, (suffix, response.text)
                assert response.headers['Cache-Control'] == 'no-store'
                assert response.json()['error']['details']['session_id'] == identifier
            restored = await change(client, identifier, 'restore')
            assert restored.status_code == 200 and restored.json()['state'] == 'archived'
            assert (await change(client, identifier, 'unarchive')).json()['state'] == 'active'
            assert (await client.get('/api/runs/' + identifier)).status_code == 200
            assert model.calls == 2
    asyncio.run(scenario())


def test_busy_and_queued_jobs_block_organization(tmp_path: Path, monkeypatch):
    """活动及排队续轮禁止移动归档删除，但允许标题更新，取消后才恢复整理。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """用真实阻塞模型和队列创建竞态边界，不用固定睡眠判断执行状态。"""
        model = NavigationModel()
        async with service(config_for(tmp_path).model_copy(update={"max_concurrent_runs": 1}), model) as (client, manager):
            idle = await start(client, 'alpha', '可续轮')
            running_response = await client.post('/api/jobs', json={'request_id': str(uuid4()), 'create': {'workspace_id': 'beta', 'prompt': 'wait'}})
            running = running_response.json()
            await asyncio.wait_for(model.entered.wait(), 10)
            request_id = str(uuid4())
            queued = await client.post('/api/jobs', json={'request_id': request_id, 'parent_run_id': idle['run_id'], 'followup': {'request_id': request_id, 'prompt': '排队续轮'}})
            assert queued.json()['status'] == 'queued'
            for identifier in (idle['session_id'], running['session_id']):
                assert (await change(client, identifier, 'rename', title='可修改名称')).status_code == 200
                for action in ('move', 'archive', 'trash'):
                    assert (await change(client, identifier, action, project_id='alpha')).status_code == 409
            assert (await client.post('/api/jobs/' + queued.json()['id'] + '/cancel')).status_code == 200
            assert (await change(client, idle['session_id'], 'archive')).status_code == 200
            await client.post('/api/jobs/' + running['id'] + '/cancel')
    asyncio.run(scenario())


def test_purge_cleans_owned_records_keeps_project_and_memory(tmp_path: Path, monkeypatch):
    """彻底清理移除证据与任务正文，源码和另存记忆保留，幂等旧作业不能重新执行。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """真实运行后保存记忆、移入回收站并清理，检查文件、来源投影和队列重试。"""
        from agent_harness.memory.service import MemoryContent, MemoryService, MemoryStore
        model = NavigationModel()
        async with service(config_for(tmp_path), model) as (client, manager):
            first = await start(client, 'alpha', '需要删除的正文')
            identifier = first['session_id']
            jobs = manager.jobs.list()
            old_request = {'request_id': jobs[0]['id'], 'create': {'workspace_id': 'alpha', 'prompt': '需要删除的正文'}}
            memory = MemoryService(MemoryStore(manager.runs_root), tmp_path / 'alpha')
            item = memory.create(MemoryContent(type='fact', title='已保存事实', details={'subject': '示例', 'attribute': '值', 'value': '2', 'observed_at': None}), str(uuid4()), provenance={'kind': 'manual_confirmation', 'run_id': identifier})
            original = (tmp_path / 'alpha/app.py').read_bytes()
            await change(client, identifier, 'trash')
            state = await metadata(client, identifier)
            assert state['can_purge'] and state['memory_references'] == 1
            request = {'request_id': str(uuid4()), 'expected_revision': state['revision'], 'confirm': True}
            response = await client.request('DELETE', '/api/conversations/' + identifier, json=request)
            assert response.status_code == 200, response.text
            assert response.json()['state'] == 'purged'
            assert not (manager.runs_root / identifier).exists()
            assert not (manager.runs_root / '.local-after' / identifier).exists()
            assert (tmp_path / 'alpha/app.py').read_bytes() == original
            current = memory.list()['items'][0]
            assert current['id'] == item['id'] and current['source']['unavailable']
            assert '需' in current['unavailable_reason']
            assert (await client.get('/api/conversations?state=trashed')).json()['total'] == 0
            assert (await client.get('/api/runs/' + identifier)).status_code == 410
            assert (await client.request('DELETE', '/api/conversations/' + identifier, json=request)).status_code == 200
            repeated = await client.post('/api/jobs', json=old_request)
            assert repeated.status_code == 202 and repeated.json()['purged']
            assert model.calls == 2
            assert (await change(client, identifier, 'restore')).status_code == 410
    asyncio.run(scenario())


def test_batch_partial_conflict_and_retryable_purge(tmp_path: Path, monkeypatch):
    """批量逐项回执保留冲突；文件清理失败保留清单，回收站仍可见且重试不扩大范围。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """模拟一次文件系统失败，真实事务和其余文件清理保持生产实现。"""
        async with service(config_for(tmp_path), NavigationModel()) as (client, manager):
            first = await start(client, 'alpha', '第一条')
            second = await start(client, 'beta', '第二条')
            await change(client, second['session_id'], 'rename', title='更新版本')
            batch = {'request_id': str(uuid4()), 'action': 'trash', 'items': [{'session_id': first['session_id'], 'expected_revision': 0}, {'session_id': second['session_id'], 'expected_revision': 0}]}
            response = await client.post('/api/conversations/batch', json=batch)
            assert [item['success'] for item in response.json()['items']] == [True, False]
            assert (await client.post('/api/conversations/batch', json=batch)).json() == response.json()
            original = manager.management._clean_files
            def fail(record):
                """模拟磁盘写入/删除失败；record范围不修改，返回前抛OSError。"""
                raise OSError('offline filesystem failure')
            monkeypatch.setattr(manager.management, '_clean_files', fail)
            state = await metadata(client, first['session_id'])
            request = {'request_id': str(uuid4()), 'expected_revision': state['revision'], 'confirm': True}
            failed = await client.request('DELETE', '/api/conversations/' + first['session_id'], json=request)
            assert failed.status_code == 409
            listed = (await client.get('/api/conversations?state=trashed')).json()
            assert listed['total'] == 1 and listed['items'][0]['management']['state'] == 'purging'
            monkeypatch.setattr(manager.management, '_clean_files', original)
            retried = await client.request('DELETE', '/api/conversations/' + first['session_id'], json=request)
            assert retried.status_code == 200
            assert (manager.runs_root / second['run_id']).is_dir()
            assert (tmp_path / 'alpha/app.py').read_text(encoding='utf-8') == 'value = 2\n'
    asyncio.run(scenario())


def test_isolated_worktree_requires_explicit_discard_before_purge(tmp_path: Path, monkeypatch):
    """保留的独立工作树阻止彻底删除；恢复并明确丢弃后才能清理，来源仓库保持。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """通过真实Git、审阅接口和清理路径验证工作树与会话记录的不同生命周期。"""
        from agent_harness.projects import ProjectPreset
        from test_project_catalog import repository
        source = repository(tmp_path / 'git-source')
        config = config_for(tmp_path).model_copy(update={'projects': [ProjectPreset(id='code', label='代码项目', path=source)]})
        async with service(config, NavigationModel()) as (client, manager):
            response = await client.post('/api/jobs', json={'request_id': str(uuid4()), 'create': {'project_id': 'code', 'execution_mode': 'isolated', 'prompt': '隔离修改'}})
            assert response.status_code == 202, response.text
            run = await finish(client, response.json()['run_id'])
            identifier = run['session_id']
            await change(client, identifier, 'trash')
            state = await metadata(client, identifier)
            assert not state['can_purge'] and '工作树' in state['purge_blocked_reason']
            request = {'request_id': str(uuid4()), 'expected_revision': state['revision'], 'confirm': True}
            assert (await client.request('DELETE', '/api/conversations/' + identifier, json=request)).status_code == 409
            assert Path(run['workspace']).is_dir()
            await change(client, identifier, 'restore')
            discarded = await client.post('/api/runs/' + run['run_id'] + '/review', json={'decision': 'discard'})
            assert discarded.status_code == 200, discarded.text
            assert not Path(run['workspace']).exists()
            await change(client, identifier, 'trash')
            state = await metadata(client, identifier)
            assert state['can_purge']
            request = {'request_id': str(uuid4()), 'expected_revision': state['revision'], 'confirm': True}
            assert (await client.request('DELETE', '/api/conversations/' + identifier, json=request)).status_code == 200
            assert (source / 'app.py').read_text(encoding='utf-8') == 'value = 1\n'
    asyncio.run(scenario())


def test_foreign_run_refuses_record_cleanup(tmp_path: Path, monkeypatch):
    """篡改轮次归属不能清理其他会话；验证清单失败时不删除任何原始记录。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')

    async def scenario():
        """构造跨会话错误关联并确认所有源文件及另一个会话保持原样，返回None。"""
        from uuid import UUID
        from agent_harness.storage.sessions import SessionRecord
        async with service(config_for(tmp_path), NavigationModel()) as (client, manager):
            first = await start(client, 'alpha', '第一会话')
            second = await start(client, 'beta', '第二会话')
            identifier = first['session_id']
            await change(client, identifier, 'trash')
            manager.conversations.store._write(SessionRecord(session_id=UUID(identifier), run_ids=[UUID(identifier), UUID(second['run_id'])]))
            state = await metadata(client, identifier)
            response = await client.request('DELETE', '/api/conversations/' + identifier, json={'request_id': str(uuid4()), 'expected_revision': state['revision'], 'confirm': True})
            assert response.status_code == 409 and '归属' in response.json()['error']['message']
            assert (manager.runs_root / second['run_id']).is_dir()
            assert (manager.runs_root / first['run_id']).is_dir()
    asyncio.run(scenario())


def test_partial_purge_survives_restart_and_keeps_tombstone(tmp_path: Path, monkeypatch):
    """部分文件已清理后重启仍显示重试入口，最终墓碑阻止旧链接复活或暴露正文。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-management')
    config = config_for(tmp_path)

    async def scenario():
        """模拟清理中断后重建整个服务，重试仅使用既定清单，不恢复模型执行。"""
        async with service(config, NavigationModel()) as (client, manager):
            first = await start(client, 'alpha', '中断清理正文')
            identifier = first['session_id']
            await change(client, identifier, 'trash')
            def partial(record):
                """只移除record首轮的一份证据后模拟故障；不处理用户目录。"""
                (manager.runs_root / record['session_id'] / 'events.jsonl').unlink()
                raise OSError('partial deletion')
            monkeypatch.setattr(manager.management, '_clean_files', partial)
            state = await metadata(client, identifier)
            request = {'request_id': str(uuid4()), 'expected_revision': state['revision'], 'confirm': True}
            assert (await client.request('DELETE', '/api/conversations/' + identifier, json=request)).status_code == 409
        async with service(config, NavigationModel()) as (client, manager):
            listed = (await client.get('/api/conversations?state=trashed')).json()
            assert listed['total'] == 1 and listed['items'][0]['management']['can_purge']
            assert listed['items'][0]['management']['run_count'] == 1
            assert (await client.get('/api/conversations?state=trashed&q=不匹配名称')).json()['total'] == 0
            assert (await client.request('DELETE', '/api/conversations/' + identifier, json=request)).status_code == 200
            assert (await client.get('/api/runs/' + identifier)).status_code == 410
            work = (await client.get('/api/work-events')).json()
            assert '中断清理正文' not in str(work)
            assert '中断清理正文' not in str((await client.get('/api/jobs')).json())
    asyncio.run(scenario())
