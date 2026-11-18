"""三组真实Docker离线验收；只有模型被固定HTTP夹具替换，不产生外部API请求。"""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 离线夹具复用仓库测试流式编码器。
import httpx

from agent_harness.config import load_config
from agent_harness.evaluation.engine import evaluate
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.evaluation.campaign_budget import atomic_json
from tests.helpers.streaming import model_stream_response

ROOT = Path('.runtime/ev3/offline').resolve()  # 与真实付费账本严格分开的脚本验收证据。
WORK = Path('D:/Codex/tmp/ah3-offline').resolve()  # 本轮专属短路径，避免Windows仓库文件路径过长。
CASE = 'django__django-16082'  # 已公开的开发题，离线夹具不用于报告模型能力。


def fixture(request: httpx.Request) -> httpx.Response:
    """以固定参考补丁检验三种执行入口；request只交MockTransport，返回有计量字段的脚本响应。"""
    payload = json.loads(request.content)
    names = {tool['function']['name'] for tool in payload.get('tools', [])}
    seen_result = any(message['role'] == 'tool' for message in payload['messages'])
    message = {'role': 'assistant', 'content': '离线工程验收', 'reasoning_content': 'fixture'}
    finish = 'stop'
    if not seen_result:
        patch = json.loads(Path('evals/swe-three-arm/records/' + CASE + '.json').read_text(encoding='utf-8'))['patch']
        encoded = base64.b64encode(patch.encode()).decode()
        script = "import base64,subprocess;subprocess.run(['git','apply','-'],input=base64.b64decode('" + encoded + "'),check=True)"
        if 'bash' in names:
            command = "python -c \"" + script + "\""
            arguments, tool = {'command': command}, 'bash'
        else:
            arguments, tool = {'argv': ['python', '-c', script]}, 'run_command'
        message['tool_calls'] = [{'id': 'apply', 'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(arguments)}}]
        finish = 'tool_calls'
    elif 'bash' in names:
        message['tool_calls'] = [{'id': 'submit', 'type': 'function', 'function': {'name': 'bash', 'arguments': json.dumps({
            'command': 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git diff'})}}]
        finish = 'tool_calls'
    body = {'id': 'offline', 'model': 'deepseek-v4.1-flash-1017', 'choices': [{'message': message, 'finish_reason': finish}],
        'usage': {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100,
                  'completion_tokens': 20, 'total_tokens': 120}}
    return model_stream_response(200, json=body) if payload.get('stream') else httpx.Response(200, json=body)


async def main() -> None:
    """执行三组真实隔离副本、官方评分及原生轨迹，成功后保存明确零外部请求的验收凭据。"""
    ROOT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    suite = json.loads(Path('evals/swe-three-arm/calibration.json').read_text(encoding='utf-8'))
    suite['cases'] = [case for case in suite['cases'] if case['id'] == CASE]
    suite['cases'][0]['source'] = 'record.json'
    (ROOT / 'record.json').write_bytes(Path('evals/swe-three-arm/records/' + CASE + '.json').read_bytes())
    atomic_json(ROOT / 'suite.json', suite)
    config = load_config(Path('evals/swe-three-arm/deepseek.toml'))
    experiment = Experiment(groups=preset_groups(['A', 'B', 'C']), max_cost_cents=500, max_model_calls=180,
                            max_known_tokens=10**9, seed=20261115, grader_timeout_seconds=600)
    report = await evaluate(ROOT / 'suite.json', config, experiment, ROOT / 'runs', WORK, 'offline-fixture',
        swe_python=Path('.runtime/swe-env/Scripts/python.exe'), transport=httpx.MockTransport(fixture))
    facts = {'mode': 'scripted', 'external_model_requests': 0, 'report_id': report['id'], 'state': report['state'],
             'stop_reason': report['stop_reason'], 'trials': [{key: row.get(key) for key in (
                 'strategy', 'run_id', 'success', 'execution_status', 'stop_reason', 'model_calls', 'http_attempts',
                 'trajectory_available', 'trajectory_sha256', 'patch_sha256', 'grade_artifacts')}
                 for row in report['trials']]}
    atomic_json(ROOT / 'evidence.json', facts)
    print(json.dumps(facts, ensure_ascii=False, indent=2))
    if report['state'] != 'completed' or len(report['trials']) != 3 or not all(row['success'] for row in report['trials']):
        raise RuntimeError('三组真实Docker离线链路未通过')


if __name__ == '__main__':
    asyncio.run(main())
