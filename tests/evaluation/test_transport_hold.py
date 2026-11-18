"""明确批准的传输未知预留回归；隔离账本验证授权范围与预算，不调用模型。"""
from pathlib import Path
import pytest
from agent_harness.evaluation.budget import RESERVATION_PICOS
from agent_harness.evaluation.campaign_budget import CampaignBudget, atomic_json, receipt_sha256
from agent_harness.runtime.runner import AgentStopped


def _save_projection():
    """隔离测试无需报告投影；无参数、无副作用，返回None。"""


def _two_approved_holds(tmp_path: Path) -> CampaignBudget:
    """在tmp_path创建分别属于400和传输失败的两笔批准预留，返回重载后的守卫；无外部请求。"""
    path = tmp_path / 'ledger.json'
    budget = CampaignBudget(path, 'fixed')
    for index, code in enumerate(('model_bad_request', 'model_transport')):
        budget.bind('formal', {}, _save_projection)
        budget.authorize(f'approved-{index}', 1, 'execution')
        budget.settle(f'approved-{index}', 1, None, None, code)
        row = budget.state['requests'][-1]
        budget.state.setdefault('approved_holds', []).append({
            'run_id': row['run_id'], 'turn': 1, 'receipt_sha256': receipt_sha256(row),
            'reserved_usd_picos': RESERVATION_PICOS, 'approval_sha256': f'offline-approval-{index}'})
        budget.state['stop_reason'] = None
        budget.save()
        budget = CampaignBudget(path, 'fixed')
    return budget


def test_two_holds_stay_reserved_and_next_unknown_stops(tmp_path: Path):
    """tmp_path隔离真实守卫；旧批准保留金额，新传输未知必须停止，重启也不能自行解除。"""
    budget = _two_approved_holds(tmp_path)
    assert budget.state['reserved_usd_picos'] == 2 * RESERVATION_PICOS
    assert budget.state['calls_admitted'] == 2 and not budget.has_pending_request()
    assert all(row['status'] == 'unknown' for row in budget.state['requests'])
    budget.bind('formal', {}, _save_projection)
    budget.authorize('new', 1, 'execution')
    assert budget.state['reserved_usd_picos'] == 3 * RESERVATION_PICOS
    budget.settle('new', 1, None, None, 'model_transport')
    restored = CampaignBudget(budget.path, 'fixed')
    restored.bind('formal', {}, _save_projection)
    with pytest.raises(AgentStopped, match='unknown_usage'):
        restored.authorize('forbidden', 1, 'execution')
    assert restored.state['calls_admitted'] == 3


@pytest.mark.parametrize('field', ['receipt_sha256', 'approval_sha256', 'turn', 'reserved_usd_picos'])
def test_transport_hold_rejects_invalid_approval_binding(tmp_path: Path, field: str):
    """field选择篡改的授权字段；tmp_path隔离原件，任何不匹配都须在接纳新请求前拒绝。"""
    budget = _two_approved_holds(tmp_path)
    hold = budget.state['approved_holds'][-1]
    hold[field] = {'receipt_sha256': 'wrong', 'approval_sha256': '', 'turn': 99,
                   'reserved_usd_picos': 0}[field]
    atomic_json(budget.path, budget.state)
    with pytest.raises(ValueError, match='回执'):
        CampaignBudget(budget.path, 'fixed')
