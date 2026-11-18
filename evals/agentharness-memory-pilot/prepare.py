"""冻结试点题集、预检和整理草案；两个入口均不发布记忆或执行 B/C 试次。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import ssl
from uuid import UUID, uuid4

import httpx

from agent_harness.config import load_config
from agent_harness.evaluation.engine import fingerprint, grade, materialize
from agent_harness.evaluation.memory_cases import MemoryTrialEnvironment
from agent_harness.evaluation.models import Suite
from agent_harness.memory.consolidation import _bounded_inputs, consolidate
from agent_harness.memory.evidence import redact
from agent_harness.memory.service import MemoryService, MemoryStore
from agent_harness.schemas import RunConfig

from spec import HISTORY, PROVENANCE, TASKS, PilotTask


REPO = Path(__file__).resolve().parents[2]  # 从试点脚本位置固定仓库根目录。
PILOT = REPO / '.runtime/memory-eval/agentharness-pilot'  # 本机隔离目录，按 UUID 新建批次。
PROJECT = 'workspace-pilot'  # 试验项目别名，不使用实际工作区身份。
MAX_DRAFT_REQUESTS = 4  # 整理和结构修复合计，纳入 300 次总预算。
REPAIR_PREFIX = '上次输出未通过结构校验。重读原始证据，按示例重新返回一个 JSON 对象。检查：'  # 整理器唯一可附加的固定反馈。
PRIVATE_MARKERS = re.compile(r'(?i)https?://|(?:^|[^\w])[a-z]:[\\/]|\\\\|[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f]{32,}\b')  # 本轮不需要网址、主机路径、邮件、地址或长标识。


def json_text(value: object) -> str:
    """把 value 统一编码为 UTF-8 可读 JSON 文本，带结尾换行。"""
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def write_json(path: Path, value: object) -> None:
    """原子保存本机 JSON 检查点；父目录必须已存在，写失败保留上一份完整记录。"""
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_text(json_text(value), encoding='utf-8')
    pending.replace(path)


def assert_sanitized(value: object) -> None:
    """拒绝含路径、联系方式、长身份标识或已知凭据的候选出站数据。"""
    rendered = json.dumps(value, ensure_ascii=False)
    if PRIVATE_MARKERS.search(rendered) or redact(rendered) != rendered:
        raise ValueError('材料包含不允许出站的标识，已停止')


def grader_script(task: PilotTask) -> str:
    """生成本机独立评分器，严格比较 JSON 结构和值，只读取数据文件。"""
    inputs = repr(json.dumps(task.inputs, ensure_ascii=False, sort_keys=True))
    expected = repr(json.dumps(task.expected, ensure_ascii=False, sort_keys=True))
    return f'''"""核对冻结输入和交付的 JSON，退出 0 通过，退出 1 为断言失败。"""
import json
from pathlib import Path
import sys

work = Path(sys.argv[1])  # 评分副本，仅从固定文件名读取。
for name, expected in (("input.json", {inputs}), ("result.json", {expected})):
    try:
        value = json.loads((work / name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise SystemExit(1)
    if json.dumps(value, ensure_ascii=False, sort_keys=True) != expected:
        raise SystemExit(1)
'''


def freeze_suite(batch: Path) -> tuple[Suite, list[dict]]:
    """在 batch 保存独立工作区素材、参考解和评分器，返回已验证的题集和冻结副本。"""
    root = batch / 'suite'
    root.mkdir()
    cases, assets = [], {}
    for task in TASKS:
        prefix = f'cases/{task.id}'
        entry = {'id': task.id, 'project': PROJECT, 'category': task.category, 'split': 'test',
                 'prompt': task.prompt + '保留 input.json 不变。', 'source': f'{prefix}/start',
                 'reference': f'{prefix}/reference', 'grader': f'{prefix}/grade.py',
                 'required_evidence': list(task.required), 'forbidden_evidence': list(task.forbidden)}
        assert_sanitized({'prompt': entry['prompt'], 'input': task.inputs})
        start = {'input.json': json_text(task.inputs)}
        reference = {**start, 'result.json': json_text(task.expected)}
        script = grader_script(task)
        materialize(root / entry['source'], start)
        materialize(root / entry['reference'], reference)
        (root / entry['grader']).write_text(script, encoding='utf-8')
        cases.append(entry)
        assets[task.id] = {'files': start, 'solution': reference, 'script': script}
    episodes = [{'id': episode, 'prompt': text, 'captured_at': date + 'T00:00:00+00:00',
                 'foreign': episode == 'foreign-sentinel'} for episode, date, text in HISTORY]
    notes = [
        {'episode': 'run-status', 'content': {'title': '独立检查回执', 'type': 'fact',
                                            'details': {'subject': '运行状态', 'attribute': '成功依据', 'value': '模型自述不能替代独立检查回执'}}},
        {'episode': 'withdrawn-sentinel', 'withdrawn': True,
         'content': {'title': '旧授权建议', 'type': 'rule', 'details': {'topic': '旧授权', 'behavior': '重新授权后沿用旧代次'}}},
    ]  # 两组共用的一个已确认条目和一个已撤回的合成条目。
    assert_sanitized({'episodes': episodes, 'notes': notes})
    suite = Suite.model_validate({'name': 'AgentHarness 脱敏项目规则试点', 'version': 'pilot-v1', 'kind': 'memory',
                                  'provenance': '项目契约改写，合成输入与独立 JSON 交付检查', 'cases': cases,
                                  'learning': {PROJECT + ':test': {'episodes': episodes, 'notes': notes}}})
    write_json(root / 'suite.json', suite.model_dump(mode='json'))
    frozen = [{**entry.model_dump(mode='json'), **assets[entry.id]} for entry in suite.cases]
    return suite, frozen


def check_learning(messages: list[dict], fixture, draftable: set[str]) -> None:
    """核对整理请求的所有学习文字都来自允许的脱敏历史或固定离线回执。"""
    assert len(messages) == 2 and [row['role'] for row in messages] == ['system', 'user']
    body = json.loads(messages[1]['content'])
    expected = {entry.prompt for entry in fixture.episodes if entry.id in draftable}
    actual = {row['text'] for row in body['historical_evidence'] if row['kind'] == 'user'}
    assert expected == actual
    for row in body['historical_evidence']:
        assert row['kind'] == 'user' or (row['kind'] == 'assistant' and row['text'] == '已记录本次提供的项目事实。')
    assert len(body['confirmed_notes']) == 1
    assert body['confirmed_notes'][0]['title'] == '独立检查回执'
    assert_sanitized(body)


class DraftTransport(httpx.AsyncBaseTransport):
    """对每次实际发出的整理请求验证目的地、固定输入和额度。"""

    def __init__(self, messages: list[dict]):
        """只允许 messages 两条固定消息，以及整理器生成的受控结构修复反馈。"""
        self.messages = messages  # 已核对的两条固定消息。
        self.sent = 0  # 实际交给网络层的 HTTP 请求数。
        self.transport = httpx.AsyncHTTPTransport(verify=ssl.create_default_context(), trust_env=False, retries=0)  # 无代理或隐式重试。

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """检查 request 再请求官方端点；校验失败在联网前中止。"""
        if request.method != 'POST' or str(request.url) != 'https://api.deepseek.com/v1/chat/completions':
            raise ValueError('整理请求目的地与冻结配置不符')
        body = json.loads(await request.aread())
        outgoing = body['messages']
        if (set(body) != {'model', 'messages', 'stream', 'max_tokens'} or body['model'] != 'deepseek-flash'
                or outgoing[:2] != self.messages or len(outgoing) not in (2, 3) or self.sent >= MAX_DRAFT_REQUESTS):
            raise ValueError('整理请求包含未核对输入或已达到预算')
        if len(outgoing) == 3:
            feedback = outgoing[2].get('content', '')
            if (outgoing[2].get('role') != 'user' or not feedback.startswith(REPAIR_PREFIX)
                    or not re.fullmatch(r'[a-zA-Z0-9_.\[\]、]+', feedback.removeprefix(REPAIR_PREFIX))):
                raise ValueError('整理修复反馈包含非预期内容')
        self.sent += 1
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        """释放已经打开的连接池。"""
        await self.transport.aclose()


async def prepare() -> None:
    """冻结六题、跑零付费预检并重放脱敏学习资料；打印批次 UUID。"""
    batch = PILOT / str(uuid4())
    batch.mkdir(parents=True)
    print(f'batch={batch.name}', flush=True)
    suite, frozen = freeze_suite(batch)
    config = load_config(REPO / 'evals/memory-v1/deepseek.toml')
    config.limits.max_model_turns = 10
    config.limits.max_tool_calls = 20
    assert config.requested_model == 'deepseek-flash' and config.limits.max_http_attempts_per_turn == 1
    grader_seconds = 20.0
    checks = []
    for case in frozen:
        case_id = case['id']
        results = {}
        for label, files in (('baseline', case['files']), ('reference', case['solution'])):
            results[label] = await grade(files, case['script'], batch / 'preflight' / case_id / label,
                                         batch / 'preflight-evidence' / case_id / label, config, grader_seconds)
        checks.append({'case_id': case_id, **results})
    write_json(batch / 'preflight.json', checks)
    if any(row['baseline']['status'] != 'failed' or row['reference']['status'] != 'passed' for row in checks):
        raise ValueError('独立评分预检未通过，详见 preflight.json')

    fixture = suite.learning[PROJECT + ':test']
    environment = MemoryTrialEnvironment(fixture, PROJECT, batch / 'runs', batch / 'learning', config)
    service = await environment.seed()
    state = service.store.read(service.source)
    selected = environment.eligible_runs()
    draftable = {episode for episode, ref in environment.refs.items() if ref['run_id'] in selected}
    messages, snapshot = _bounded_inputs(service, selected, config)
    check_learning(messages, fixture, draftable)
    write_json(batch / 'protocol.json', {
        'id': batch.name, 'state': 'pending_review', 'suite': suite.model_dump(mode='json'),
        'frozen': frozen, 'config': config.model_dump(mode='json'), 'project': PROJECT,
        'experiment': {'kind': 'memory', 'split': 'test', 'strategies': ['memory_history', 'memory_full'],
                       'repeats': 2, 'max_model_calls': 300, 'answer_request_cap': 240,
                       'max_consolidation_calls': MAX_DRAFT_REQUESTS, 'grader_timeout_seconds': grader_seconds},
        'refs': environment.refs, 'source_locations': PROVENANCE, 'base_state_sha256': fingerprint(state),
        'draft_scope': snapshot, 'draft_messages_sha256': fingerprint(messages), 'preflight': checks,
    })
    print(f'preflight={len(checks)} learning={len(environment.refs)} draft_sources={len(selected)} paid=0', flush=True)


async def draft(batch: Path) -> None:
    """读取固定批次，仅生成待审草案；结果和每次请求检查点保存在 batch。"""
    protocol = json.loads((batch / 'protocol.json').read_text(encoding='utf-8'))
    config = RunConfig.model_validate(protocol['config'])
    config.limits.max_model_turns = MAX_DRAFT_REQUESTS
    config.limits.model_max_output_tokens = 8192
    service = MemoryService(MemoryStore(batch / 'runs'), batch / 'learning')
    assert fingerprint(service.store.read(service.source)) == protocol['base_state_sha256']
    inputs, snapshot = _bounded_inputs(service, protocol['draft_scope']['run_ids'], config)
    assert fingerprint(inputs) == protocol['draft_messages_sha256'] and snapshot == protocol['draft_scope']
    key = os.environ.get(config.api_key_env or '', '')
    if not key:
        raise ValueError('当前配置未提供整理模型的凭据')
    progress_path = batch / 'draft-progress.json'
    with progress_path.open('x', encoding='utf-8') as progress_file:
        progress_file.write(json_text({'state': 'starting', 'http_attempts': 0}))

    def record(value: dict) -> None:
        """把整理器当前状态和请求账本写入同一批次，便于中断时核对。"""
        write_json(progress_path, value)

    transport = DraftTransport(inputs)
    result = await consolidate(service, snapshot['run_ids'], config, key, progress=record, transport=transport)
    assert fingerprint(service.store.read(service.source)) == protocol['base_state_sha256']
    assert result['http_attempts'] == transport.sent <= MAX_DRAFT_REQUESTS
    write_json(batch / 'draft.json', {'id': str(uuid4()), 'protocol_sha256': fingerprint(protocol), **result})
    print(f"draft={result['state']} changes={len(result['changes'])} requests={transport.sent} "
          f"known_tokens={result['usage'].get('total_tokens', 0)}", flush=True)


def main() -> None:
    """默认执行本机准备；--draft 后的 UUID 指明一个已准备批次。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--draft', type=UUID, help='仅为指定批次生成待审草案')
    args = parser.parse_args()
    asyncio.run(draft(PILOT / str(args.draft)) if args.draft else prepare())


if __name__ == '__main__':
    main()
