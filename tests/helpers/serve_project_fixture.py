"""以临时真实 Git 项目验证网页改码、测试与审阅链路；模型响应固定，不访问供应商。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import uvicorn

from agent_harness.projects.models import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


# 脚本化模型写入的正确实现；它只验证运行链路，不代表模型具备修复能力。
FIXED_SOURCE = 'def add(left, right):\n    """返回两个参数之和，用于项目测试。"""\n    return left + right\n'


class ProjectBrowserModel:
    """给出可预测的改码、失败或阻塞命令，供浏览器核对真实 Git 与测试结果。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据 messages 的任务和工具历史返回一次调用或最终回答；tools 保持原样，不访问网络。"""
        await asyncio.sleep(0.4)
        prompt = next(item['content'] for item in messages if item['role'] == 'user')
        used = sum(item['role'] == 'tool' for item in messages)
        calls: list[ToolCall] = []
        content = None
        if used == 0 and '取消' in prompt:
            program = "import pathlib,time;pathlib.Path('command.ready').write_text('ready');time.sleep(30)"
            calls = [ToolCall('wait-project', 'run_command', json.dumps({'argv': [sys.executable, '-c', program]}))]
        elif used == 0 and '保持失败' not in prompt:
            calls = [ToolCall('fix-add', 'write_file', json.dumps({'path': 'src/calc.py', 'content': FIXED_SOURCE, 'overwrite': True}, ensure_ascii=False))]
        elif used == 1 and '保持失败' not in prompt and '取消' not in prompt:
            calls = [ToolCall('add-note', 'write_file', json.dumps({'path': 'CHANGELOG.md', 'content': '修正加法计算；测试结果由宿主命令给出。\n'}, ensure_ascii=False))]
        else:
            content = '本轮执行已结束，请查看宿主项目测试与代码改动后决定是否保留。'
        message = {'role': 'assistant', 'content': content}
        if calls:
            message['tool_calls'] = [{'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason='tool_calls' if calls else 'stop', response_model='offline-project-browser', usage={'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8})


def initialize_repository(path: Path) -> None:
    """仅首次在专属 path 创建测试仓库；已有仓库保持原样，拒绝复用未知目录，返回 None。"""
    marker = path / '.fixture-owned'
    if path.exists():
        if not marker.is_file() or marker.read_text(encoding='utf-8') != 'agent-harness-project-browser':
            raise RuntimeError('离线测试目录已存在且不属于本辅助服务。')
        return
    path.mkdir(parents=True)
    marker.write_text('agent-harness-project-browser', encoding='utf-8')
    (path / 'src').mkdir()
    (path / 'tests').mkdir()
    (path / 'src/calc.py').write_text('def add(left, right):\n    """测试项目中的待修复加法。"""\n    return left - right\n', encoding='utf-8')
    (path / 'tests/test_calc.py').write_text('from calc import add\n\ndef test_add():\n    """已有测试要求加法返回两数之和。"""\n    assert add(1, 2) == 3\n', encoding='utf-8')
    (path / 'README.md').write_text('用于浏览器验证的独立测试仓库。\n', encoding='utf-8')
    (path / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.fixture-owned\n', encoding='utf-8')
    commands = [
        ['git', 'init'], ['git', 'config', 'user.name', 'Harness Fixture'],
        ['git', 'config', 'user.email', 'fixture@example.invalid'],
        ['git', 'add', '.'], ['git', 'commit', '-m', 'Create browser fixture'],
    ]
    for argv in commands:
        subprocess.run(argv, cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    # 保留一份真实未提交改动，网页任务必须以提交为基线，不能将它复制或清除。
    with (path / 'README.md').open('a', encoding='utf-8') as target:
        target.write('用户保留的本地未提交说明。\n')


def main() -> None:
    """读取 --port（默认18767）启动隔离离线服务；创建测试资源，不修改生产配置或用户仓库。"""
    parser = argparse.ArgumentParser(description='真实Git项目的离线浏览器服务')
    parser.add_argument('--port', type=int, default=18767)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    root = project / '.runtime' / 'project-browser'
    source = root / 'source'
    initialize_repository(source)
    os.environ['DEEPSEEK_API_KEY'] = 'offline-project-browser-key'
    config = WebConfig(
        project_dir=project, runtime_root=root / 'worktrees', frontend_dist=project / 'frontend/dist',
        workspaces=[], projects=[ProjectPreset(id='browser-project', label='已有Python项目（离线联动验证）', path=source, test_argv=[sys.executable, '-m', 'pytest', '-q'], test_python_paths=['src', '.'])],
    )
    app = create_app(config, RunConfig(requested_model='offline-project-browser', tools=['read_file', 'search_files', 'write_file', 'run_command']), root / 'runs', model_client=ProjectBrowserModel())
    uvicorn.run(app, host='127.0.0.1', port=args.port, loop='asyncio', workers=1, timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()