"""自动产物收集的离线页面服务：真实命令、原目录/Git工作树及逐轮固定下载。"""

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
from agent_harness.snapshots import FileSnapshotStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


OWNER = 'harness-auto-artifacts-20261028'  # 专属临时目录归属，重启保持文件与记录。


class ArtifactBrowserModel:
    """只通过run_command生成交付文件，供应商离线；每轮根据用户要求选择固定样例行为。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """messages最新要求包含first/second/third/limit；tools为真实定义，返回一次命令或最终回答。"""
        last = max(index for index, item in enumerate(messages) if item['role'] == 'user')
        if any(item['role'] == 'tool' for item in messages[last + 1:]):
            return ModelResponse(message={'role': 'assistant', 'content': '命令已结束，请查看自动收集的本轮文件。'}, tool_calls=[], finish_reason='stop', response_model='offline-artifacts')
        prompt = messages[last]['content']
        if 'limit' in prompt:
            program = "from pathlib import Path; Path('large.bin').write_bytes(b'x'*4097)"
        elif 'third' in prompt:
            program = "from pathlib import Path; Path('extra.txt').write_bytes(b'third output')"
        else:
            number = 2 if 'second' in prompt else 1
            program = ("from pathlib import Path; import json; Path('report.json').write_text(json.dumps({'round':" + str(number)
                       + "}),encoding='utf-8'); Path('image.bin').write_bytes(bytes([0,255," + str(number)
                       + "])); Path('app.py').write_text('ROUND = " + str(number)
                       + "\\n',encoding='utf-8'); Path('__pycache__').mkdir(exist_ok=True); Path('__pycache__/generated.pyc').write_bytes(b'cache')")
        call = ToolCall(uuid4().hex, 'run_command', json.dumps({'argv': [sys.executable, '-c', program]}))
        return ModelResponse(message={'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json},
        }]}, tool_calls=[call], finish_reason='tool_calls', response_model='offline-artifacts')


def main() -> None:
    """按--root专属D盘目录和--port（默认18808）启动生产API/前端；返回None，重启不覆盖已有文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('D:/Codex/Temp/harness-auto-artifacts-browser-20261028'))
    parser.add_argument('--port', type=int, default=18808)
    args = parser.parse_args()
    root = args.root.resolve()
    marker = root / '.fixture-owned'
    if root.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8') != OWNER):
        raise RuntimeError('目标目录不属于本次自动产物验收。')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(OWNER, encoding='utf-8')
    git_root, files_root = root / '代码项目', root / '普通目录'
    for source in (git_root, files_root):
        if source.exists():
            continue
        source.mkdir()
        (source / 'app.py').write_bytes(b'ROUND = 0\n')
        (source / '.gitignore').write_text('__pycache__/\n', encoding='utf-8')
        if source == git_root:
            for argv in (['init', '-q'], ['config', 'core.autocrlf', 'false'], ['add', '.'],
                         ['-c', 'user.name=ArtifactFixture', '-c', 'user.email=fixture@invalid', 'commit', '-qm', 'baseline']):
                subprocess.run(['git', *argv], cwd=source, capture_output=True, check=True, timeout=15)
        (source / 'USER.txt').write_bytes('用户已有文件保持。\n'.encode('utf-8'))
    # 仅在本夹具缩小快照预算，便于页面可靠验证“收集不完整”，产品默认预算不变。
    FileSnapshotStore.MAX_BYTES = 4096
    project = Path(__file__).resolve().parents[2]
    check = "import json; from pathlib import Path; assert json.loads(Path('report.json').read_text())['round'] in [1,2]; print('report check passed')"
    config = WebConfig(project_dir=project, worktrees_root=root / 'worktrees', frontend_dist=project / 'frontend/dist',
                       projects=[ProjectPreset(id='artifact-code', label='自动产物代码项目', path=git_root, test_argv=[sys.executable, '-c', check])],
                       workspaces=[WebWorkspace(id='artifact-files', label='普通目录', path=files_root)])
    os.environ['DEEPSEEK_API_KEY'] = 'offline-artifacts-browser'
    app = create_app(config, RunConfig(requested_model='offline-artifacts', tools=['run_command']), root / 'runs', model_client=ArtifactBrowserModel())
    uvicorn.run(app, host='127.0.0.1', port=args.port, loop='asyncio', timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()
