"""用真实Runner、账本和调度器复现检查点保存失败；全部HTTP使用本地MockTransport。"""
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from agent_harness.config import load_config
from agent_harness.evaluation.campaign_budget import CampaignBudget, atomic_json
from agent_harness.evaluation.engine import evaluate, save_report
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.runtime.runner import AgentStopped, run_task
from agent_harness.storage.runs import RunStore
from tests.context.test_compression import _manifest
from tests.evaluation.test_evaluation import suite_file
from tests.evaluation.test_three_arm import usage
from tests.helpers.streaming import model_stream_response


def new_budget(path):
    """在path创建并绑定隔离正式账本，返回守卫；不读取真实账本或网络凭据。"""
    budget = CampaignBudget(path, 'checkpoint-offline')
    budget.bind('formal', {}, lambda: None)  # 绑定阶段不产生请求。
    return budget


def test_authorization_save_failure_stops_before_http(tmp_path):
    """真实Runner的发单前保存失败须明确停轮并释放未发送预留；tmp_path隔离全部副作用。"""
    budget = new_budget(tmp_path / 'ledger.json')
    manifest = _manifest(tmp_path)
    attempts = []  # 真实传输边界记录，必须始终为空。

    def broken_checkpoint():
        """模拟账本已落盘、报告原子替换失败的边界；不涉及HTTP，不返回。"""
        raise PermissionError(13, 'offline checkpoint sharing violation')

    def transport(request):
        """若错误越过发单门禁则记录request并失败；本测试不允许实际进入HTTP。"""
        attempts.append(request)
        raise AssertionError('request must remain unsent')

    budget._report_save = broken_checkpoint
    result = asyncio.run(run_task(manifest, tmp_path / 'runs', 'offline',
        model_request_guard=budget, transport=httpx.MockTransport(transport))).result
    events = RunStore.read(tmp_path / 'runs', str(manifest.run_id)).events
    assert not attempts and not any(e.type == 'model_request_started' for e in events)
    assert result.stop_reason == 'budget_checkpoint_error'
    assert budget.stop_reason == 'budget_checkpoint_error'
    assert budget.state['requests'][-1]['status'] == 'not_sent'
    assert budget.state['reserved_usd_picos'] == 0
    assert json.loads(budget.path.read_text(encoding='utf-8'))['requests'][-1]['status'] == 'not_sent'


def test_engine_never_continues_after_checkpoint_failure(tmp_path):
    """运行真实多试次调度，在首次预留时一次性失败；必须只留下一个无效试次而非续跑。"""
    budget = new_budget(tmp_path / 'ledger.json')
    config = load_config(Path('evals/swe-three-arm/deepseek.toml'))
    suite = suite_file(tmp_path / 'suite')
    failed = []  # 仅首个预留后的检查点失败，后续保存可用以落盘停止状态。
    requests = []  # 所有传输均被替换为本地拒绝边界。

    def progress(report):
        """首次出现reserved时抛保存错误；report为真实当前报告，后续回调正常返回None。"""
        if not failed and budget.state['requests'] and budget.state['requests'][-1]['status'] == 'reserved':
            failed.append(True)
            raise PermissionError(13, 'offline progress report sharing violation')

    def transport(request):
        """记录意外发单并失败；请求对象只存在内存，不调用外部服务。"""
        requests.append(request)
        raise AssertionError('no request after persistence failure')

    report = asyncio.run(evaluate(suite, config,
        Experiment(groups=preset_groups(['B', 'C']), max_cost_cents=2800),
        tmp_path / 'runs', tmp_path / 'work', 'offline',
        campaign_budget=budget, campaign_phase='formal', progress=progress,
        transport=httpx.MockTransport(transport)))
    assert not requests
    assert report['state'] == 'stopped'
    assert report['stop_reason'] == 'budget_checkpoint_error'
    assert len(report['trials']) == 1
    assert report['trials'][0]['comparison_valid'] is False


