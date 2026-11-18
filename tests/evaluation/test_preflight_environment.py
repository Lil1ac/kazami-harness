"""复现预检基础设施错误被误用于换题；真实运行Campaign预检与冻结流程，不调用模型或Docker。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def module():
    """加载环境变量指定的调度源码；默认当前冻结源码，返回隔离模块供红绿对照。"""
    path = Path(os.environ.get('EV3_CAMPAIGN_SOURCE', 'src/agent_harness/evaluation/campaign.py'))
    spec = importlib.util.spec_from_file_location('ev3_preflight_under_test', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def scenario(tmp_path, monkeypatch, module):
    """构造一题一候补的真实调度器；tmp_path隔离落盘，monkeypatch替代Docker及评分边界。

    返回调度器、边界调用记录和可指定的原题结果字典；不初始化费用账本或任何模型传输。
    """
    campaign = module.Campaign(tmp_path / 'round', Path('evals/swe-three-arm'), tmp_path / 'work', Path('.runtime/swe-env/Scripts/python.exe'))
    campaign.root.mkdir();campaign.work_root.mkdir()
    campaign.selection = {'selected': [['original', 'repo/example', '<15 min fix']],
        'reserves': [['reserve', 'repo/example', '<15 min fix']], 'sentinels': [['original', 'repo/example', '<15 min fix']]}
    campaign.state = {'preflight': {}, 'active': None, 'replacements': [], 'protocol_sha256': 'offline'}
    calls = []  # 保存实际进入评分边界的题号，以断言基础设施错误不会消耗候补。
    outcome = {'reason': 'preflight_environment'}  # 原题默认模拟镜像限流；invalid_case用于合法替换对照。

    def suite(identifier, phase):
        """为identifier生成隔离输入路径；phase仅标记阶段，返回路径且不包含模型输入或参考答案。"""
        return tmp_path / (identifier + '.json')

    async def evaluate(path, config, experiment, runs_root, work_root, api_key, **options):
        """模拟官方预检结果并记录题号；核对空api_key和preflight_only，返回无模型试次的报告。"""
        assert not api_key and options['preflight_only']
        calls.append(path.stem)
        failed = path.stem == 'original'
        return {'id': path.stem, 'state': 'stopped' if failed else 'preflight_passed',
                'stop_reason': outcome['reason'] if failed else None, 'trials': [], 'preflight': [], 'environments': {}}

    def save(folder, report):
        """将report原样保存至隔离folder；不测试无关的汇总渲染，返回None。"""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'report.json').write_text(json.dumps(report), encoding='utf-8')

    def cleanup(report):
        """保留无容器夹具原件；report只作完整边界参数，返回None。"""
        assert not report['trials']

    # 本测试只覆盖换题边界；证书真实性由 test_recovery 的原件夹具覆盖。
    monkeypatch.setattr('agent_harness.evaluation.preflight.make_certificate', lambda folder: {"fixture": folder.name})
    monkeypatch.setattr('agent_harness.evaluation.preflight.verify_certificate', lambda *args, **kwargs: {})
    monkeypatch.setattr(campaign, 'record_bytes', lambda identifier: b'{}')
    monkeypatch.setattr(campaign, 'suite', suite)
    monkeypatch.setattr(campaign, 'cleanup', cleanup)
    monkeypatch.setattr(module, 'evaluate', evaluate)
    monkeypatch.setattr(module, 'save_report', save)
    return campaign, calls, outcome


@pytest.mark.parametrize('cached', [False, True])
def test_infrastructure_failure_never_consumes_reserve(scenario, cached):
    """新发生或已缓存的环境失败均须停止；scenario提供边界，cached表示恢复路径，返回None。"""
    campaign, calls, outcome = scenario
    if cached:
        campaign.state['preflight']['formal:original'] = {'report_id': 'original', 'passed': False, 'reason': outcome['reason']}
    with pytest.raises(ValueError, match='基础设施'):
        asyncio.run(campaign.freeze_formal())
    assert calls == ([] if cached else ['original'])
    assert not campaign.state['replacements'] and not campaign.state.get('formal_ids')
    assert campaign.state['active'] is None
    if not cached:
        assert (campaign.runs_root / '.evaluations/original/report.json').is_file()


def test_invalid_case_keeps_authorized_reserve_selection(scenario):
    """真实题目预检不满足门槛时仍允许同分层候补；scenario提供隔离状态，返回None。"""
    campaign, calls, outcome = scenario
    outcome['reason'] = 'invalid_case'
    assert asyncio.run(campaign.freeze_formal()) == ['reserve']
    assert calls == ['original', 'reserve']
    assert campaign.state['replacements'][0]['replacement'] == 'reserve'
