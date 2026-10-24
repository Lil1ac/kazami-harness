"""真实运行验证会话导航聚合、分页、项目隔离与坏记录保守展示。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx

from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from agent_harness.schemas import RunConfig
from test_project_catalog import EditingModel, finish


class NavigationModel(EditingModel):
    """保留真实Runner和文件动作，额外计数并提供可取消的后续轮。"""

    def __init__(self) -> None:
        """初始化模型调用计数与阻塞事件，不访问外部服务。"""
        self.calls = 0  # 列表读取前后必须保持不变。
        self.entered = asyncio.Event()  # wait要求进入模型后置位。

    async def complete(self, messages: list[dict], tools: list[dict]):
        """收到wait时等待取消；其他要求委托离线写文件模型，返回统一响应。"""
        self.calls += 1
        if next(item for item in reversed(messages) if item['role'] == 'user')['content'] == 'wait':
            self.entered.set()
            await asyncio.Event().wait()
        return await super().complete(messages, tools)


def config_for(root: Path) -> WebConfig:
    """创建两个独立普通来源，返回可重启复用的配置；记录和副本均在测试根目录内。"""
    for name in ('alpha', 'beta'):
        (root / name).mkdir(parents=True, exist_ok=True)
    return WebConfig(project_dir=root, runtime_root=root / 'runtime/copies', frontend_dist=root / 'dist',
        workspaces=[WebWorkspace(id=name,label=name.upper(),path=root/name,copy_on_run=True) for name in ('alpha','beta')])


@asynccontextmanager
async def service(config: WebConfig, model: NavigationModel):
    """产出真实HTTP客户端及管理器；退出时清理任务，既有记录留给重启验证。"""
    app=create_app(config,RunConfig(tools=['write_file']),config.project_dir/'runs',model_client=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost',trust_env=False) as client:
            yield client,app.state.manager


async def start(client: httpx.AsyncClient, source: str, prompt: str) -> dict:
    """在source登记下真实创建首轮并等待结束，返回运行视图。"""
    response=await client.post('/api/runs',json={'workspace_id':source,'prompt':prompt})
    assert response.status_code==202,response.text
    return await finish(client,response.json()['run_id'])


async def follow(client: httpx.AsyncClient, parent: dict, prompt: str) -> dict:
    """从parent末轮追加要求，返回已接纳视图；等待由调用者按场景决定。"""
    response=await client.post('/api/runs/'+parent['run_id']+'/followups',json={'request_id':str(uuid4()),'prompt':prompt})
    assert response.status_code==202,response.text
    return response.json()


def test_three_turns_form_one_entry_before_filter_and_pagination(tmp_path: Path, monkeypatch):
    """三轮只有一项，首条要求做标题；项目筛选和分页基于聚合结果，重启与读取均不执行。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-navigation')
    config=config_for(tmp_path)

    async def scenario():
        """构建三个会话、五次真实运行，再验证聚合、稳定入口和重启结果。"""
        model=NavigationModel()
        async with service(config,model) as (client,manager):
            first=await start(client,'alpha','first task')
            second=await follow(client,first,'second requirement')
            second=await finish(client,second['run_id'])
            third=await follow(client,second,'third requirement')
            third=await finish(client,third['run_id'])
            beta=await start(client,'beta','beta task')
            other=await start(client,'alpha','another task')
            before={str(path.relative_to(tmp_path)):path.read_bytes() for path in (tmp_path/'runs').rglob('*.json*')}
            calls=model.calls
            listed=(await client.get('/api/conversations')).json()
            assert listed['total']==3
            row=next(item for item in listed['items'] if item['session_id']==first['run_id'])
            assert row['title']=='first task' and row['turn_count']==3
            assert row['latest_run_id']==third['run_id'] and row['entry_run_id']==third['run_id']
            assert row['latest']['execution_status']=='completed' and row['source']['id']=='alpha'
            page=(await client.get('/api/conversations?limit=1')).json()
            next_page=(await client.get('/api/conversations?limit=1&offset=1')).json()
            assert page['total']==next_page['total']==3
            assert page['items'][0]['session_id']!=next_page['items'][0]['session_id']
            assert (await client.get('/api/conversations?project_id=alpha')).json()['total']==2
            filtered=(await client.get('/api/conversations?project_id=beta')).json()
            assert filtered['total']==1 and filtered['items'][0]['session_id']==beta['run_id']
            assert (await client.get('/api/conversations?offset=99')).json()['items']==[]
            assert (await client.get('/api/conversations?project_id=unknown')).status_code==404
            assert (await client.get('/api/conversations?limit=101')).status_code==422
            assert model.calls==calls
            assert before=={str(path.relative_to(tmp_path)):path.read_bytes() for path in (tmp_path/'runs').rglob('*.json*')}
        async with service(config,NavigationModel()) as (client,manager):
            page=(await client.get('/api/conversations?project_id=alpha')).json()
            assert page['total']==2 and any(item['turn_count']==3 for item in page['items'])
            assert (await client.get('/api/runs')).json()['total']==5
            assert manager._model_client.calls==0
    asyncio.run(scenario())


