"""公开入口必须给出相符的实际/期望观察，并绑定确切的补丁。"""

from pathlib import Path
import asyncio
import json
from uuid import uuid4

import pytest

from agent_harness.evaluation.reproduction import RepositoryDelivery, coverage_review, entry_requirement, matching_entries, observation, patch_paths
from agent_harness.web.evaluations import attach_entry_reviews


@pytest.mark.parametrize(("code", "output", "status"), [
    (0, "", "error"), (1, "ImportError: missing extension", "error"),
    (0, '{"page":{"actual":"escaped","expected":"中国"}}', "error"),
    (1, '{"page":{"actual":"escaped","expected":"中国"}}', "failed"),
    (0, '{"page":{"actual":"中国","expected":"中国"}}', "passed"),
    (0, '{}', "error"), (0, '{"page":{}}', "error"),
])
def test_observed_business_result(code, output, status):
    """校验退出码 code 与真实业务输出 output 一致时才返回预期 status。"""
    result = observation({"exit_code": code}, "HARNESS_OBSERVATIONS " + output)
    assert result["status"] == status


def test_changed_patch_and_new_file(tmp_path: Path):
    """确认新增文件可收集，tmp_path 内试次的已验证补丁在内容变化时失效。"""
    patch = "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n"
    assert patch_paths(patch) == ["new.txt"]
    delivery = RepositoryDelivery(None, tmp_path, tmp_path / "proof")
    delivery.fact = {"status": "verified", "patch_sha256": "0" * 64}
    assert delivery.final(patch)["status"] == "candidate_changed"


def test_component_success_cannot_prove_admin_request():
    """检查页面和 CLI 操作按原问题选边界，同名组件输出、无路由请求和无关命令均无法作证。"""
    assert entry_requirement("Admin doesn't display dates correctly", "django/django") == ("admin", "")
    assert entry_requirement("Improve CLI command\n$ sphinx-build -b html", "sphinx-doc/sphinx") == ("cli", "sphinx-build")
    assert entry_requirement("Serializer renders dates", "django/django") == ("self", "")
    assert matching_entries("GET /admin/ 200", "admin", "") == []
    web = {"path": "/staff/orders/", "route": "staff:orders", "method": "GET", "admin": True, "status": 200}
    mixed = "HARNESS_ENTRY_TRACE " + json.dumps([{**web, "admin": False}, web, {"command": ["pytest"]}])
    assert matching_entries(mixed, "admin", "") == [web]
    assert matching_entries(mixed, "cli", "sphinx-build") == []


def test_helper_cannot_authorize_page_delivery(tmp_path):
    """tmp_path 内自选组件即使红绿均正确，也需先补齐原页面证据才能确认交付。"""
    delivery = RepositoryDelivery(None, tmp_path, tmp_path)
    delivery.script = b"fixture"
    delivery.fact = {"baseline": {"status": "failed"}, "next_action": "请请求实际页面"}
    with pytest.raises(ValueError, match="实际页面"):
        asyncio.run(delivery.candidate(["example.py"], 60))


def test_page_review_uses_observation_count():
    """页面的单状态成功要复核其他适用路径，已实测两种显示状态时无额外提醒。"""
    fact = {"status": "passed", "observations": {"textarea": {"actual": "汉字", "expected": "汉字"}}}
    assert "textarea" in coverage_review("admin", fact)
    assert coverage_review("self", fact) is None
    fact["observations"]["readonly"] = {"actual": "汉字", "expected": "汉字"}
    assert coverage_review("admin", fact) is None


def test_api_and_cli_observer_run_real_operations(tmp_path, capsys, monkeypatch):
    """用 tmp_path 的隔离脚本执行公开方法和子进程，确认回执来自实际调用。"""
    import sys
    from agent_harness.evaluation.entry_observer import observe

    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(sys, "argv", sys.argv.copy())
    script = tmp_path / "probe.py"
    kind, target = entry_requirement("Widget.render() yields the wrong color", "demo/widgets")
    assert (kind, target) == ("api", "Widget.render")
    # 模拟镜像 checkout 的代码文件名；测试不依赖本机存在 /testbed。
    code = "class Widget:\n def render(self):\n  return 'result'\nWidget().render()"
    script.write_text(f"exec(compile({code!r}, '/testbed/widget.py', 'exec'))", encoding="utf-8")
    observe(str(script), kind, target)
    assert matching_entries(capsys.readouterr().out, kind, target)[0]["symbol"] == target
    script.write_text("import subprocess,sys\nsubprocess.check_call([sys.executable,'-m','this'],stdout=subprocess.DEVNULL)",
                      encoding="utf-8")
    observe(str(script), "cli", "this")
    assert matching_entries(capsys.readouterr().out, "cli", "this")


def test_host_review_is_bound_to_submitted_patch(tmp_path: Path):
    """在 tmp_path 模拟冻结报告；自选用例通过和宿主复核失败各自展示，拒绝套用其他补丁的结果。"""
    run_id = str(uuid4())
    trial = {"run_id": run_id, "patch_sha256": "a" * 64, "entry": {"status": "verified"}}
    report = {"protocol_sha256": "b" * 64, "trials": [trial]}
    folder = tmp_path / "entry-reviews"
    folder.mkdir()
    path = folder / (run_id + ".json")
    receipt = {"run_id": run_id, "protocol_sha256": report["protocol_sha256"], "patch_sha256": trial["patch_sha256"],
               "status": "failed", "output": "GET /admin/ 200; readonly value escaped"}
    path.write_text(json.dumps(receipt), encoding="utf-8")
    attach_entry_reviews(tmp_path, report)
    assert trial["entry"]["status"] == "verified"
    assert trial["host_entry"] == {"status": "failed", "output": receipt["output"]}
    path.write_text(json.dumps({**receipt, "patch_sha256": "c" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="送评补丁"):
        attach_entry_reviews(tmp_path, report)
