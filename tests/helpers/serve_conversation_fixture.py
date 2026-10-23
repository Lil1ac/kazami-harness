"""显式启动多轮任务离线浏览器服务；执行真实Runner、文件与Git测试，不访问模型网络。"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

import uvicorn

from agent_harness.projects import ProjectPreset
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


def main() -> None:
    """读取--port（默认18771）建立专属有标记验收目录；保留已有证据，不覆盖用户仓库。"""
    parser = argparse.ArgumentParser(description='多轮任务离线浏览器验收服务')
    parser.add_argument('--port', type=int, default=18771)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    # 脚本化模型与API回归共用，以便页面实测同一组真实工具调用。
    sys.path.insert(0, str(project / 'tests/web'))
    from test_conversations import EditingModel
    root = project / '.runtime/conversation-browser'
    marker = root / '.fixture-owned'
    if root.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8') != 'harness-conversation-browser'):
        raise RuntimeError('多轮验收目录已存在且没有正确归属标记。')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text('harness-conversation-browser', encoding='utf-8')
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    source = root / 'source'
    if not source.exists():
        source.mkdir()
        (source / 'app.py').write_text('def add(a,b):\n    return a-b\n', encoding='utf-8')
        (source / 'check.py').write_text('from app import add\nassert add(2,3)==5\nprint("project test passed")\n', encoding='utf-8')
        (source / 'AGENTS.md').write_text('新增或修改函数提供中文说明。不要改check.py。\n', encoding='utf-8')
        (source / '.gitignore').write_text('__pycache__/\n', encoding='utf-8')
        for argv in (['git','init','-q'], ['git','config','core.autocrlf','false'], ['git','add','.'], ['git','-c','user.name=HarnessFixture','-c','user.email=fixture@localhost','commit','-qm','baseline']):
            subprocess.run(argv, cwd=source, check=True, capture_output=True, timeout=10)
        (source / 'local.txt').write_text('源目录未提交内容保持原样。', encoding='utf-8')
    os.environ['DEEPSEEK_API_KEY'] = 'offline-conversation-browser-key'
    config = WebConfig(project_dir=project, runtime_root=root / 'copies', frontend_dist=project / 'frontend/dist', workspaces=[WebWorkspace(id='files',label='普通文件（离线验收）',path=workspace)], projects=[ProjectPreset(id='code',label='代码项目（离线验收）',path=source,test_argv=[sys.executable,'check.py'])])
    app = create_app(config, RunConfig(requested_model='offline-conversation', tools=['read_file','write_file']), root / 'runs', model_client=EditingModel(project=True))
    uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1, loop='asyncio', timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()