def test_latest_cancelled_turn_controls_status_filter(tmp_path: Path, monkeypatch):
    """旧轮completed不让最新cancelled会话进入完成筛选，运行时导航使用真实活动状态。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-navigation')
    async def scenario():
        """先完成首轮，再真实阻塞并取消后续轮，核对最新轮状态及只有一个入口。"""
        model=NavigationModel()
        async with service(config_for(tmp_path),model) as (client,manager):
            first=await start(client,'alpha','first task')
            waiting=await follow(client,first,'wait')
            await asyncio.wait_for(model.entered.wait(),5)
            row=(await client.get('/api/conversations')).json()['items'][0]
            assert row['latest']['active'] and row['latest_run_id']==waiting['run_id']
            await client.post('/api/runs/'+waiting['run_id']+'/cancel')
            await finish(client,waiting['run_id'])
            assert (await client.get('/api/conversations?status=completed')).json()['total']==0
            page=(await client.get('/api/conversations?status=cancelled')).json()
            assert page['total']==1 and page['items'][0]['turn_count']==2
    asyncio.run(scenario())


def test_missing_latest_and_damaged_metadata_never_show_old_success(tmp_path: Path, monkeypatch):
    """缺失最新轮或坏会话表时保留历史入口和受控提示，不退回旧轮伪装成功或修写记录。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-navigation')
    async def scenario():
        """真实创建两轮，然后隔离最新目录和损坏元数据，只读列表仍能明确表达异常。"""
        async with service(config_for(tmp_path),NavigationModel()) as (client,manager):
            first=await start(client,'alpha','first task')
            second=await follow(client,first,'second')
            await finish(client,second['run_id'])
            (tmp_path/'runs'/second['run_id']).rename(tmp_path/'isolated-latest')
            row=(await client.get('/api/conversations')).json()['items'][0]
            assert row['latest_run_id']==second['run_id'] and row['latest'] is None and row['error']
            assert row['entry_run_id']==first['run_id'] and row['turn_count']==2
            metadata=tmp_path/'runs/.sessions'/(first['run_id']+'.json')
            metadata.write_bytes(b'{broken')
            row=(await client.get('/api/conversations')).json()['items'][0]
            assert row['latest'] is None and row['latest_run_id'] is None and row['turn_count'] is None and row['error']
            assert metadata.read_bytes()==b'{broken'
    asyncio.run(scenario())


def test_reassigned_project_id_does_not_import_old_directory_conversations(tmp_path: Path, monkeypatch):
    """宿主将同编号指向另一路径后，旧会话仅保留在全局历史，不混入新项目。"""
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-navigation')
    config=config_for(tmp_path)
    async def scenario():
        """先记录旧来源，再以重分配后的配置重启，验证路径身份参与项目筛选。"""
        async with service(config,NavigationModel()) as (client,manager):
            first=await start(client,'alpha','old project')
        changed=config.model_copy(deep=True)
        changed.workspaces[0].path=tmp_path/'new-alpha'
        changed.workspaces[0].path.mkdir()
        async with service(changed,NavigationModel()) as (client,manager):
            global_page=(await client.get('/api/conversations')).json()
            assert global_page['total']==1 and global_page['items'][0]['source']['registered'] is False
            assert global_page['items'][0]['entry_run_id']==first['run_id']
            assert (await client.get('/api/conversations?project_id=alpha')).json()['total']==0
    asyncio.run(scenario())
