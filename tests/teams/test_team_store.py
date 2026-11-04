"""团队事务、共享任务认领、消息幂等及依赖失效的并发回归。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from agent_harness.teams.store import TeamStore
from agent_harness.work.tasks import TaskService, TaskCreate, TaskUpdate, WorkStore, TaskError


def prepared(root):
    """在root建立两位同授权成员与两项依赖任务，返回仓储、任务服务、会话、成员和任务编号。"""
    session, parent = str(uuid4()), str(uuid4())
    store = TeamStore(root)
    service = TaskService(WorkStore(root), session, parent)
    store.create(session, 'create', '测试团队', str(root), [1, 2], parent)
    members = [store.add_member(session, 'add:' + name, name, ['src'])['member_id'] for name in ['甲', '乙']]
    first = service.create(TaskCreate(request_id='a', subject='后端', acceptance=['固定源码']))['id']
    second = service.create(TaskCreate(request_id='b', subject='前端', acceptance=['核对接口'], depends_on=[first]))['id']
    for identifier in [first, second]:
        store.offer(session, 'offer:' + identifier, {'task_id': identifier, 'objective': '实现', 'member_id': None, 'write_scope': ['src']})
    return store, service, session, members, [first, second]


def test_atomic_claim_and_dependency_lock(tmp_path):
    """两个真实线程同时认领同一候选仅一位成功；下游在上游未固定时始终无法认领。"""
    store, service, session, members, tasks = prepared(tmp_path)
    def claim(member):
        """以member独立事务认领，返回唯一候选或None，不执行模型。"""
        return store.claim(session, 'claim:' + member, member, service.run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, members))
    assert sum(value is not None for value in results) == 1
    selected = next(value for value in results if value)
    assert selected['task_id'] == tasks[0]
    loser = next(m for m, result in zip(members, results) if result is None)
    assert store.claim(session, 'again', loser, service.run_id) is None
    with pytest.raises(TaskError, match='先停止'):
        store.offer(session, 'unsafe-transfer', {'task_id': tasks[0], 'objective': '转派', 'member_id': loser, 'write_scope': ['src']})
    assert store.get(session)['tasks'][0]['member_id'] == selected['member_id']


def test_messages_idempotency_and_sender_validation(tmp_path):
    """消息成功落盘可重试且正文不重复；编号换正文、冒充发送者或跨团队收件人均拒绝。"""
    store, service, session, members, tasks = prepared(tmp_path)
    result = store.message(session, 'msg', 'user', members[0], '保持批准范围')
    assert store.message(session, 'msg', 'user', members[0], '保持批准范围') == result
    assert len(store.get(session)['messages']) == 1
    for sender, recipient in [('外部身份', members[0]), ('lead', str(uuid4()))]:
        with pytest.raises(TaskError):
            store.message(session, str(uuid4()), sender, recipient, '无权限指示')
    with pytest.raises(TaskError, match='不能更换内容'):
        store.message(session, 'msg', 'user', members[0], '不同正文')


def test_changed_upstream_invalidates_fixed_downstream(tmp_path):
    """上游条件变化使已固定下游失效；不能绕过任务清单直接交接旧版本。"""
    store, service, session, members, tasks = prepared(tmp_path)
    def ready(state, db):
        """模拟已经持久发布的两份固定成果，供业务条件失效检查；不创建模型记录。"""
        for item in state['tasks']:
            item.update(state='ready', run_id=str(uuid4()))
        state['tasks'][1]['dependency_versions'] = {tasks[0]: state['tasks'][0]['run_id']}
        state['stage_ready'] = True
    store.mutate(session, 'fixed-fixture', {}, ready)
    current = service.get(tasks[0])
    service.update(TaskUpdate(request_id='new-contract', task_id=tasks[0], expected_revision=current['revision'], acceptance=['新的接口约定']))
    assert store.claim(session, 'after-change', members[0], service.run_id) is None
    state = store.get(session)
    assert not state['stage_ready']
    assert all(item['state'] == 'stale' for item in state['tasks'])


def test_scope_and_revision_reject_invalid_claim(tmp_path):
    """窄范围成员不能认领范围更大的授权；过期版本管理决定没有副作用。"""
    store, service, session, members, tasks = prepared(tmp_path)
    narrow = store.add_member(session, 'narrow', '窄成员', ['src/only.py'])['member_id']
    assert store.claim(session, 'narrow-claim', narrow, service.run_id) is None
    state = store.get(session)
    def change(current, db):
        """测试性变更目标；过期检查应在进入回调前拒绝。"""
        raise AssertionError('不得执行过期回调')
    with pytest.raises(TaskError, match='团队已变化'):
        store.mutate(session, 'stale', {}, change, expected_revision=state['revision'] - 1)
    assert store.get(session) == state


def controller_for(root, session, run_id):
    """为root/session/run_id构建无模型、无进程的负责人控制器，返回控制器和仅记录事件的列表。"""
    from types import SimpleNamespace
    from agent_harness.teams.controller import TeamController
    from uuid import UUID
    events = []
    def emit(kind, payload):
        """记录kind/payload供状态断言，返回None；不创建伪造运行日志。"""
        events.append((kind, payload))
    context = SimpleNamespace(runs_root=root, manifest=SimpleNamespace(run_id=UUID(run_id), session_id=UUID(session)), emit=emit)
    subagents = SimpleNamespace(executor=SimpleNamespace())
    return TeamController(context, subagents), events


def test_restart_does_not_replay_unknown_claim(tmp_path):
    """旧成员缺可信子日志时重新挂接只标待核对，不自动请求模型或释放未知认领。"""
    store, service, session, members, tasks = prepared(tmp_path)
    store.claim(session, 'claim', members[0], service.run_id)
    def started(state, db):
        """模拟宿主中断在开始登记之后、子日志完成之前，返回None。"""
        state['tasks'][0]['run_id'] = str(uuid4())
    store.mutate(session, 'interrupted-fixture', {}, started)
    controller, events = controller_for(tmp_path, session, str(uuid4()))
    controller.reconcile()
    state = store.get(session)
    assert state['tasks'][0]['state'] == 'needs_review'
    assert state['members'][0]['status'] == 'needs_review'
    assert not state['stage_ready']
    assert len(events) == 1
    with pytest.raises(TaskError, match='先停止'):
        store.offer(session, 'transfer', {'task_id': tasks[0], 'objective': '接管', 'member_id': members[1], 'write_scope': ['src']})


def test_message_ack_only_after_actual_response(tmp_path):
    """取到消息但模型失败时不推进持久游标，新段重送；成功确认后跨段不重复交付。"""
    store, service, session, members, tasks = prepared(tmp_path)
    store.message(session, 'notify', members[0], 'lead', '旧摘要不能授予写权限')
    first, _ = controller_for(tmp_path, session, str(uuid4()))
    content, seq = first.message_input()
    assert seq == 1 and '不是用户授权' in content
    assert first.message_input() == (None, 0)
    second, _ = controller_for(tmp_path, session, str(uuid4()))
    assert second.message_input()[1] == 1
    second.acknowledge(seq)
    third, _ = controller_for(tmp_path, session, str(uuid4()))
    assert third.message_input() == (None, 0)


def test_team_owner_cannot_be_bypassed_by_ordinary_delegation(tmp_path):
    """普通C07和任务重开不能绕过团队尚未停止的认领；安全失败后可明确转派给另一成员。"""
    store, service, session, members, tasks = prepared(tmp_path)
    store.claim(session, 'claim', members[0], service.run_id)
    with pytest.raises(TaskError, match='普通委派'):
        service.claim_delegation(tasks[0], str(uuid4()))
    current = service.get(tasks[0])
    with pytest.raises(TaskError, match='先停止'):
        service.update(TaskUpdate(request_id='unsafe-release', task_id=tasks[0], expected_revision=current['revision'], status='pending'))
    def stopped(state, db):
        """模拟可信取消和清理已完成的发布，不模拟模型成功；只允许随后明确重新分配。"""
        state['tasks'][0]['state'] = 'failed'
        state['members'][0].update(status='failed', task_id=None, active_run_id=None)
    store.mutate(session, 'confirmed-failure', {}, stopped)
    store.offer(session, 'transfer', {'task_id': tasks[0], 'member_id': members[1], 'objective': '接续失败项', 'write_scope': ['src']})
    assert store.claim(session, 'claim-new', members[1], service.run_id)['member_id'] == members[1]


def test_duplicate_cross_segment_call_id_is_not_evidence():
    """不同执行段复用同名调用时拒绝歧义引用，不随意借其他段回执支撑报告结论。"""
    from agent_harness.schemas import RunEvent, RunResult
    from agent_harness.subagents.reports import finalize_report
    from tests.subagents.test_subagents import report
    events = [RunEvent(run_id=uuid4(), seq=1, elapsed_ms=0, type='tool_result', payload={'call_id': 'same-id', 'name': 'write_file', 'result': {'ok': True, 'data': {'path': path}}}) for path in ['first.py', 'second.py']]
    result = RunResult(run_id=uuid4(), execution_status='completed', final_answer=report(ids=['same-id']))
    checked = finalize_report(result, events, role='develop')
    assert checked.partial and result.stop_reason == 'subagent_report_evidence_invalid'
    assert len({source.run_id for source in checked.evidence}) == 2


def test_cancelled_upstream_invalidates_ready_and_parent_blocks_claim(tmp_path):
    """取消已固定上游或父分组都不能继续交接；团队不能绕过C04原有的取消与分组依赖。"""
    store, service, session, members, tasks = prepared(tmp_path)
    def ready(state, db):
        """模拟可信已固定成果以验证后续取消失效，不伪造模型成功日志。"""
        for task in state['tasks']:
            task.update(state='ready', run_id=str(uuid4()))
        state['stage_ready'] = True
    store.mutate(session, 'ready-fixture', {}, ready)
    original = service.get(tasks[0])
    service.update(TaskUpdate(request_id='cancel-upstream', task_id=tasks[0], expected_revision=original['revision'], status='cancelled'))
    assert store.claim(session, 'after-cancel', members[0], service.run_id) is None
    assert all(task['state'] == 'stale' for task in store.get(session)['tasks'])
    parent = service.create(TaskCreate(request_id='parent', subject='父分组'))['id']
    child = service.create(TaskCreate(request_id='child', subject='子任务', parent_task_id=parent, acceptance=['固定源码']))['id']
    store.offer(session, 'child', {'task_id': child, 'member_id': members[0], 'objective': '实现子项', 'write_scope': ['src']})
    current = service.get(parent)
    service.update(TaskUpdate(request_id='cancel-parent', task_id=parent, expected_revision=current['revision'], status='cancelled'))
    assert store.claim(session, 'after-parent-cancel', members[0], service.run_id) is None
    assert next(task for task in store.get(session)['tasks'] if task['task_id'] == child)['state'] == 'stale'


def test_explicit_reopen_invalidates_ready_without_changing_business_text(tmp_path):
    """用户按原条件重新打开任务也必须让旧成果失效，不能与子执行正常释放pending混淆。"""
    store, service, session, members, tasks = prepared(tmp_path)
    def ready(state, db):
        """模拟完整固定成果及待采纳状态，返回None，不创建模型记录。"""
        for task in state['tasks']:
            task.update(state='ready', run_id=str(uuid4()))
        state['stage_ready'] = True
    store.mutate(session, 'ready-fixture', {}, ready)
    current = service.get(tasks[0])
    service.update(TaskUpdate(request_id='reopen-same-scope', task_id=tasks[0], expected_revision=current['revision'], status='pending'))
    state = store.get(session)
    assert not state['stage_ready']
    assert all(task['state'] == 'stale' for task in state['tasks'])
