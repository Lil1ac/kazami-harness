"""公开测试自动修复的独立页面验收服务；离线模型，文件工具/Git/测试进程实际执行。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import uvicorn

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from tests.helpers.models import configured_run
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


OWNER = 'harness-auto-repair-20261029'  # 重启只使用本次夹具目录，保留源码和历史，不覆盖同名其他目录。


class AutoRepairBrowserModel:
    """按最新明确要求和宿主反馈选择错误/正确代码；修复阶段短暂等待便于核对真实页面。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """messages/tools为真实输入；first一次修复、second两次、third关闭时失败、cancel等待取消。"""
        users = [index for index, message in enumerate(messages) if message['role'] == 'user']
        latest = max(index for index in users if not messages[index]['content'].startswith('[宿主公开项目测试反馈'))
        prompt = messages[latest]['content']
        attempt = sum(messages[index]['content'].startswith('[宿主公开项目测试反馈') for index in users if index > latest)
        if any(message['role'] == 'tool' for message in messages[users[-1] + 1:]):
            return ModelResponse(message={'role':'assistant','content':f'代码阶段{attempt}已完成，请以项目测试结果为准。'},
                                 tool_calls=[], finish_reason='stop', response_model='offline-repair')
        if attempt:
            await asyncio.sleep(60 if 'cancel' in prompt else 3)
        required = 2 if 'second' in prompt else 1
        number = 4 if 'cancel' in prompt else 3 if 'third' in prompt else 2 if 'second' in prompt else 1
        operation = '+' if attempt >= required else '-'
        code = f'ROUND = {number}\n\ndef add(a, b):\n    return a {operation} b\n'
        call = ToolCall(uuid4().hex, 'write_file', json.dumps({'path':'app.py','content':code,'overwrite':True}))
        return ModelResponse(message={'role':'assistant','content':None,'tool_calls':[{
            'id':call.call_id,'type':'function','function':{'name':call.name,'arguments':call.arguments_json},
        }]}, tool_calls=[call], finish_reason='tool_calls', response_model='offline-repair')


def main() -> None:
    """以--root专属D盘目录及--port（默认18809）启动正式API/前端；重启保留所有历史与当前代码。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('D:/Codex/Temp/harness-auto-repair-browser-20261029'))
    parser.add_argument('--port', type=int, default=18809)
    args = parser.parse_args()
    root = args.root.resolve()
    marker = root / '.fixture-owned'
    if root.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8') != OWNER):
        raise RuntimeError('目标目录不属于本次自动修复验收。')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(OWNER, encoding='utf-8')
    source = root / '代码项目'
    if not source.exists():
        source.mkdir()
        (source/'app.py').write_bytes(b'ROUND = 0\n\ndef add(a, b):\n    return a - b\n')
        (source/'.gitignore').write_text('__pycache__/\n', encoding='utf-8')
        for argv in (['init','-q'], ['config','core.autocrlf','false'], ['add','.'],
                     ['-c','user.name=RepairFixture','-c','user.email=fixture@invalid','commit','-qm','baseline']):
            subprocess.run(['git',*argv], cwd=source, capture_output=True, check=True, timeout=15)
        (source/'USER.txt').write_text('用户原有未提交文件必须保留。\n', encoding='utf-8')
    project = Path(__file__).resolve().parents[2]
    check = "import app; print('ROUND='+str(app.ROUND),flush=True); print('add(2,3)='+str(app.add(2,3)),flush=True); assert app.add(2,3)==5, 'expected add(2,3)=5'"
    config = WebConfig(project_dir=project, frontend_dist=project/'frontend/dist', worktrees_root=root/'worktrees',
        projects=[ProjectPreset(id='repair-code',label='自动修复代码项目',path=source,test_argv=[sys.executable,'-B','-c',check])])
    os.environ['HARNESS_TEST_MODEL_KEY'] = 'offline-auto-repair-browser'
    app = create_app(config, configured_run(requested_model='offline-repair', tools=['write_file']), root/'runs', model_client=AutoRepairBrowserModel())
    uvicorn.run(app, host='127.0.0.1', port=args.port, loop='asyncio', timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()
