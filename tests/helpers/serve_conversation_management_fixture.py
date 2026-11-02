"""为会话管理页面提供可重启离线夹具；运行真实队列和文件工具，不连接供应商。"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class ManagementModel:
    """仅确定化供应商响应；所有写文件、证据收集和持久化仍走生产执行链路。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按messages最后用户要求写app.py一次再结束；tools不扩权，返回离线统一响应。"""
        last = max(index for index, item in enumerate(messages) if item['role'] == 'user')
        used = any(item['role'] == 'tool' for item in messages[last + 1:])
        calls = [] if used else [ToolCall(uuid4().hex, 'write_file', json.dumps({'path': 'app.py', 'content': 'value = 2\n', 'overwrite': True}))]
        message = {'role': 'assistant', 'content': '已完成离线文件修改，可查看本轮记录。' if used else None}
        if calls:
            message['tool_calls'] = [{'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason='stop' if used else 'tool_calls', response_model='offline-management')


def application(root: Path):
    """为专属root构造全新应用；返回app，目录已由main核对，重启不重置用户操作。"""
    config = WebConfig(project_dir=root, worktrees_root=root / 'trees', frontend_dist=Path(__file__).resolve().parents[2] / 'frontend/dist',
                       workspaces=[WebWorkspace(id=name, label=label, path=root / name) for name, label in [('alpha', '日志工具'), ('beta', '文档项目')]])
    app = create_app(config, RunConfig(tools=['write_file']), root / 'runs', model_client=ManagementModel())
    app.state.manager._key_provider = lambda config: 'offline-management'  # config仅用于符合供应商凭据接口；不读取真实密钥。
    return app


async def seed(root: Path) -> None:
    """首次在root接纳三条真实会话并等待收尾，写fixture.json供页面验收定位；返回None。"""
    app = application(root)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', trust_env=False) as client:
            runs = []
            for workspace, prompt in [('alpha', '整理导出日志流程'), ('alpha', '核对中文长名称与移动后原目录继续执行的会话管理交互'), ('beta', '更新说明文档')]:
                response = await client.post('/api/jobs', json={'request_id': str(uuid4()), 'create': {'workspace_id': workspace, 'prompt': prompt}})
                response.raise_for_status()
                identifier = response.json()['run_id']
                async with asyncio.timeout(30):
                    while (await client.get('/api/runs/' + identifier)).json()['active']:
                        await asyncio.sleep(0.02)
                    await app.state.manager.wait_idle()
                runs.append(identifier)
            (root / 'fixture.json').write_text(json.dumps({'runs': runs}, ensure_ascii=False), encoding='utf-8')


def main() -> None:
    """以--data-dir专属D盘目录和--port（默认18841）启动服务；拒绝复用非本夹具目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18841)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    marker = root / '.conversation-management-fixture'
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError('验收目录已有其他文件，拒绝复用。')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text('offline-management\n', encoding='utf-8')
    for name in ('alpha', 'beta'):
        (root / name).mkdir(exist_ok=True)
        source = root / name / 'app.py'
        if not source.exists():
            source.write_text('value = 1\n', encoding='utf-8')
    if not (root / 'fixture.json').exists():
        asyncio.run(seed(root))
    uvicorn.run(application(root), host='127.0.0.1', port=args.port, loop='asyncio')


if __name__ == '__main__':
    main()
