"""项目/会话导航专用浏览器服务，模型离线，Git、文件和pytest均真实执行。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import uvicorn

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


class NavigationModel:
    """逐轮读取当前代码、保留已有内容并追加要求标记，供应商调用不访问外网。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据最新用户要求与工具结果生成读写调用或回答；返回统一模型响应。"""
        last=max(index for index,item in enumerate(messages) if item['role']=='user')
        prompt=messages[last]['content']
        observations=[json.loads(item['content']) for item in messages[last+1:] if item['role']=='tool']
        calls=[]
        if not observations:
            calls=[ToolCall(uuid4().hex,'read_file',json.dumps({'path':'app.py'}))]
        elif len(observations)==1:
            content=observations[0]['data']['content'].replace('return a - b','return a + b')
            content+='\n# '+prompt.replace('\n',' ')+'\n'
            calls=[ToolCall(uuid4().hex,'write_file',json.dumps({'path':'app.py','content':content,'overwrite':True},ensure_ascii=False))]
        message={'role':'assistant','content':None if calls else '已完成本轮修改，保留此前代码，并交由项目测试检查。'}
        if calls:
            message['tool_calls']=[{'id':call.call_id,'type':'function','function':{'name':call.name,'arguments':call.arguments_json}} for call in calls]
        return ModelResponse(message=message,tool_calls=calls,finish_reason='tool_calls' if calls else 'stop',response_model='offline-conversation-navigation')


def main() -> None:
    """建立带归属标记的专用资源，--port默认18781；已有源项目和记录不覆盖，返回None。"""
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18781)
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[2]
    root=project/'.runtime/conversation-navigation-browser'
    marker=root/'.fixture-owned'
    owner='harness-conversation-navigation-20261024'  # 仅本轮专属资源允许复用。
    if root.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8')!=owner):
        raise RuntimeError('目标目录不属于本轮浏览器验收。')
    root.mkdir(parents=True,exist_ok=True)
    marker.write_text(owner,encoding='utf-8')
    presets=[]
    for name,label in [('alpha','Alpha 后端项目'),('beta','Beta 工具项目')]:
        source=root/label
        if not source.exists():
            source.mkdir()
            (source/'app.py').write_text('def add(a, b):\n    """返回两数之和。"""\n    return a - b\n',encoding='utf-8')
            (source/'test_app.py').write_text('from app import add\n\ndef test_add():\n    """原有测试必须针对工作树实际代码执行。"""\n    assert add(2, 1) == 3\n',encoding='utf-8')
            (source/'.gitignore').write_text('__pycache__/\n.pytest_cache/\n',encoding='utf-8')
            for argv in [['init','-q'],['config','core.autocrlf','false'],['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-qm','baseline']]:
                subprocess.run(['git',*argv],cwd=source,capture_output=True,check=True,timeout=15)
            (source/'local.txt').write_text('用户未提交文件保持。\n',encoding='utf-8')
        presets.append(ProjectPreset(id='nav-'+name,label=label,path=source,test_argv=[sys.executable,'-m','pytest','-q'],test_timeout_seconds=30))
    os.environ['DEEPSEEK_API_KEY']='offline-navigation-browser'
    app=create_app(WebConfig(project_dir=project,runtime_root=root/'copies',frontend_dist=project/'frontend/dist',projects=presets),RunConfig(tools=['read_file','write_file']),root/'runs',model_client=NavigationModel())
    uvicorn.run(app,host='127.0.0.1',port=args.port,loop='asyncio',timeout_graceful_shutdown=2)


if __name__=='__main__':
    main()
