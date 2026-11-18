"""发布明确批准的试点条目，在隔离副本执行固定的 B/C 配对。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from uuid import UUID, uuid4, uuid5

import httpx

from agent_harness.config import prepare_task
from agent_harness.evaluation.engine import files_at, fingerprint, grade, materialize
from agent_harness.evaluation.memory_cases import MemoryTrialEnvironment, MemoryTrialExecutor, billable_usage, memory_trace
from agent_harness.evaluation.models import Suite
from agent_harness.evaluation.report import summarize
from agent_harness.memory.consolidation import publish
from agent_harness.memory.evidence import EvidenceIndex, project_events, redact
from agent_harness.memory.service import MemoryService, MemoryStore
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runtime.runner import run_task
from agent_harness.schemas import RunConfig, RunManifest
from agent_harness.storage.runs import RunStore

from prepare import PILOT, write_json


APPROVED = (1, 2, 3, 4, 5)  # 1–5 发布，第 6 项仅保留原历史证据。
APPROVED_DRAFT_SHA256 = 'bba0f51b2b4b73d512d289eca41c35f19443c8772ace5446d46b6cd6f541d16b'  # 审阅时固定的草案指纹。
STRATEGIES = ('memory_history', 'memory_full')  # B 使用基线，C 增加批准条目和主题索引。
TOOLS = {'read_file', 'search_files', 'write_file', 'edit_file', 'ask_user_question', 'memory_search', 'memory_get'}  # 评测只开放这些工具。
UNMASKED = re.compile(r'(?i)https?://|(?<![\w])[a-z]:[\\/]|\\\\[a-z0-9_.-]+\\|/(?:home|Users|mnt|var|etc|tmp)/|[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b')  # 不允许发送的主机位置或直接标识。


def read_json(path: Path) -> dict:
    """读取本机 path 下 UTF-8 JSON 记录。"""
    return json.loads(path.read_text(encoding='utf-8'))


def mask(value, paths: dict[str, str]):
    """以 paths 给出的别名投影请求字段，发现其余直接标识时拒绝出站。"""
    if isinstance(value, str):
        for path, alias in paths.items():
            value = re.sub(re.escape(path), lambda _: alias, value, flags=re.IGNORECASE)
        # 工具回执还会内嵌 JSON。先核对解码后的字段，避免把多层转义的换行当成 UNC 路径。
        if value.lstrip().startswith(('{', '[')):
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            if isinstance(decoded, (dict, list)):
                checked = mask(decoded, paths)
                return value if checked == decoded else json.dumps(checked, ensure_ascii=False)
        if UNMASKED.search(value) or redact(value) != value:
            raise ValueError('答题请求含未脱敏的主机位置或标识')
    elif isinstance(value, list):
        return [mask(item, paths) for item in value]
    elif isinstance(value, dict):
        return {key: mask(item, paths) for key, item in value.items()}
    return value


def path_aliases(workspace: Path, executable: str | None) -> dict[str, str]:
    """返回隔离工作区和本机解释器常见路径表示的对应别名。"""
    return {variant: name for path, name in ((workspace, '.'), (executable, 'python')) if path is not None
            for variant in {str(path), Path(path).as_posix(), str(path).replace('\\', '\\\\')}}


class OfflineProbe:
    """用固定回执核对 B/C 的出站投影和写入链路，整个过程无 HTTP。"""

    def __init__(self, aliases: dict[str, str], output: str):
        """保存待替换的路径和仅供本机探针写入的参考 JSON。"""
        self.aliases = aliases  # 实际试次使用相同的脱敏函数。
        self.output = output  # 固定回执内容，不参与正式答题。
        self.turns = 0  # 已检查的消息投影数。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """检查当前消息与工具，并依次给出固定读、写、结束回执。"""
        mask({'messages': messages, 'tools': tools}, self.aliases)
        assert {tool['function']['name'] for tool in tools} <= TOOLS
        self.turns += 1
        usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        if self.turns == 3:
            return ModelResponse({'role': 'assistant', 'content': '本机探针完成'}, [], 'stop', 'offline-pilot-probe', usage=usage)
        assert self.turns in (1, 2)
        name = 'read_file' if self.turns == 1 else 'write_file'
        arguments = {'path': 'input.json'} if self.turns == 1 else {'path': 'result.json', 'content': self.output}
        call = ToolCall(f'probe-{self.turns}', name, json.dumps(arguments, ensure_ascii=False))
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': call.call_id, 'type': 'function',
                   'function': {'name': call.name, 'arguments': call.arguments_json}}]}
        return ModelResponse(message, [call], 'tool_calls', 'offline-pilot-probe', usage=usage)


class TrialTransport(httpx.AsyncBaseTransport):
    """核对每次实际发出的答题请求，写入不含正文和凭据的共享账本。"""

    def __init__(self, paths: dict[str, str], planned: dict, ledger: list[dict], ledger_path: Path, draft_requests: int):
        """绑定本试次别名、计划与共享账本；draft_requests 为已经使用的整理请求。"""
        self.paths = paths  # 仅替换当前隔离工作区和解释器。
        self.planned = planned  # 稳定配对编号，写入本机审计记录。
        self.ledger = ledger  # 整批已发送请求的内存镜像。
        self.ledger_path = ledger_path  # 每次发出前追加一行。
        self.draft_requests = draft_requests  # 计入总上限的已完成整理次数。
        self.sent: list[dict] = []  # 本试次发出时刻，用于对应供应商回执。
        self.blocked = False  # 出站校验失败时标记，避免错误结果进入配对。
        self.transport = httpx.AsyncHTTPTransport(verify=ssl.create_default_context(), trust_env=False, retries=0)  # HTTPS 连接，无隐式重试。

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """在网络层发送前核对目标和额度，返回真实流式响应。"""
        try:
            if request.method != 'POST' or str(request.url) != 'https://api.deepseek.com/v1/chat/completions':
                raise ValueError('答题请求的目标与冻结协议不符')
            if len(self.sent) >= 10 or len(self.ledger) + self.draft_requests >= 300:
                raise ValueError('答题请求达到单题或整批上限')
            data = json.loads(await request.aread())
            if (not set(data) <= {'model', 'messages', 'stream', 'max_tokens', 'tools'} or data['model'] != 'deepseek-flash'
                    or not {tool['function']['name'] for tool in data.get('tools', [])} <= TOOLS):
                raise ValueError('答题模型、参数或工具超出冻结范围')
            outgoing = mask(data, self.paths)
        except (ValueError, KeyError, TypeError):
            self.blocked = True
            raise
        body = json.dumps(outgoing, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        ticket = {**self.planned, 'number': len(self.ledger) + 1 + self.draft_requests,
                  'requested_at': datetime.now(UTC).isoformat(), 'payload_sha256': fingerprint(outgoing)}
        with self.ledger_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(ticket, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.ledger.append(ticket)
        self.sent.append(ticket)
        headers = httpx.Headers(request.headers)
        headers['Content-Length'] = str(len(body))
        masked = httpx.Request(request.method, request.url, headers=headers, content=body, extensions=request.extensions)
        return await self.transport.handle_async_request(masked)

    async def aclose(self) -> None:
        """关闭该试次独立的连接池。"""
        await self.transport.aclose()


def load_environment(batch: Path, protocol: dict, review: dict, config: RunConfig) -> tuple[MemoryTrialEnvironment, dict]:
    """核对冻结历史与基线，发布批准的五项，返回 B/C 各自的固定记忆。"""
    project = protocol['project']
    fixture = Suite.model_validate(protocol['suite']).learning[project + ':test']
    environment = MemoryTrialEnvironment(fixture, project, batch / 'runs', batch / 'learning', config)
    environment.refs = protocol['refs']
    for episode in fixture.episodes:
        ref = environment.refs[episode.id]
        history = RunStore.read(environment.root, ref['run_id'])
        assert history.result.response_model == 'offline-memory-replay'
        assert [event['text'] for event in project_events(history) if event['kind'] == 'user'] == [episode.prompt]
        environment.history[episode.id] = history
    service = MemoryService(MemoryStore(environment.root), environment.work)
    baseline = service.store.read(environment.work, sequence=review['snapshot']['base_sequence'])
    assert fingerprint(baseline) == protocol['base_state_sha256']
    receipt = publish(service, review['id'], review, [number - 1 for number in APPROVED],
                      str(uuid5(UUID(review['id']), 'approved-items-1-5')))
    assert receipt['state'] == 'published' and receipt['selected'] == [0, 1, 2, 3, 4] and len(receipt['notes']) == 5
    enriched = service.store.read(environment.work, sequence=receipt['sequence'])
    environment.baseline = baseline['items']
    environment.enriched = enriched['items']
    assert sum(item['status'] == 'active' for item in environment.baseline) == 1
    assert sum(item['status'] == 'active' for item in environment.enriched) == 6
    return environment, receipt


def checkpoint(directory: Path, report: dict) -> None:
    """按同题同重复汇总有效配对，原子落盘全部已结束的试次。"""
    summary = summarize(report)
    by_pair = {(row['case_id'], row['repeat'], row['strategy']): row for row in report['trials'] if row['comparison_valid']}
    paired = {'wins': [], 'losses': [], 'ties': []}
    for case in report['cases']:
        for repeat in range(2):
            left, right = (by_pair.get((case['id'], repeat, strategy)) for strategy in STRATEGIES)
            if left is not None and right is not None:
                delta = int(right['success']) - int(left['success'])
                paired['wins' if delta > 0 else 'losses' if delta < 0 else 'ties'].append(f"{case['id']}#{repeat + 1}")
    summary['memory_pairs'] = [{'baseline': STRATEGIES[0], 'candidate': STRATEGIES[1], **paired,
                                'valid_pairs': sum(map(len, paired.values())), 'planned_pairs': 12}]
    for group in summary['groups']:
        rows = [row for row in report['trials'] if row['strategy'] == group['strategy']]
        group['artifact_passes'] = sum(row['grade']['status'] == 'passed' for row in rows)
        group['http_requests'] = sum(row['http_attempts'] for row in rows)
    report['summary'] = summary
    write_json(directory / 'report.json', report)


async def check_local(batch: Path, case: dict, environment: MemoryTrialEnvironment) -> list[dict]:
    """离线执行两组探针，验证脱敏投影、内存约定和读写，返回本机验收记录。"""
    checked = []
    for strategy in STRATEGIES:
        run_id = uuid4()
        workspace = batch / 'offline-probe' / str(run_id)
        materialize(workspace, case['files'])
        config, _ = environment.bind(workspace, strategy)
        task = prepare_task(case['prompt'], workspace, python_executable=sys.executable)
        output = case['solution']['result.json']
        probe = OfflineProbe(path_aliases(workspace, task.python_execution.executable), output)
        manifest = RunManifest(run_id=run_id, task=task, config=config)
        outcome = await run_task(manifest, environment.root, 'offline-pilot-probe', model_client=probe,
                                 executor=MemoryTrialExecutor(workspace, environment.root, config))
        result = outcome.result
        assert result.http_attempts == 0 and result.execution_status == 'completed' and probe.turns == 3
        assert (workspace / 'result.json').read_text(encoding='utf-8') == output
        checked.append({'strategy': strategy, 'run_id': str(run_id), 'projections_checked': probe.turns, 'http_attempts': 0})
    return checked


async def trial(batch: Path, directory: Path, planned: dict, case: dict, environment: MemoryTrialEnvironment,
                key: str, ledger: list[dict], draft_requests: int, forbidden: list[str]) -> dict:
    """运行一份隔离数据，使用冻结评分器和历史追踪返回独立记录。"""
    started = time.monotonic()
    run_id = planned['run_id']
    workspace = batch / 'trial-work' / run_id
    materialize(workspace, case['files'])
    config, aliases = environment.bind(workspace, planned['strategy'])
    task = prepare_task(case['prompt'], workspace, python_executable=sys.executable)
    paths = path_aliases(workspace, task.python_execution.executable)
    transport = TrialTransport(paths, planned, ledger, directory / 'http-ledger.jsonl', draft_requests)
    manifest = RunManifest(run_id=UUID(run_id), task=task, config=config)
    await run_task(manifest, environment.root, key, transport=transport,
                   executor=MemoryTrialExecutor(workspace, environment.root, config))
    history = RunStore.read(environment.root, run_id)
    if transport.blocked:
        raise RuntimeError('出站防护拦截请求，请检查本机运行记录')
    return await score_trial(batch, planned, case, history, aliases, transport.sent, forbidden, time.monotonic() - started)


async def score_trial(batch: Path, planned: dict, case: dict, history, aliases: dict[str, str],
                      sent: list[dict], forbidden: list[str], run_seconds: float) -> dict:
    """根据已保存终态评分并汇总用量，不发送请求；run_seconds 为评分前实际运行秒数。"""
    started = time.monotonic()
    run_id = planned['run_id']
    workspace = batch / 'trial-work' / run_id
    result = history.result
    assert history.manifest.task.workspace == workspace and result is not None
    assert result.http_attempts == len(sent) <= 10 and result.model_turns <= 10
    grading = batch / 'g' / run_id / uuid4().hex[:8]  # 避开 Windows 命令日志目录的路径长度限制。
    try:
        graded = await grade(files_at(workspace), case['script'], grading / 'work',
                             grading / 'log', history.manifest.config, 20.0)
    except (OSError, ValueError, UnicodeError):
        graded = {'status': 'error', 'reason': 'artifact_unreadable'}
    clean = result.cleanup_status in {'complete', 'not_needed'}
    responses = model_usage(history, sent)
    success = result.execution_status == 'completed' and clean and graded['status'] == 'passed'
    return {**planned, 'project': case['project'], 'category': case['category'], 'success': success,
            'execution_status': result.execution_status, 'stop_reason': result.stop_reason, 'grade': graded,
            'needs_input': result.execution_status == 'waiting_input', 'duration_seconds': run_seconds + time.monotonic() - started,
            'model_calls': result.model_turns, 'http_attempts': result.http_attempts, 'tool_calls': result.tool_calls,
            'known_tokens': result.usage.total_tokens, 'usage_complete': result.usage_complete,
            'response_model': result.response_model, 'patch_available': False, 'cleanup_status': result.cleanup_status,
            'failure_type': None if success else 'execution' if result.execution_status != 'completed' else
                'grading_environment' if graded['status'] == 'error' else 'test_assertion',
            'input_bytes_peak': max((event.payload['input_bytes'] for event in history.events if event.type == 'context_checked'), default=0),
            'summary_calls': 0, 'compression_applied': False,
            'memory': memory_trace(history, aliases, case['required_evidence'], forbidden),
            'usage': result.usage.model_dump(), 'model_usage': responses,
            'started_at': history.manifest.created_at.isoformat(), 'grade_evidence': (grading / 'log').relative_to(batch).as_posix(),
            'comparison_valid': result.usage_complete and graded['status'] != 'error' and clean}


def model_usage(history, sent: list[dict]) -> list[dict]:
    """对齐供应商回执与已发请求；出站前被拦截的轮次不计为外部请求。"""
    responses = billable_usage(history)
    for item, event in zip(responses, (e for e in history.events if e.type == 'model_response'), strict=True):
        item['requested_at'] = sent[event.payload['model_turn'] - 1]['requested_at']
    return responses


async def recover_trial(batch: Path, planned: dict, case: dict, environment: MemoryTrialEnvironment,
                        ledger: list[dict], forbidden: list[str]) -> dict:
    """重用已完成的试次和原始请求账本，补做因评分路径故障缺失的评分。"""
    history = RunStore.read(environment.root, planned['run_id'])
    workspace = batch / 'trial-work' / planned['run_id']
    scope = MemoryStore(environment.root).scope(None, workspace)
    index = EvidenceIndex(environment.root)
    records = index.search(scope, index.cutoff(scope), '', limit=60)['items']
    by_prompt = {row['text']: row['id'] for row in records if row['kind'] == 'user'}
    aliases = {episode.id: environment.refs[episode.id]['id'] if episode.foreign else by_prompt[episode.prompt]
               for episode in environment.fixture.episodes}
    sent = [entry for entry in ledger if entry['run_id'] == planned['run_id']]
    elapsed = max((event.elapsed_ms for event in history.events), default=0) / 1000
    return await score_trial(batch, planned, case, history, aliases, sent, forbidden, elapsed)


async def run(batch: Path, approved: list[int], history_only: int, prepare_only: bool, resume: bool, retry_guard: bool) -> None:
    """准备批准的计划；prepare_only 为真时仅在本机发布、验证和落盘。"""
    protocol = read_json(batch / 'trial-protocol.json')
    review = read_json(batch / 'review-draft.json')
    original = read_json(batch / 'draft.json')
    assert tuple(approved) == APPROVED and history_only == 6 and fingerprint(review) == APPROVED_DRAFT_SHA256
    assert review['protocol_sha256'] == fingerprint(protocol) and review['raw_draft_sha256'] == fingerprint(original)
    assert original['usage_complete'] and original['http_attempts'] == 1
    config = RunConfig.model_validate(protocol['config'])
    assert config.requested_model == 'deepseek-flash' and (config.limits.max_model_turns, config.limits.max_tool_calls) == (10, 20)
    assert config.limits.max_http_attempts_per_turn == 1 and config.limits.run_timeout_seconds == 120
    assert not config.auto_repair and not config.skill_auto and not config.memory.capture and config.context.compression == 'off'
    assert config.permission_policy.read_roots == config.permission_policy.write_roots == ['.']
    assert config.context.max_summary_calls == 0 and set(config.tools) <= TOOLS
    assert all(row['baseline']['status'] == 'failed' and row['reference']['status'] == 'passed' for row in protocol['preflight'])
    cases = {case['id']: case for case in protocol['frozen']}
    schedule = []
    for repeat in range(2):
        for index, case_id in enumerate(cases):
            order = STRATEGIES if (index + repeat) % 2 == 0 else STRATEGIES[::-1]
            for side in order:
                schedule.append({'case_id': case_id, 'strategy': side, 'repeat': repeat,
                                 'run_id': str(uuid5(UUID(batch.name), f'{case_id}/{side}/{repeat}'))})
    assert len(cases) == 6 and len(schedule) == 24 and len(schedule) * 10 + original['http_attempts'] <= 300
    directory = batch / 'runs' / '.evaluations' / batch.name
    directory.mkdir(parents=True, exist_ok=True)
    prior = read_json(directory / 'report.json') if (directory / 'report.json').exists() else None
    ledger_path = directory / 'http-ledger.jsonl'
    assert not (resume and retry_guard)
    resuming = resume or retry_guard
    if resuming:
        expected_reason = 'RuntimeError' if retry_guard else 'StorageError'
        assert not prepare_only and prior and prior['state'] == 'stopped' and prior['stop_reason'] == expected_reason
        assert prior['active_trial'] == schedule[len(prior['trials'])]
        assert prior['schedule'] == schedule
    elif ledger_path.exists() or (prior and (prior['trials'] or prior['state'] not in {'preparing', 'ready'})):
        raise ValueError('已有正式执行记录，不能重新启动并重复计费')
    environment, receipt = load_environment(batch, protocol, review, config)
    write_json(directory / 'approval.json', {'draft_id': review['id'], 'draft_sha256': fingerprint(review),
                                            'selected': list(APPROVED), 'history_only': [history_only], 'receipt': receipt})
    experiment = {**protocol['experiment'], 'seed': 20261112}
    execution = {**protocol, 'experiment': experiment, 'schedule': schedule, 'review_sha256': fingerprint(review)}
    if resuming:
        assert prior['protocol_sha256'] == fingerprint(execution) == fingerprint(read_json(directory / 'protocol.json'))
    write_json(directory / 'protocol.json', execution)
    report = {'id': batch.name, 'name': protocol['suite']['name'], 'suite_version': protocol['suite']['version'],
              'provenance': protocol['suite']['provenance'], 'mode': 'real_model', 'model': config.requested_model,
              'created_at': datetime.now(UTC).isoformat(), 'protocol_sha256': fingerprint(execution), 'experiment': experiment,
              'state': 'preparing', 'stop_reason': None, 'schedule': schedule, 'cases': protocol['suite']['cases'],
              'preflight': [{**row, 'baseline_valid': True} for row in protocol['preflight']], 'trials': [], 'active_trial': None,
              'environments': {}, 'active_consolidation': None, 'human_review_seconds': None,
              'consolidations': [{'project': protocol['project'], **review, 'receipt': receipt}],
              'http_requests': original['http_attempts']}
    if resuming:
        report = prior
    else:
        checkpoint(directory, report)
        report['offline_probes'] = prior.get('offline_probes') if prior else None
        if not report['offline_probes']:
            report['offline_probes'] = await check_local(batch, cases[schedule[0]['case_id']], environment)
        report['state'] = 'ready'
        checkpoint(directory, report)
    if prepare_only:
        print(f"published={len(receipt['notes'])} planned={len(schedule)} offline_probes={len(report['offline_probes'])} "
              f"formal_trials=0 total_http={report['http_requests']}", flush=True)
        return
    key = os.environ.get(config.api_key_env or '', '')
    if not key:
        raise ValueError('当前配置未提供模型凭据')
    report.update(state='running', stop_reason=None)
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding='utf-8').splitlines()] if resuming else []
    forbidden = sorted({name for case in cases.values() for name in case['forbidden_evidence']})
    if resuming:
        planned = report['active_trial']
        spent_ids = {row['run_id'] for row in report['trials']} | {planned['run_id']}
        assert {entry['run_id'] for entry in ledger} <= spent_ids
        assert report['http_requests'] == original['http_attempts'] + len(ledger)
        assert [entry['number'] for entry in ledger] == list(range(2, len(ledger) + 2))
    if resume:
        row = await recover_trial(batch, planned, cases[planned['case_id']], environment, ledger, forbidden)
        report['trials'].append(row)
        report['active_trial'] = None
        report.setdefault('recovered_grades', []).append({'run_id': row['run_id'], 'reason': 'windows_path_limit', 'extra_http': 0})
        print(f"Recovered {row['case_id']} {row['strategy']} artifact={row['grade']['status']} requests={row['http_attempts']}", flush=True)
    if retry_guard:
        saved = RunStore.read(environment.root, planned['run_id'])
        sent = [entry for entry in ledger if entry['run_id'] == planned['run_id']]
        assert saved.result and saved.result.stop_reason == 'internal_error' and saved.result.http_attempts == len(sent) + 1
        assert report['http_requests'] + (24 - len(report['trials'])) * 10 <= 300
        replacement = {**planned, 'run_id': str(uuid5(UUID(planned['run_id']), 'retry-json-escape-check'))}
        discarded = {**planned, 'reason': 'guard_misread_json_escape', 'replacement_run_id': replacement['run_id'],
                     'http_attempts': len(sent), 'model_usage': model_usage(saved, sent)}
        write_json(directory / 'protocol-before-guard-retry.json', execution)
        schedule[len(report['trials'])] = replacement
        report['schedule'] = schedule
        report['active_trial'] = None
        report.setdefault('infrastructure_retries', []).append(discarded)
        execution['infrastructure_retry'] = {key: discarded[key] for key in ('run_id', 'replacement_run_id', 'reason')}
        write_json(directory / 'protocol.json', execution)
        report['protocol_sha256'] = fingerprint(execution)
        print(f"Retrying {planned['case_id']} after an egress-check false positive; prior HTTP={len(sent)}", flush=True)
    checkpoint(directory, report)
    print(f'published={len(receipt["notes"])} planned={len(schedule)} draft_requests={original["http_attempts"]}', flush=True)
    try:
        for planned in schedule[len(report['trials']):]:
            report['active_trial'] = planned
            checkpoint(directory, report)
            row = await trial(batch, directory, planned, cases[planned['case_id']], environment, key, ledger,
                              original['http_attempts'], forbidden)
            report['trials'].append(row)
            report['active_trial'] = None
            report['http_requests'] = original['http_attempts'] + len(ledger)
            checkpoint(directory, report)
            print(f"{len(report['trials'])}/24 {row['strategy']} {row['case_id']} repeat={row['repeat'] + 1} "
                  f"stop={row['stop_reason']} artifact={row['grade']['status']} total_http={report['http_requests']}", flush=True)
        report['state'] = 'completed'
    except BaseException as error:
        report.update(state='stopped', stop_reason=type(error).__name__, http_requests=original['http_attempts'] + len(ledger))
        raise
    finally:
        checkpoint(directory, report)


def main() -> None:
    """读取批次及人工决定，只启动经过同一指纹核对的试点。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch', type=UUID, help='已预检和审阅的试点批次')
    parser.add_argument('--approve', type=int, nargs='+', required=True, help='明确批准的条目编号')
    parser.add_argument('--history-only', type=int, required=True, help='只留在历史中的条目编号')
    parser.add_argument('--prepare-only', action='store_true', help='仅在本机发布、检查和编排，零外部请求')
    parser.add_argument('--resume-after-grading-error', action='store_true', help='重用已落盘试次，补评分后续跑')
    parser.add_argument('--retry-guarded-trial', action='store_true', help='保留误拦截的已用请求，仅重试受影响的试次')
    args = parser.parse_args()
    asyncio.run(run(PILOT / str(args.batch), args.approve, args.history_only, args.prepare_only,
                    args.resume_after_grading_error, args.retry_guarded_trial))


if __name__ == '__main__':
    main()
