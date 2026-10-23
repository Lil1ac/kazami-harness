"""启动显式离线上下文验收服务，真实执行 Runner/工具，不连接模型厂商。"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class ContextBrowserModel:
    """检查实际传入消息中的规则标记，支持普通完成和工具后输入增长两个离线场景。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """从完整 messages 读取规则与任务，返回确定性响应；tools 不修改，不访问网络。

        含“持续增长”的首轮请求生成真实文件工具调用，工具结果后会增大下一轮输入。
        等待 0.3 秒供页面观察阶段；所有文件副作用仍由真实工具执行，返回统一 ModelResponse。
        """
        await asyncio.sleep(0.3)
        prompt = next(item['content'] for item in reversed(messages) if item['role'] == 'user')
        has_rule = 'CONTEXT_BROWSER_RULE' in json.dumps(messages, ensure_ascii=False)
        calls: list[ToolCall] = []  # 默认直接完成，增长场景首轮才提出文件调用。
        content = f'离线模型实际收到项目规则：{has_rule}。这是链路检查结果。'
        if '持续增长' in prompt and not any(item['role'] == 'tool' for item in messages):
            calls = [ToolCall('context-growth', 'write_file', json.dumps({
                'path': 'growth.txt', 'content': '上下文预算测试的已完成中间产物。\n' + '增长内容' * 2000,
                'overwrite': True,
            }, ensure_ascii=False))]
            content = None
        message = {'role': 'assistant', 'content': content}
        if calls:
            message['tool_calls'] = [
                {'id': call.call_id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments_json}}
                for call in calls
            ]
        return ModelResponse(message=message, tool_calls=calls, finish_reason='tool_calls' if calls else 'stop', response_model='offline-context-browser')


def main() -> None:
    """读取 --port（默认18769）启动服务；只初始化带归属标记的专属.runtime目录。

    凭据仅设置在当前辅助进程，不读取或覆盖宿主真实密钥；历史与工作区保留供重启检查。
    遇到非本服务拥有的同名目录即失败，不删除目录，也不重写已有项目指令。
    """
    parser = argparse.ArgumentParser(description='上下文管理离线浏览器服务')
    parser.add_argument('--port', type=int, default=18769)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    root = project / '.runtime/context-browser'
    marker = root / '.fixture-owned'
    if root.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8') != 'harness-context-browser'):
        raise RuntimeError('上下文验收目录已存在且没有正确归属标记。')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text('harness-context-browser', encoding='utf-8')
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    instructions = workspace / 'AGENTS.md'
    if not instructions.exists():
        instructions.write_text('请保留项目约定标记 CONTEXT_BROWSER_RULE。\n交付使用中文。\n', encoding='utf-8')
    os.environ['DEEPSEEK_API_KEY'] = 'offline-context-browser-key'
    config = WebConfig(
        project_dir=project, runtime_root=root / 'copies', frontend_dist=project / 'frontend/dist',
        workspaces=[WebWorkspace(id='context', label='上下文联动工作区（离线）', path=workspace)],
        verifiers=[],
    )
    app = create_app(config, RunConfig(requested_model='offline-context-browser', tools=['read_file', 'write_file']), root / 'runs', model_client=ContextBrowserModel())
    uvicorn.run(app, host='127.0.0.1', port=args.port, loop='asyncio', workers=1, timeout_graceful_shutdown=2)


if __name__ == '__main__':
    main()
