"""五类记忆的三十个行为场景；直接检查可见输入、文件变化和事务结果。"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.memory import MemoryChange, MemoryContent, MemoryController, MemoryError, MemoryGet, MemoryService, MemoryStore
from agent_harness.schemas import MemoryPolicy, RunConfig, RunManifest, TaskSpec


DETAILS = {
    "rule": {"topic": "金额格式", "behavior": "金额写成两位小数字符串", "reason": "下游契约", "exceptions": ""},
    "decision": {"topic": "金额格式", "choice": "金额写成两位小数字符串", "rationale": "下游契约", "revisit_when": "客户升级后重评"},
    "fact": {"subject": "付款服务", "attribute": "金额单位", "value": "元", "observed_at": None},
    "reference": {"target": "guide.txt", "purpose": "金额接口说明", "consult_when": "修改付款服务时"},
    "experience": {"situation": "金额接口联调", "action": "序列化为字符串", "outcome": "当时客户端解析成功", "limitations": "仅验证过该客户端版本"},
}  # 场景明确的业务主值，不依赖模型推测。


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    """创建含证据和参考文档的独立项目，返回当前格式的服务；不联网。"""
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "guide.txt").write_text("v1", encoding="utf-8")
    return MemoryService(MemoryStore(tmp_path / "runs"), workspace)


def content(kind: str, **options) -> MemoryContent:
    """为 kind 构造明确知识；options 覆盖条件或专属主值，返回验证模型。"""
    return MemoryContent(**{"title": "金额接口", "type": kind, "details": DETAILS[kind], **options})


def save(service: MemoryService, kind: str, **options) -> dict:
    """人工保存 kind 知识及 options；返回持久修订，冲突原样抛出。"""
    return service.create(content(kind, **options), str(uuid4()), provenance={"kind": "manual_confirmation"})


def controller(service: MemoryService, prompt: str = "修改金额接口", **policy) -> MemoryController:
    """创建固定版本读取集；prompt 为任务文字，policy 是宿主预算覆盖，返回控制器。"""
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(workspace=service.workspace, prompt=prompt), config=RunConfig(memory=MemoryPolicy(**policy)))

    def emit(kind, payload):
        """忽略领域测试的 kind/payload 事件；真实事件由 HTTP 测试验证，返回 None。"""

    return MemoryController(service.store.root, manifest, emit)


@pytest.mark.parametrize("scenario", ["project", "path", "zero_depth", "wrong_path", "phase", "command"])
def test_rule_scenarios(service, scenario):
    """用 service 验证 scenario 指定的六种约定情境；检查真实选择或变更前错误，返回 None。"""
    scope = {} if scenario == "project" else {"paths": ["src/**/*.py"]}
    if scenario == "phase":
        scope = {"phases": ["planning"]}
    save(service, "rule", applies_to=scope, origin="user_correction")
    frozen = controller(service, "完成用户要求")
    if scenario == "project":
        assert frozen.selected[0]["type"] == "rule" and frozen.selected[0]["origin"] == "user_correction"
    elif scenario == "phase":
        assert frozen.selected == []
        frozen.select("planning")
        assert frozen.selected
    elif scenario == "wrong_path":
        frozen.before_tool("write_file", SimpleNamespace(path="docs/a.md"), "execution")
        assert not frozen.selected
    else:
        name = "run_command" if scenario == "command" else "write_file"
        args = SimpleNamespace(argv=["python", "script.py"], cwd=".") if name == "run_command" else SimpleNamespace(path="src/a.py" if scenario == "zero_depth" else "src/api/a.py")
        with pytest.raises(MemoryError) as error:
            frozen.before_tool(name, args, "execution")
        assert error.value.code == "memory_context_updated" and frozen.selected
        frozen.record_use(2)
        frozen.before_tool(name, args, "execution")


@pytest.mark.parametrize("scenario", ["rationale", "changed_file", "conflict", "disjoint", "required_reason", "stale"])
def test_decision_scenarios(service, scenario):
    """用 service 验证 scenario 指定的决策理由、冲突、互斥范围和原子替代；返回 None。"""
    if scenario == "required_reason":
        with pytest.raises(ValidationError):
            content("decision", details={"topic": "金额", "choice": "字符串"})
        return
    item = save(service, "decision", evidence=["guide.txt"], applies_to={"paths": ["src/**"]} if scenario == "disjoint" else {})
    if scenario in {"rationale", "changed_file"}:
        if scenario == "changed_file":
            (service.workspace / "guide.txt").write_text("v2", encoding="utf-8")
        assert controller(service).selected[0]["details"]["rationale"] == "下游契约"
        return
    new = content("decision", details={**DETAILS["decision"], "choice": "数字"}, applies_to={"paths": ["docs/**"]} if scenario == "disjoint" else {})
    if scenario == "conflict":
        with pytest.raises(MemoryError) as error:
            service.create(new, str(uuid4()), provenance={"kind": "manual_confirmation"})
        assert error.value.details["conflicts"][0]["id"] == item["id"]
    elif scenario == "disjoint":
        service.create(new, str(uuid4()), provenance={"kind": "manual_confirmation"})
        assert len(service.list(status="active")["items"]) == 2
    else:
        proposal = service.create(new, str(uuid4()), provenance={"kind": "run_reference"}, proposed=True, replacements=[{"id": item["id"], "revision": 1}])
        service.update(item["id"], MemoryChange(request_id=uuid4(), expected_revision=1, action="edit", content=content("decision", content="更新理由补充")))
        with pytest.raises(MemoryError, match="原记忆已变化"):
            service.update(proposal["id"], MemoryChange(request_id=uuid4(), expected_revision=1, action="accept"))
        assert service.list(status="proposed")["items"][0]["revision"] == 1


@pytest.mark.parametrize("scenario", ["unchanged", "changed", "removed", "time", "conflict", "expired"])
def test_fact_scenarios(service, scenario):
    """用 service 验证 scenario 指定的证据稳定/变化/缺失、观察时间、冲突或到期；返回 None。"""
    if scenario == "time":
        with pytest.raises(ValidationError):
            content("fact", details={**DETAILS["fact"], "observed_at": "2026-01-01"})
        return
    item = save(service, "fact", evidence=["guide.txt"], expires_at="2000-01-01T00:00:00Z" if scenario == "expired" else None)
    frozen = controller(service)
    if scenario == "changed":
        (service.workspace / "guide.txt").write_text("v2", encoding="utf-8")
    elif scenario == "removed":
        (service.workspace / "guide.txt").unlink()
    elif scenario == "conflict":
        with pytest.raises(MemoryError) as error:
            save(service, "fact", details={**DETAILS["fact"], "value": "分"})
        assert error.value.code == "memory_knowledge_conflict"
        return
    frozen.select("execution")
    if scenario == "unchanged":
        assert frozen.selected[0]["details"]["value"] == "元"
    else:
        assert frozen.selected == []
        with pytest.raises(MemoryError):
            frozen.execute("memory_get", MemoryGet(memory_id=item["id"]))


@pytest.mark.parametrize("scenario", ["index", "changed", "removed", "url", "invalid_url", "explicit_read"])
def test_reference_scenarios(service, scenario):
    """用 service 验证 scenario 指定的短入口、目标变化、URL校验或明确回读；返回 None。"""
    if scenario == "invalid_url":
        with pytest.raises(ValidationError):
            content("reference", details={"target": "https://user:pass@example.com", "purpose": "说明"})
        return
    details = {**DETAILS["reference"], "target": "https://example.invalid/api"} if scenario == "url" else DETAILS["reference"]
    item = save(service, "reference", details=details, content="SUPPLEMENT_ONLY_" + "补充" * 500)
    if scenario == "changed":
        (service.workspace / "guide.txt").write_text("v2", encoding="utf-8")
    elif scenario == "removed":
        (service.workspace / "guide.txt").unlink()
    frozen = controller(service)
    if scenario == "removed":
        assert not frozen.selected and "入口" in frozen.skipped[0]["reason"]
    else:
        assert frozen.selected[0]["details"]["target"] == details["target"]
        assert "SUPPLEMENT_ONLY_" not in frozen.input_text()
        if scenario == "explicit_read":
            result = frozen.execute("memory_get", MemoryGet(memory_id=item["id"], max_bytes=16384))
            assert "SUPPLEMENT_ONLY_" in result["content"]


@pytest.mark.parametrize("scenario", ["success", "failure", "platform", "phase", "missing_result", "changed_file"])
def test_experience_scenarios(service, scenario):
    """用 service 验证 scenario 指定的案例结果、环境/阶段限制和历史证据；返回 None。"""
    if scenario == "missing_result":
        details = {key: value for key, value in DETAILS["experience"].items() if key != "outcome"}
        with pytest.raises(ValidationError):
            content("experience", details=details)
        return
    details = {**DETAILS["experience"], "outcome": "当时失败，不能照搬"} if scenario == "failure" else DETAILS["experience"]
    scope = {"platforms": ["linux"]} if scenario == "platform" else {"phases": ["planning"]} if scenario == "phase" else {}
    if scenario == "platform":
        import sys
        scope = {"platforms": ["win32" if sys.platform != "win32" else "linux"]}
    save(service, "experience", details=details, applies_to=scope, evidence=["guide.txt"])
    if scenario == "changed_file":
        (service.workspace / "guide.txt").write_text("v2", encoding="utf-8")
    frozen = controller(service)
    if scenario in {"platform", "phase"}:
        assert frozen.selected == []
    else:
        selected = frozen.selected[0]
        assert selected["details"]["outcome"] == details["outcome"]
        assert "不代表当前验收" in selected["presentation"]


def test_explicit_complete_rule_read_allows_search_only_policy(service):
    """service 中约定不能自动注入时，完整分页确实进入下一模型输入后才放行；返回 None。"""
    item = save(service, "rule")
    frozen = controller(service, max_entries=0)
    args = SimpleNamespace(path="result.json")
    with pytest.raises(MemoryError):
        frozen.before_tool("write_file", args, "execution")
    page = frozen.execute("memory_get", MemoryGet(memory_id=item["id"], max_bytes=16384))
    with pytest.raises(MemoryError):
        frozen.before_tool("write_file", args, "execution")
    frozen.record_use(2, [{"role": "tool", "content": json.dumps({"ok": True, "data": page})}])
    frozen.before_tool("write_file", args, "execution")
    assert frozen.uses[-1]["exposed_rule_ids"] == [item["id"]]


def test_partial_rule_page_is_not_complete_exposure(service):
    """service 中只读到尾页不能冒充整条已看过，原变更仍被阻止；返回 None。"""
    item = save(service, "rule")
    frozen = controller(service, max_entries=0)
    page = frozen.execute("memory_get", MemoryGet(memory_id=item["id"], offset=4, max_bytes=16384))
    assert page["eof"]
    frozen.record_use(2, [{"role": "tool", "content": json.dumps({"ok": True, "data": page})}])
    with pytest.raises(MemoryError):
        frozen.before_tool("write_file", SimpleNamespace(path="result.json"), "execution")


def test_type_input_uses_clear_usage_text_and_leaves_storage_metadata_out(service):
    """service 中约定以行为说明进入输入，存储摘要与时间不挤占自动正文；返回 None。"""
    save(service, "rule")
    frozen = controller(service)
    text = frozen.input_text()
    assert "适用时遵循此行为约定：金额写成两位小数字符串" in text
    assert "content_sha256" not in text and "created_at" not in text


def test_expired_claim_does_not_block_current_value(service):
    """service 的旧事实已明确到期时，新的当前属性值可保存，旧修订仍保留；返回 None。"""
    save(service, "fact", expires_at="2000-01-01T00:00:00Z")
    save(service, "fact", details={**DETAILS["fact"], "value": "美元"})
    frozen = controller(service)
    assert len(frozen.selected) == 1 and frozen.selected[0]["details"]["value"] == "美元"