def test_pending_request_stops_in_memory_before_next_trial(tmp_path):
    """同一进程存在未结算请求时，下一个试次必须在计数增加之前得到整轮停止原因。"""
    budget = new_budget(tmp_path / 'ledger.json')
    budget.authorize('prior', 1, 'execution')
    with pytest.raises(AgentStopped, match='unknown_usage'):
        budget.authorize('next', 1, 'execution')
    assert budget.state['calls_admitted'] == 1
    assert budget.stop_reason == 'unknown_usage'


def test_settlement_save_failure_preserves_received_cost(tmp_path):
    """已收到可信usage后报告保存失败，须保留收费并停轮，不将已发送请求改成未发送。"""
    budget = new_budget(tmp_path / 'ledger.json')
    budget.authorize('one', 1, 'execution')

    def broken_checkpoint():
        """只模拟结算后的报告保存失败，不改变回执或收费，不正常返回。"""
        raise PermissionError(13, 'offline settlement report failure')

    budget._report_save = broken_checkpoint
    with pytest.raises(AgentStopped, match='budget_checkpoint_error'):
        budget.settle('one', 1, usage(), 'deepseek-flash')
    saved = json.loads(budget.path.read_text(encoding='utf-8'))
    assert saved['requests'][0]['status'] == 'settled'
    assert saved['known_usd_picos'] > 0 and saved['reserved_usd_picos'] == 0
    assert saved['stop_reason'] == 'budget_checkpoint_error'


@pytest.mark.skipif(os.name != 'nt', reason='需要Windows文件共享语义')
@pytest.mark.parametrize('kind', ['ledger', 'report'])
def test_atomic_replace_survives_short_windows_reader(tmp_path, kind):
    """真实Windows读句柄短暂阻止替换时须有限重试成功；kind区分账本和报告写入入口。"""
    report = {'experiment': Experiment(groups=preset_groups(['B', 'C'])).model_dump(),
              'trials': [], 'schedule': [], 'cases': []}
    path = tmp_path / ('ledger.json' if kind == 'ledger' else 'report.json')
    atomic_json(path, {'old': True})
    ready = Event()  # 读句柄已建立；主线程此后才尝试写入。
    released = Event()  # 无论测试成败都释放读句柄，避免污染下一用例。
    failures = []  # 将辅助线程异常带回测试主线程。
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def reader():
        """持有不共享删除权限的只读句柄80毫秒；路径固定于隔离目录，退出时必定关闭句柄。"""
        handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            failures.append(ctypes.get_last_error())
            ready.set()
            return
        try:
            ready.set()
            released.wait(.08)
        finally:
            kernel.CloseHandle(handle)

    thread = Thread(target=reader)
    thread.start()
    try:
        assert ready.wait(2) and not failures
        if kind == 'ledger':
            atomic_json(path, {'new': True})
        else:
            save_report(tmp_path, report)
    finally:
        released.set()
        thread.join(2)
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved.get('new') if kind == 'ledger' else 'summary' in saved


def test_native_adapter_preserves_pending_budget_stop(tmp_path):
    """真实mini适配器遇到已有未结算预留时返回整轮停止原因，不能伪装成模型失败或发单。"""
    from types import SimpleNamespace
    from uuid import uuid4
    from agent_harness.evaluation.mini import run_mini

    budget = new_budget(tmp_path / 'ledger.json')
    budget.authorize('prior', 1, 'execution')
    config = load_config(Path('evals/swe-three-arm/deepseek.toml'))
    attempts = []  # 原生适配器也必须保持零实际HTTP尝试。

    def transport(request):
        """记录越过停止门禁的请求并立即失败；本测试不连接外部模型。"""
        attempts.append(request)
        raise AssertionError('pending budget must stop native adapter')

    result, _ = asyncio.run(run_mini(config, SimpleNamespace(image='sha256:offline', directory=tmp_path),
        tmp_path, str(uuid4()), 'offline task', budget, 'offline', tmp_path / 'native.json',
        transport=httpx.MockTransport(transport)))
    assert not attempts and result.http_attempts == 0
    assert result.stop_reason == 'unknown_usage'
    assert budget.stop_reason == 'unknown_usage'
    assert budget.state['calls_admitted'] == 1
