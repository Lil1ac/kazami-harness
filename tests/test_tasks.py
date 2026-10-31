"""工作项事务、依赖、证据及运行接续的行为回归。"""

from pathlib import Path
import pytest

from agent_harness.tasks import WorkStore, TaskService, TaskCreate, TaskUpdate, TaskError, ExecutionLease


def create(service: TaskService, name: str, **options) -> str:
    """为 service 创建 name 工作项；options 指定父子或依赖，返回稳定编号。"""
    return service.create(TaskCreate(request_id=name, subject=name, **options))["id"]


def update(service: TaskService, identifier: str, status: str, **options) -> dict:
    """以 identifier 最新版本执行 status 变更；options 提供完成证据，返回回执。"""
    item = service.get(identifier)
    return service.update(TaskUpdate(request_id=f"{identifier}:{item['revision']}:{status}", task_id=identifier, expected_revision=item["revision"], status=status, **options))


def test_dependency_idempotency_and_reopen(tmp_path: Path):
    """同请求不重复创建，依赖由宿主阻塞；重新打开前置后下游须复核。"""
    service = TaskService(WorkStore(tmp_path / "runs"), "session")
    first = create(service, "接口")
    assert create(service, "接口") == first
    second = create(service, "前端", depends_on=[first])
    assert service.get(second)["blocked_reason"]
    with pytest.raises(TaskError, match="前置"):
        update(service, second, "completed", completion_summary="自报完成", evidence_refs=[{"kind": "manual_confirmation"}])
    update(service, first, "completed", completion_summary="人工核对接口", evidence_refs=[{"kind": "manual_confirmation"}])
    assert service.get(second)["blocked_reason"] is None
    update(service, second, "completed", completion_summary="已核对页面", evidence_refs=[{"kind": "manual_confirmation"}])
    with pytest.raises(TaskError) as error:
        update(TaskService(service.store, 'session', 'other-run'), first, 'in_progress')
    assert error.value.code == 'task_reopen_required'
    update(service, first, "pending")
    assert service.get(second)["blocked_reason"]
    assert service.get(second)["completion_summary"] == "已核对页面"
    update(service, second, "pending")
    assert not service.get(second)["review_required"]


def test_graph_cycle_rolls_back_and_parent_aggregates(tmp_path: Path):
    """依赖与父子组合环整笔回滚；父进度仅从子任务派生。"""
    service = TaskService(WorkStore(tmp_path), "session")
    parent = create(service, "功能")
    child = create(service, "实现", parent_task_id=parent)
    with pytest.raises(TaskError) as error:
        create(service, "循环", parent_task_id=parent, depends_on=[parent])
    assert error.value.code == "task_dependency_cycle"
    assert len(service.store.snapshot("session")["items"]) == 2
    update(service, child, "completed", completion_summary="人工核对完成", evidence_refs=[{"kind": "manual_confirmation"}])
    assert service.get(parent)["status"] == "completed"
    assert service.store.events(0)


def test_stale_update_evidence_and_cross_session(tmp_path: Path):
    """过期更新、伪造证据、跨会话读均被拒绝；失败不改变已保存状态。"""
    service = TaskService(WorkStore(tmp_path), "session")
    identifier = create(service, "实现")
    body = TaskUpdate(request_id="edit", task_id=identifier, expected_revision=1, description="新范围")
    assert service.update(body) == service.update(body)
    with pytest.raises(TaskError) as error:
        service.update(body.model_copy(update={"request_id": "stale"}))
    assert error.value.code == "task_revision_conflict"
    with pytest.raises(TaskError, match="证据"):
        update(service, identifier, "completed", completion_summary="没有证据")
    with pytest.raises(TaskError):
        TaskService(service.store, "other").get(identifier)


def test_execution_lease_excludes_other_runner(tmp_path: Path):
    """同数据目录的第二执行者被拒绝，释放后可以重新申请。"""
    first, second = ExecutionLease(tmp_path), ExecutionLease(tmp_path)
    first.acquire()
    try:
        with pytest.raises(TaskError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_parent_dependency_does_not_bypass_child_review(tmp_path: Path):
    """子项原先完成但因上游变化待复核时，下游不能通过其父项间接开工。"""
    service = TaskService(WorkStore(tmp_path), "session")
    upstream = create(service, "接口约定")
    parent = create(service, "交付")
    child = create(service, "前端", parent_task_id=parent, depends_on=[upstream])
    consumer = create(service, "集成", depends_on=[parent])
    for identifier in (upstream, child):
        update(service, identifier, "completed", completion_summary="已人工核对", evidence_refs=[{"kind": "manual_confirmation"}])
    assert not service.get(consumer)["blocked_reason"]
    update(service, upstream, "pending")
    assert service.get(parent)["status"] == "pending"
    assert service.get(consumer)["blocked_reason"]
