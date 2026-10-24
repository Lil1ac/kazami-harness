"""原目录执行页面验收服务：离线模型，真实项目、文件、测试和持久记录。"""

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
from serve_navigation_fixture import NavigationModel


def main() -> None:
    """创建有归属标记的专属目录，--port默认18782；重启保留文件和历史，不覆盖现有资源。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18782)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    root = project / '.runtime/local-execution-browser'
    marker = root / '.fixture-owned'
    owner = 'harness-local-execution-20261024'  # 本轮专用资源标识，拒绝复用其他目录。
    if root.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8') != owner):
        raise RuntimeError('目标目录不属于本轮原目录验收。')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(owner, encoding='utf-8')
    git_root, files_root = root / '自己选择的 Git 项目', root / '自己选择的 普通文件夹'
    for source in (git_root, files_root):
        if source.exists():
            continue
        source.mkdir()
        (source / 'app.py').write_text('def add(a, b):\n    """返回两数之和。"""\n    return a - b\n', encoding='utf-8')
        if source == git_root:
            (source / 'test_app.py').write_text('from app import add\n\ndef test_add():\n    """检查所选原目录实际代码。"""\n    assert add(2, 1) == 3\n', encoding='utf-8')
            (source / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n', encoding='utf-8')
            for argv in (['init', '-q'], ['config', 'core.autocrlf', 'false'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'baseline']):
                subprocess.run(['git', *argv], cwd=source, check=True, capture_output=True, timeout=15)
        (source / 'local.txt').write_text('用户当前未提交文件保持。\n', encoding='utf-8')
    os.environ['DEEPSEEK_API_KEY'] = 'offline-local-browser'
    config = WebConfig(project_dir=project, runtime_root=root / 'copies', frontend_dist=project / 'frontend/dist', projects=[ProjectPreset(id='local-code', label=git_root.name, path=git_root, test_argv=[sys.executable, '-m', 'pytest', '-q'])], workspaces=[WebWorkspace(id='local-files', label=files_root.name, path=files_root)])
    app = create_app(config, RunConfig(tools=['read_file', 'write_file']), root / 'runs', model_client=NavigationModel())
    app.state.manager.catalog.register(git_root)  # 普通目录用显式测试预设；本夹具位于父Git仓库内，不冒充原生选择结果。
    uvicorn.run(app, host='127.0.0.1', port=args.port, loop='asyncio', timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()
