"""用门闩而非耗时猜测验证真实并行、共享预算、HTTP归属与父生命周期。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runtime.runner import REQUEST_CONTEXT, run_task
from agent_harness.schemas import RunConfig, RunLimits, RunManifest, TaskSpec
from tests.subagents.test_subagents import report


def response(text=None, calls=()):
    """以text或calls构造带真实配对编号的模型响应，返回ModelResponse；不产生网络请求。"""
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
    return ModelResponse(message, list(calls), "tool_calls" if calls else "stop", "offline", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


class ParallelModel:
    """两个子请求必须同时进入才打开门闩；父按实际回执等待并交付。"""

    def __init__(self, blocked=False):
        """blocked为True时子项等外部取消，否则两项到齐后放行；构造不启动协程。"""
        self.blocked = blocked  # 取消用例不自动放行。
        self.entered = 0  # 已进入首次模型请求的独立子项数。
        self.both = asyncio.Event()  # 两个请求确实重叠后通知测试。
        self.gate = asyncio.Event()  # 子项等待点；不使用时间差推断并发。
        self.parent_step = 0  # 父脚本进度。

    async def complete(self, messages, tools):
        """从messages识别父子，tools沿用宿主声明；返回脚本响应，HTTP回调经过实际任务归属路由。"""
        context = REQUEST_CONTEXT.get()
        root = context.budget_parent or context
        root.http_attempt(1)
        if context.budget_parent is not None:
            observed = [item for item in messages if item["role"] == "tool"]
            if not observed:
                self.entered += 1
                if self.entered == 2:
                    self.both.set()
                    if not self.blocked:
                        self.gate.set()
                await self.gate.wait()
                return response(calls=[ToolCall(str(context.manifest.run_id), "read_file", '{"path":"api.py"}')])
            reference = json.loads(observed[-1]["content"])["evidence_ref"]["tool_call_id"]
            return response(report(ids=[reference]))
        self.parent_step += 1
        if self.parent_step == 1:
            return response(calls=[ToolCall(str(index), "delegate_readonly", json.dumps({"title": f"研究{index}", "objective": "读取api.py", "background": True, "max_model_turns": 3, "max_tool_calls": 2})) for index in range(2)])
        if self.parent_step == 2:
            await self.both.wait()
            await asyncio.sleep(0)  # 让子模型在父请求尚未返回时推进总计数，核对请求/响应轮号配对。
            return response(calls=[ToolCall("wait", "wait_subagents", '{"timeout_seconds":60}')])
        if self.parent_step == 3:
            return response(calls=[ToolCall("write", "write_file", '{"path":"result.txt","content":"42"}')])
        return response("已核对两份报告并交付。")


def manifest(tmp_path: Path):
    """在tmp_path生成独立代码，返回限制12模型/20工具的父清单；不访问用户项目。"""
    (tmp_path / "api.py").write_text("value = 42", encoding="utf-8")
    return RunManifest(run_id=uuid4(), task=TaskSpec(prompt="并行研究后交付", workspace=tmp_path), config=RunConfig(tools=["read_file", "write_file"], limits=RunLimits(max_model_turns=12, max_tool_calls=20)))


def test_two_children_really_overlap_and_account_once(tmp_path):
    """子请求必须同时进入才可完成，父使用报告写文件，模型/HTTP/token按归属只计一次。"""
    async def scenario():
        """运行真实父子核心并返回结果；超时意味着两个请求没有重叠。"""
        model = ParallelModel()
        outcome = await asyncio.wait_for(run_task(manifest(tmp_path), tmp_path / "runs", "offline", model_client=model), 10)
        assert model.both.is_set()
        return outcome.result
    result = asyncio.run(scenario())
    assert result.execution_status == "completed" and (tmp_path / "result.txt").read_text() == "42"
    assert len(result.subagents) == 2 and all(item.status == "completed" for item in result.subagents)
    assert result.model_turns == result.http_attempts == 8 and result.usage.total_tokens == 16
    assert all(item.model_turns == item.http_attempts == 2 and item.usage["total_tokens"] == 4 for item in result.subagents)
    from agent_harness.storage.runs import RunStore
    events = RunStore.read(tmp_path / "runs", str(result.run_id)).events
    started = [item.payload["model_turn"] for item in events if item.type == "model_request_started"]
    returned = [item.payload["model_turn"] for item in events if item.type == "model_response"]
    assert started == returned


def test_parent_cancel_drains_both_children(tmp_path):
    """父停止时两个门闩内的研究都封存取消报告，结果返回后不会遗留后台协程。"""
    async def scenario():
        """确认两子进入请求后取消父，返回已收拢的父结果。"""
        model = ParallelModel(blocked=True)
        running = asyncio.create_task(run_task(manifest(tmp_path), tmp_path / "runs", "offline", model_client=model))
        await asyncio.wait_for(model.both.wait(), 5)
        running.cancel()
        return (await asyncio.wait_for(running, 5)).result
    result = asyncio.run(scenario())
    assert result.execution_status == "cancelled"
    assert len(result.subagents) == 2
    assert all(item.status == "cancelled" and item.report.partial for item in result.subagents)


def test_cancel_one_via_http_keeps_sibling_and_history(tmp_path, monkeypatch):
    """真实HTTP单项取消只影响直属目标，兄弟继续交付；重复及外来编号语义明确。"""
    import httpx
    from agent_harness.web.api import create_app
    from tests.web.test_subagents_api import app_config, wait_finished

    async def scenario():
        """启动真实队列并等两子进入门闩，通过接口取消一项后释放另一项，返回None。"""
        config = app_config(tmp_path, monkeypatch)
        model = ParallelModel(blocked=True)
        app = create_app(config, RunConfig(tools=["read_file", "write_file"]), tmp_path / "records", model_client=model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            accepted = await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": "sample", "prompt": "并行检查"}})
            run_id = accepted.json()["run_id"]
            await asyncio.wait_for(model.both.wait(), 5)
            children = (await client.get(f"/api/runs/{run_id}/subagents")).json()["items"]
            first, second = [item["run_id"] for item in children]
            foreign = await client.post(f"/api/runs/{run_id}/subagents/{uuid4()}/cancel")
            assert foreign.status_code == 404
            cancelled = await client.post(f"/api/runs/{run_id}/subagents/{first}/cancel")
            assert cancelled.status_code == 200 and cancelled.json()["subagent"]["status"] == "cancelled"
            assert cancelled.json()["subagent"]["stop_reason"] == "user_cancelled"
            assert (await client.get(f"/api/runs/{run_id}/subagents/{second}")).json()["subagent"]["status"] == "running"
            repeated = await client.post(f"/api/runs/{run_id}/subagents/{first}/cancel")
            assert repeated.json() == cancelled.json()
            model.gate.set()
            view = await wait_finished(client, run_id)
            assert view["result"]["execution_status"] == "completed"
            final = (await client.get(f"/api/runs/{run_id}/subagents")).json()["items"]
            assert [item["status"] for item in final] == ["cancelled", "completed"]
            assert all(item["report"] is not None for item in final)
    asyncio.run(scenario())


class ControlModel(ParallelModel):
    """双子等待时尝试第三委派和父写入，再查询、单项取消并收回另一报告。"""

    async def complete(self, messages, tools):
        """沿用messages/tools的实际父子路由，父控制脚本返回操作并核对回执，不访问网络。"""
        context = REQUEST_CONTEXT.get()
        if context.budget_parent is not None:
            return await super().complete(messages, tools)
        context.http_attempt(1)
        self.parent_step += 1
        step = self.parent_step
        receipts = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        if step == 1:
            return response(calls=[ToolCall(str(index), "delegate_readonly", json.dumps({"title": str(index), "objective": "读取", "background": True, "max_model_turns": 3, "max_tool_calls": 2})) for index in range(2)])
        if step == 2:
            await self.both.wait()
            return response(calls=[ToolCall("third", "delegate_readonly", '{"title":"第三项","objective":"读取","background":true}')])
        if step == 3:
            assert receipts[-1]["error"]["code"] == "subagent_concurrency_limit"
            return response(calls=[ToolCall("blocked-write", "write_file", '{"path":"wrong.txt","content":"bad"}')])
        if step == 4:
            assert receipts[-1]["error"]["code"] == "subagent_readers_active"
            return response(calls=[ToolCall("poll", "wait_subagents", '{"timeout_seconds":0}')])
        if step == 5:
            assert not receipts[-1]["data"]["finished"]
            first = receipts[-1]["data"]["items"][0]["run_id"]
            return response(calls=[ToolCall("cancel-one", "cancel_subagent", json.dumps({"run_id": first}))])
        if step == 6:
            assert receipts[-1]["data"]["subagent"]["status"] == "cancelled"
            self.gate.set()
            return response(calls=[ToolCall("finish", "wait_subagents", '{}')])
        assert [item["status"] for item in receipts[-1]["data"]["items"]] == ["cancelled", "completed"]
        return response("已核对取消及完整报告。")


def test_concurrency_poll_and_shared_directory_write_barrier(tmp_path):
    """第三项被限流、查询不取消研究、共享目录拒绝父写入，单项取消后兄弟仍完成。"""
    outcome = asyncio.run(run_task(manifest(tmp_path), tmp_path / "runs", "offline", model_client=ControlModel(blocked=True)))
    assert outcome.result.execution_status == "completed"
    assert len(outcome.result.subagents) == 2 and not (tmp_path / "wrong.txt").exists()
    assert outcome.result.model_turns <= 12 and outcome.result.tool_calls <= 20


def test_budget_is_reserved_before_second_child_acceptance(tmp_path):
    """未消费的分配不能重复授予第二子，父预留保留在原上限内，收尾释放余额。"""
    async def scenario():
        """缩小预算并驱动两项；第二子只能获1轮，父仍可收回部分报告，返回执行事实。"""
        value = manifest(tmp_path)
        value.config.limits.max_model_turns = 7
        result = await asyncio.wait_for(run_task(value, tmp_path / "runs", "offline", model_client=ParallelModel()), 10)
        return result.result
    result = asyncio.run(scenario())
    assert result.model_turns <= 7
    assert [item.max_model_turns for item in result.subagents] == [3, 1]
    assert result.subagents[1].report.partial
