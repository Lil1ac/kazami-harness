"""团队页面验收宿主：确定性模型驱动真实队列、隔离工作树、通信、依赖与人工集成。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import uvicorn
from agent_harness.projects.models import ProjectPreset
from agent_harness.schemas import RunConfig, RunLimits
from tests.helpers.models import configured_run
from agent_harness.permissions.models import PermissionPolicy
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from tests.teams.test_team_execution import TeamModel, approved_run
from tests.web.test_projects import git


def application(root: Path):
    """在专属root创建/重开团队验收项目并返回应用；已有源码及执行历史不重置，模型完全离线。"""
    project = root / 'source'
    if not (project / '.git').exists():
        (project / 'src').mkdir(parents=True, exist_ok=True)
        (project / 'tests').mkdir(exist_ok=True)
        (project / 'src/app.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        (project / 'ui.txt').write_text('父目录既有前端\n', encoding='utf-8')
        (project / 'tests/check.py').write_text("import app\nfrom pathlib import Path\nassert app.add(2, 3) == 5\nassert '已固定后端' in Path('ui.txt').read_text(encoding='utf-8')\nassert Path('NOTES.txt').exists()\nprint('front and backend integration passed')\n", encoding='utf-8')
        (project / '.gitignore').write_text('__pycache__/\n', encoding='utf-8')
        git(project, 'init', '-q')
        git(project, 'add', '.')
        git(project, '-c', 'user.name=HarnessFixture', '-c', 'user.email=fixture@localhost', 'commit', '-qm', 'baseline')
        (project / 'src/app.py').write_text('def add(a, b):\n    return 999\n', encoding='utf-8')
    config = WebConfig(project_dir=root, worktrees_root=root / 'trees', frontend_dist=Path(__file__).resolve().parents[2] / 'frontend/dist',
        projects=[ProjectPreset(id='backend', label='团队协作验收', path=project, test_argv=[sys.executable, 'tests/check.py'], test_timeout_seconds=10)])
    run_config = configured_run(tools=['read_file', 'search_files', 'write_file', 'edit_file', 'run_command'],
        permission_policy=PermissionPolicy(verification='allow'), limits=RunLimits(max_model_turns=36, max_tool_calls=100, run_timeout_seconds=240))
    app = create_app(config, run_config, root / 'runs', model_client=TeamModel())
    def key_provider(config):
        """为离线config提供占位凭据，返回固定字符串；不读取真实密钥或访问供应商。"""
        return 'offline-team-fixture'
    app.state.manager._key_provider = key_provider
    return app


async def seed(root: Path) -> None:
    """为空root通过真实计划批准和开发接口建立团队，返回None；已有fixture.json时只读，不重放。"""
    if (root / 'fixture.json').exists():
        return
    app = application(root)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        view = await approved_run(client)
        if not view['result']['team']['stage_ready']:
            raise RuntimeError('团队夹具未完整汇集，请核对真实运行记录。')
        (root / 'fixture.json').write_text(json.dumps({'run_id': view['run_id'], 'session_id': view['session_id'], 'stage_run_id': view['result']['team']['stage_run_id']}), encoding='utf-8')


def main() -> None:
    """读取专属data-dir与port，先生成验收事实再启动本机服务；不操作产品源码或用户工作树。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18865)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    asyncio.run(seed(root))
    uvicorn.run(application(root), host='127.0.0.1', port=args.port, loop='asyncio', log_level='warning')


if __name__ == '__main__':
    main()
