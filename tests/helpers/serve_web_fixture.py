"""显式启动离线浏览器验收服务；复用真实Runner、工具和检查器，不请求模型网络。"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebVerifier, WebWorkspace


LOG_SCRIPT = """# 从日志实际计算统计结果，供离线浏览器验证真实工具链。
import json
from pathlib import Path
import re

counts = {}
for line in Path('app.log').read_text(encoding='utf-8').splitlines():
    parts = line.split()
    if len(parts) > 1 and parts[1] == 'ERROR':
        match = re.search(r'code=([A-Za-z_]+)', line)
        if match:
            code = match.group(1)
            counts[code] = counts.get(code, 0) + 1
Path('report.json').write_text(json.dumps({'counts': counts, 'total_errors': sum(counts.values())}), encoding='utf-8')
print('report generated', flush=True)
"""  # 输入来自独立工作区，计数没有固定参考值。


class BrowserFixtureModel:
    """根据消息进度返回确定性工具调用，供页面观察真实文件/进程/验收状态。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收本轮messages/tools并返回脚本化响应；最多等待30秒以供取消，不访问网络。

        普通文本任务写note.txt；含“命令等待”的任务启动真实阻塞子进程；其他任务
        搜索/读取日志、写脚本、执行并读取报告。函数只返回调用，副作用由真实工具执行。
        """
        await asyncio.sleep(0.7)
        prompt = next(item['content'] for item in messages if item['role'] == 'user')
        count = sum(item['role'] == 'tool' for item in messages)
        calls: list[ToolCall] = []
        content = None
        if '命令等待' in prompt and count == 0:
            calls = [ToolCall('wait-command', 'run_command', json.dumps({'argv': ['python', '-c', "import pathlib,time;print('command ready',flush=True);pathlib.Path('process-ready').write_text('ready');time.sleep(30)"]}))]
        elif '普通文本' in prompt:
            if count == 0:
                calls = [ToolCall('write-note', 'write_file', json.dumps({'path': 'note.txt', 'content': '这是一份普通文本产物，用于确认任务交付不受JSON格式限制。', 'overwrite': True}, ensure_ascii=False))]
            else:
                content = '已生成普通文本产物 note.txt；本任务未配置独立验收。'
        elif count == 0:
            calls = [ToolCall('search-log', 'search_files', json.dumps({'query': 'ERROR', 'glob': '*.log'})), ToolCall('read-log', 'read_file', json.dumps({'path': 'app.log'}))]
        elif count == 2:
            calls = [ToolCall('write-script', 'write_file', json.dumps({'path': 'analyze_logs.py', 'content': LOG_SCRIPT}, ensure_ascii=False))]
        elif count == 3:
            calls = [ToolCall('execute-script', 'run_command', json.dumps({'argv': ['python', 'analyze_logs.py']}))]
        elif count == 4:
            calls = [ToolCall('read-report', 'read_file', json.dumps({'path': 'report.json'}))]
        else:
            content = '日志脚本已执行并生成report.json。最终独立验收结果由宿主检查程序给出。'
        message = {'role': 'assistant', 'content': content}
        if calls:
            message['tool_calls'] = [{'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason='tool_calls' if calls else 'stop', response_model='offline-browser-fixture', usage={'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8})


def main() -> None:
    """解析--port（默认8766）并启动本机离线服务；返回None，关闭时等待后台任务收尾。

    仅当前辅助进程使用dummy凭据与专属.runtime目录，不更改用户持久环境或生产配置。
    """
    parser = argparse.ArgumentParser(description='离线浏览器验证服务')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    workspace = project / '.runtime' / 'browser-workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ['DEEPSEEK_API_KEY'] = 'offline-browser-key'
    config = WebConfig(
        project_dir=project, runtime_root=project / '.runtime' / 'browser-copies',
        frontend_dist=project / 'frontend' / 'dist',
        workspaces=[WebWorkspace(id='workspace', label='普通任务（离线浏览器验证）', path=workspace), WebWorkspace(id='logs', label='日志样例（每次独立复制）', path=project / 'examples' / 'logs', copy_on_run=True)],
        verifiers=[WebVerifier(id='logs', label='固定日志样例验收', description='仅用于仓库固定日志样例。', path=project / 'examples' / 'verifiers' / 'logs.toml')],
    )
    app = create_app(config, RunConfig(requested_model='offline-browser-fixture', tools=['read_file', 'search_files', 'write_file', 'run_command']), project / '.runtime' / 'browser-runs', model_client=BrowserFixtureModel())
    uvicorn.run(app, host='127.0.0.1', port=args.port, loop='asyncio', workers=1, timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()
