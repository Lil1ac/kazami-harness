"""验证计划 Markdown 是可信运行记录的派生附件，网页和桌面下载共用一条边界。"""

from tests.web.job_helpers import submit_run, submit_followup

import asyncio
from pathlib import Path

import httpx
import pytest

from agent_harness.work.exports import materialize_plan_export, render_plan_markdown
from agent_harness.storage.runs import StorageError
from tests.work.test_plan_context import plan_state
from tests.web.test_planning_api import PLAN, ScriptModel, application, call, decision, finished, planning_config, reply, saved_files


def test_plan_exports_preserve_historical_bytes_and_do_not_execute_or_authorize(planning_config) -> None:
    """待批准和旧版可导出；跨轮修改不改旧附件，刷新下载不增加模型调用或修改运行证据。"""
    config = planning_config
    second_plan = {**PLAN, "title": "第二版交付方案", "summary": "补充异常边界，仍须用户批准。"}
    model = ScriptModel([reply(call("submit_plan", **PLAN)), reply(call("submit_plan", **second_plan))])
    app = application(config, model)

    async def scenario() -> None:
        """通过真实 HTTP 请求生成两版计划和附件；返回 None，所有断言依据文件与后端状态。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "先规划"}))
                original = saved_files(config, first["run_id"])
                url = "/api/runs/" + first["run_id"] + "/artifacts"
                parameters = {"location": "run", "path": "plans/v1.md"}
                document = await client.get(url, params=parameters)
                assert document.status_code == 200 and "attachment" in document.headers["content-disposition"]
                assert "plan-v1.md" in document.headers["content-disposition"]
                assert "# 交付结果文件" in document.text and "待批准" in document.text
                assert "## 执行步骤" in document.text and "## 验收方式" in document.text
                assert (await client.get(url, params=parameters)).content == document.content
                assert saved_files(config, first["run_id"]) == original and len(model.requests) == 1
                second = await finished(client, await submit_followup(client, "/api/runs/" + first["run_id"] + "/followups", json=decision(first, "revise")))
                current_url = "/api/runs/" + second["run_id"] + "/artifacts"
                current = await client.get(current_url, params={"location": "run", "path": "plans/v2.md"})
                assert current.status_code == 200 and "第二版交付方案" in current.text
                assert (await client.get(url, params=parameters)).content == document.content
                assert "已被替代" in (await client.get(current_url, params=parameters)).text
                for invalid in ("plans/v3.md", "plans/v0.md", "plans/../run.json", "plans/v1.md/../../run.json"):
                    assert (await client.get(current_url, params={"location": "run", "path": invalid})).status_code == 404
                assert len(model.requests) == 2 and saved_files(config, first["run_id"]) == original
                assert sorted(item.name for item in config.workspaces[0].path.iterdir()) == ["source.txt"]
                assert (await client.get("/api/runs/" + second["run_id"])).json()["result"]["planning"]["pending"]["kind"] == "plan_approval"
    asyncio.run(scenario())


def test_plan_export_cache_preserves_content_and_rejects_corruption(tmp_path: Path) -> None:
    """导出缓存按内容寻址，进度更新不覆盖旧快照，缓存被篡改时拒绝而不返回伪造文本。"""
    plan = plan_state(tmp_path).plans[0]
    directory = tmp_path / "run"
    directory.mkdir()
    first = materialize_plan_export(directory, plan)
    original = first.read_bytes()
    assert first == materialize_plan_export(directory, plan)
    plan.steps[0].status = "completed"
    second = materialize_plan_export(directory, plan)
    assert second != first and first.read_bytes() == original
    assert "1. [x]" in second.read_text(encoding="utf-8")
    second.write_text("伪造导出", encoding="utf-8")
    with pytest.raises(StorageError, match="导出不可用"):
        materialize_plan_export(directory, plan)


def test_markdown_preserves_summary_and_escapes_plain_fields(tmp_path: Path) -> None:
    """Markdown 正文保持原格式，普通字段按字面输出，步骤完成不会改变批准或验收事实。"""
    plan = plan_state(tmp_path).plans[0]
    plan.title = "计划 <script> & [链接]"
    plan.summary = "### 架构\n\n保留 **完整正文**。"
    plan.steps[0].status = "completed"
    text = render_plan_markdown(plan)
    assert plan.summary in text and "&lt;script&gt;" in text and "\\[链接\\]" in text
    assert "批准状态：已批准" in text and "实际验收结论见对应运行结果" in text
