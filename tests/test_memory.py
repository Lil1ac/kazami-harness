"""长期记忆的领域回归：作用域、生命周期、来源、分类边界和读取一致性。"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.memory import (MemoryChange, MemoryContent, MemoryController, MemoryError,
                                  MemoryGet, MemorySearch, MemoryService, MemoryStore, ranked, encode)
from tests.memory_helpers import fact_content
from agent_harness.schemas import MemoryPolicy, RunConfig, RunManifest, RunOrigin, TaskSpec


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    """在 tmp_path 建立独立普通项目并返回服务；运行记录位于不同子目录。"""
    project = tmp_path / "project"
    project.mkdir()
    return MemoryService(MemoryStore(tmp_path / "runs"), project)


def save(service: MemoryService, text: str = "接口保留旧字段，客户仍未完成升级", **options) -> dict:
    """给 service 保存 text；options 提供类型及条件，返回人工确认条目。"""
    return service.create(fact_content(title=text[:80], content=text, **options), str(uuid4()), provenance={"kind": "manual_confirmation"})


def change(service: MemoryService, item: dict, action: str, **options) -> dict:
    """对 item 当前版本执行 action；options 可指定正文，返回新修订。"""
    return service.update(item["id"], MemoryChange(request_id=uuid4(), expected_revision=item["revision"], action=action, **options))


def controller(service: MemoryService, *, workspace: Path | None = None, policy: MemoryPolicy | None = None) -> MemoryController:
    """用 service 来源创建独立 Run 读取集；workspace 可模拟隔离目录，policy=None 沿用默认。"""
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(workspace=workspace or service.workspace, prompt="修改接口兼容字段"),
                           config=RunConfig(memory=policy or MemoryPolicy()),
                           origin=RunOrigin(kind="project", preset_id="test", registered_path=service.source, execution_mode="isolated"))

    def emit(kind, payload):
        """收集器占位；kind/payload 不写磁盘，领域测试只观察返回值。"""

    return MemoryController(service.store.root, manifest, emit)


def test_read_is_side_effect_free_and_scope_isolated(service, tmp_path):
    """空读取不创建库；不同项目不串入，同来源隔离工作树可读取。"""
    assert service.list()["items"] == []
    assert not service.store.path.exists()
    item = save(service)
    other = tmp_path / "other"
    other.mkdir()
    assert MemoryService(service.store, other).list()["items"] == []
    assert controller(service, workspace=other).items[0]["id"] == item["id"]
    assert MemoryService(MemoryStore(service.store.root), service.source).list()["items"][0]["id"] == item["id"]


def test_idempotency_conflict_and_old_revision(service):
    """同编号不重复执行，不同载荷冲突；修订可按序号回读，旧请求不能覆盖新版本。"""
    content = fact_content(title="约定", content="保持返回字段")
    op = str(uuid4())
    item = service.create(content, op, provenance={"kind": "manual_confirmation"})
    before = service.store.read(service.source)
    assert service.create(content, op, provenance={"kind": "manual_confirmation"}) == item
    with pytest.raises(MemoryError, match="编号"):
        service.create(content.model_copy(update={"content": "改变返回字段"}), op, provenance={"kind": "manual_confirmation"})
    new = change(service, item, "edit", content=fact_content(title="约定", content="用户确认新约定"))
    assert new["revision"] == 2
    assert service.store.read(service.source, sequence=before["sequence"])["items"][0] == item
    with pytest.raises(MemoryError, match="已被更新"):
        change(service, item, "delete")


def test_candidate_confirmation_rejection_and_atomic_replacement(service):
    """候选确认前不可召回，替代目标变化则整笔回滚；同源拒绝候选不会反复重建。"""
    item = save(service)
    candidate = service.create(fact_content(title="接口兼容决定", content="客户已升级，可以删除旧字段"), str(uuid4()), provenance={"kind": "run_reference", "run_id": "known"}, proposed=True, replacements=[{"id": item["id"], "revision": item["revision"]}])
    assert [value["id"] for value in controller(service).items] == [item["id"]]
    accepted = change(service, candidate, "accept")
    assert accepted["status"] == "active"
    assert [value["id"] for value in controller(service).items] == [accepted["id"]]
    content = fact_content(title="候选", content="还未确定的知识")
    source = {"kind": "run_reference", "run_id": "same"}
    rejected = service.create(content, str(uuid4()), provenance=source, proposed=True)
    rejected = change(service, rejected, "reject")
    assert service.create(content, str(uuid4()), provenance=source, proposed=True)["id"] == rejected["id"]
    stale = service.create(fact_content(title="替代", content="另一替代"), str(uuid4()), provenance=source, proposed=True, replacements=[{"id": accepted["id"], "revision": accepted["revision"]}])
    change(service, accepted, "edit", content=fact_content(title="新约定", content="期间已有用户修改"))
    with pytest.raises(MemoryError, match="原记忆已变化"):
        change(service, stale, "accept")
    assert next(value for value in service.list()["items"] if value["id"] == stale["id"])["status"] == "proposed"


def test_frozen_read_survives_edit_delete_and_unicode_paging(service):
    """活动 Run 保持固定正文；修改删除只影响新 Run，UTF-8 分页无缺失且核验指纹。"""
    item = save(service, "中文知识🙂" * 50)
    frozen = controller(service)
    change(service, item, "delete")
    assert not controller(service).items
    pages = []
    offset = 0
    fingerprint = None
    while True:
        page = frozen.execute("memory_get", MemoryGet(memory_id=item["id"], offset=offset, max_bytes=17, expected_sha256=fingerprint))
        pages.append(page["content"])
        fingerprint = page["sha256"]
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert item["content"] in "".join(pages)
    with pytest.raises(MemoryError, match="指纹"):
        frozen.execute("memory_get", MemoryGet(memory_id=item["id"], expected_sha256="0" * 64))


def test_conditions_and_fact_evidence_recheck(service):
    """事实证据和有效条件逐次核验；文本检索支持中文和路径，不把范围当指纹。"""
    source = service.workspace / "api.py"
    source.write_text("old", encoding="utf-8")
    item = save(service, "接口兼容模块位于 api.py", evidence=["api.py"])
    save(service, "过去适用", expires_at="2000-01-01T00:00:00+00:00")
    save(service, "仅用于特定分支", branch="unavailable-branch")
    frozen = controller(service)
    assert len(frozen.selected) == 1 and len(frozen.skipped) == 2
    for query in ("接口", "修改接口兼容", "api.py"):
        assert frozen.execute("memory_search", MemorySearch(query=query))["total"] == 1
    source.write_text("new", encoding="utf-8")
    frozen.select("execution")
    assert not frozen.selected
    with pytest.raises(MemoryError, match="需复核"):
        frozen.execute("memory_get", MemoryGet(memory_id=item["id"]))
    assert "需复核" in next(value for value in service.list()["items"] if value["id"] == item["id"])["unavailable_reason"]


@pytest.mark.parametrize("options", [{"content": " "}, {"content": "中" * 3000}, {"content": "api_key=secret-value-for-tests"}, {"expires_at": "2026-12-01"}, {"tags": ["x" * 65]}])
def test_invalid_or_sensitive_content_rejected(options):
    """正文大小、凭据格式、无时区日期和标签边界失败时，不产生可持久化模型。"""
    with pytest.raises(ValidationError):
        fact_content(**{"title": "知识", "content": "正常正文", **options})


def test_storage_corruption_and_replaced_scope_are_explicit(service):
    """损坏库不会当作空记忆，目录对象替换后不能自动继承旧知识。"""
    save(service)
    moved = service.source.with_name("old-project")
    service.source.rename(moved)
    service.source.mkdir()
    with pytest.raises(MemoryError, match="替换"):
        service.list()
    service.store.path.write_bytes(b"not-sqlite")
    with pytest.raises(MemoryError) as error:
        service.list()
    assert error.value.code == "memory_storage_error"


def test_use_and_capture_independent_and_entry_budget(service):
    """关闭读取仍可独立贡献候选；自动条数和字节上限不改变完整可搜索集合。"""
    save(service)
    disabled = controller(service, policy=MemoryPolicy(use=False, capture=True))
    assert not disabled.items and not disabled.input_text()
    limited = controller(service, policy=MemoryPolicy(max_entries=0))
    assert not limited.selected and len(limited.items) == 1
    smallest = controller(service, policy=MemoryPolicy(max_input_bytes=512))
    assert len(encode(smallest.input_text()).encode("utf-8")) <= 512


def test_same_request_receipt_survives_source_file_change(service):
    """提交后的响应丢失重试仍返回原回执，不因关联文件变化重新执行或丢失幂等性。"""
    path = service.workspace / "source.txt"
    path.write_text("原文件", encoding="utf-8")
    content = fact_content(title="来源约定", content="确认的规则", evidence=["source.txt"])
    operation = str(uuid4())
    first = service.create(content, operation, provenance={"kind": "manual_confirmation"})
    path.unlink()
    assert service.create(content, operation, provenance={"kind": "manual_confirmation"}) == first
