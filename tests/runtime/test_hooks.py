"""真实脚本、工具、审批和进程上的 Hooks 端到端回归，不访问模型网络。"""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from agent_harness.hooks.catalog import HookStore, discover, review
from agent_harness.hooks.models import HookPolicy, HookRule, HookReview
from agent_harness.permissions.models import PermissionPolicy
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.work.tasks import TaskError
from tests.runtime.test_runner import call, response, run_script


def configured(tmp_path: Path, event: str, script: str, *, trust: bool = True, **rule_options):
    """在 tmp_path 创建事件脚本和清单，返回清单/记录根；trust=False 保留待审核，其他项传给规则。"""
    from uuid import uuid4
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = workspace / "hook.py"
    entry.write_text(script, encoding="utf-8")
    root = tmp_path / "records"
    rule = HookRule(id="check", event=event, argv=[sys.executable, str(entry)], **rule_options)
    policy = HookPolicy(rules=[rule])
    config = RunConfig(tools=["read_file", "write_file", "edit_file", "run_command"], permission_policy=PermissionPolicy(command="allow", verification="allow"), hooks=policy)
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="完成任务", workspace=workspace), config=config)
    if trust:
        store = HookStore(root)
        item = discover(workspace, policy, store)[0]
        review(workspace, policy, store, HookReview(key=item["key"], fingerprint=item["fingerprint"], action="trust"))
    return manifest, root


@pytest.mark.parametrize("path", ["result.txt", "./result.txt", "nested/../result.txt", pytest.param("RESULT.TXT", marks=pytest.mark.skipif(os.name != "nt", reason="Windows文件名大小写等价"))])
def test_pre_tool_denies_without_write(tmp_path, path):
    """PreToolUse 拒绝写入时，目标不存在且没有 tool_started；模型能收到可读原因。"""
    value, root = configured(tmp_path, "PreToolUse", 'import json\nprint(json.dumps({"decision":"deny","reason":"禁止生成该文件"}))', tools=["write_file"], paths=["result.txt"])
    outcome, requests, history = run_script(value, root, [response(call("one", "write_file", json.dumps({"path": path, "content": "x"}))), response(content="已停止")])
    assert not (value.task.workspace / "result.txt").exists()
    assert not any(event.type == "tool_started" for event in history.events)
    assert json.loads(requests[1]["messages"][-1]["content"])["error"]["code"] == "hook_denied"
    assert outcome.result.hooks["calls"] == 1


def test_post_tool_formats_final_artifact(tmp_path):
    """写入后真实脚本修改目标，最终差异和模型回执保留原动作与后处理事实。"""
    value, root = configured(tmp_path, "PostToolUse", 'import json,pathlib,sys\nx=json.load(sys.stdin)\np=pathlib.Path(x["tool_input"]["path"])\np.write_text(p.read_text().upper())\nprint("{}")', tools=["write_file"])
    outcome, _, history = run_script(value, root, [response(call("one", "write_file", '{"path":"result.txt","content":"hello"}')), response(content="完成")])
    assert (value.task.workspace / "result.txt").read_text() == "HELLO"
    assert outcome.result.execution_status == "completed"
    assert any(event.type == "hook_tool_receipt" for event in history.events)
    assert outcome.result.hooks["records"][0]["status"] == "completed"


@pytest.mark.parametrize("required", [False, True])
def test_untrusted_hook_does_not_execute(tmp_path, required):
    """未审核脚本不产生 marker；required 门禁拒绝动作，普通规则保留跳过原因。"""
    value, root = configured(tmp_path, "PreToolUse", 'import pathlib\npathlib.Path("marker").touch()\nprint("{}")', trust=False, required=required)
    run_script(value, root, [response(call("one", "write_file", '{"path":"result.txt","content":"x"}')), response(content="完成")])
    assert not (value.task.workspace / "marker").exists()
    assert (value.task.workspace / "result.txt").exists() is not required


def test_changed_script_requires_new_review(tmp_path):
    """修改已审核入口会改变指纹，旧审核请求被拒绝，新定义处于待审核。"""
    value, root = configured(tmp_path, "RunStart", 'print("{}")')
    store = HookStore(root)
    old = discover(value.task.workspace, value.config.hooks, store)[0]
    (value.task.workspace / "hook.py").write_text('print("{ } ")', encoding="utf-8")
    current = discover(value.task.workspace, value.config.hooks, store)[0]
    assert not current["trusted"] and old["fingerprint"] != current["fingerprint"]
    with pytest.raises(TaskError, match="已变化"):
        review(value.task.workspace, value.config.hooks, store, HookReview(key=old["key"], fingerprint=old["fingerprint"], action="trust"))


def test_stop_can_continue_once(tmp_path):
    """Stop 首次要求续跑，第二次接受；两次模型请求沿用同一预算并保留反馈。"""
    value, root = configured(tmp_path, "Stop", 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"decision":"continue"} if x["stop_hook_active"] else {"decision":"retry","reason":"补充结果"}))')
    outcome, requests, history = run_script(value, root, [response(content="初稿"), response(content="补充后的结果")])
    assert outcome.result.execution_status == "completed"
    assert outcome.result.model_turns == 2 and len(requests) == 2
    assert outcome.result.hooks["stop_retries"] == 1
    assert any(event.type == "hook_stop_retry" for event in history.events)


def test_command_finished_is_not_handle_return(tmp_path):
    """后台命令的实际结果触发一次 Hook；启动句柄与后续等待不重复消费该事实。"""
    value, root = configured(tmp_path, "CommandFinished", 'import json,pathlib,sys\nx=json.load(sys.stdin)\nassert x["command"]["finished_at"]\np=pathlib.Path("marker")\np.write_text(p.read_text()+"1" if p.exists() else "1")\nprint("{}")')
    args = json.dumps({"argv": [sys.executable, "-c", "print('done')"], "background": True})
    outcome, _, _ = run_script(value, root, [response(call("one", "run_command", args)), response(content="候选完成"), response(content="完成")])
    assert outcome.result.execution_status == "completed"
    assert (value.task.workspace / "marker").read_text() == "1"


@pytest.mark.parametrize("on_failure,expected", [("continue", True), ("block", False)])
def test_protocol_failure_policy(tmp_path, on_failure, expected):
    """非法标准输出按明确策略继续或阻断，stderr/异常不冒充成功回执。"""
    value, root = configured(tmp_path, "PreToolUse", 'print("invalid")', on_failure=on_failure, tools=["write_file"])
    run_script(value, root, [response(call("one", "write_file", '{"path":"result.txt","content":"x"}')), response(content="完成"), response(content="完成")])
    assert (value.task.workspace / "result.txt").exists() is expected


def test_verification_observes_hook_modified_final_code(tmp_path):
    """独立验收在格式化之后运行，以实际最终文件判断通过，不使用 Hook 通过代替验收。"""
    from agent_harness.schemas import VerifierSpec
    value, root = configured(tmp_path, "PostToolUse", 'from pathlib import Path\np=Path("result.txt")\np.write_text(p.read_text().upper())\nprint("{}")', tools=["write_file"])
    value.task.verifier = VerifierSpec(argv=[sys.executable, "-c", "from pathlib import Path; assert Path('result.txt').read_text() == 'HELLO'"], cwd=value.task.workspace)
    outcome, _, history = run_script(value, root, [response(call("one", "write_file", '{"path":"result.txt","content":"hello"}')), response(content="完成")])
    assert outcome.result.validation_status == "passed", outcome.result
    events = [event.type for event in history.events]
    assert events.index("hook_tool_receipt") < events.index("verification_started") < events.index("artifacts_collected")


def test_cancel_hook_cleans_process_and_does_not_write_original_target(tmp_path):
    """脚本已启动时取消运行，进程被回收且原文件工具未开始，不把脚本回执冒充原工具结果。"""
    from agent_harness.runtime.runner import run_task
    from tests.web.test_planning_api import ScriptModel, call as model_call, reply
    value, root = configured(tmp_path, "PreToolUse", 'import pathlib,time\npathlib.Path("ready").touch()\ntime.sleep(30)\nprint("{}")', timeout_seconds=30.0, tools=["write_file"])

    async def scenario():
        """以 ready 文件确认真实启动后取消；返回执行结果，不依赖固定睡眠猜测进程阶段。"""
        model = ScriptModel([reply(model_call("write_file", path="result.txt", content="x"))])
        job = asyncio.create_task(run_task(value, root, "offline", model_client=model))
        async with asyncio.timeout(10):
            while not (value.task.workspace / "ready").exists():
                await asyncio.sleep(.01)
        job.cancel()
        return await job

    outcome = asyncio.run(scenario())
    assert outcome.result.execution_status == "cancelled"
    assert outcome.result.cleanup_status == "complete"
    assert outcome.result.hooks["records"][0]["status"] == "cancelled"
    assert not (value.task.workspace / "result.txt").exists()


def test_compaction_hooks_wrap_real_summary(tmp_path):
    """真实摘要前后触发对应 Hook，并保留摘要预算和原压缩投影。"""
    import httpx
    from agent_harness.runtime.runner import run_task
    from tests.context.test_compression import _manifest, CompressionServer
    value = _manifest(tmp_path, max_input_bytes=15000, inline_tool_bytes=20000, keep_recent_groups=1)
    value.config.tools.append("run_command")  # Hook 命令同样要求当前运行启用命令能力。
    script = value.task.workspace / "hook.py"
    script.write_text('import json,pathlib,sys\nx=json.load(sys.stdin)\np=pathlib.Path("compact-events")\np.write_text((p.read_text() if p.exists() else "")+x["event"]+"\\n")\nprint("{}")', encoding="utf-8")
    value.config.hooks = HookPolicy(rules=[HookRule(id=event, event=event, argv=[sys.executable, str(script)]) for event in ("PreCompact", "PostCompact")])
    root = tmp_path / "runs"
    store = HookStore(root)
    for item in discover(value.task.workspace, value.config.hooks, store):
        review(value.task.workspace, value.config.hooks, store, HookReview(key=item["key"], fingerprint=item["fingerprint"], action="trust"))
    server = CompressionServer(observations=3)
    outcome = asyncio.run(run_task(value, root, "offline", transport=httpx.MockTransport(server.handle)))
    assert outcome.result.execution_status == "completed", outcome.result
    events = (value.task.workspace / "compact-events").read_text().splitlines()
    assert events and events == [name for _ in server.summaries for name in ("PreCompact", "PostCompact")]


def test_parent_subagent_hooks_do_not_execute_inside_readonly_children(tmp_path):
    """父宿主收到两个子项起止事件；脚本在共享读取结束后运行，子 Agent 不运行外部 Hook。"""
    from agent_harness.runtime.runner import run_task
    from tests.subagents.test_parallel import manifest, ParallelModel
    value = manifest(tmp_path)
    value.config.tools.append("run_command")
    value.config.permission_policy.command = "allow"
    script = tmp_path / "hook.py"
    script.write_text('import json,pathlib,sys\nx=json.load(sys.stdin)\np=pathlib.Path("agent-events")\np.write_text((p.read_text() if p.exists() else "")+x["event"]+"\\n")\nprint("{}")', encoding="utf-8")
    value.config.hooks = HookPolicy(rules=[HookRule(id=event, event=event, argv=[sys.executable, str(script)]) for event in ("SubagentStart", "SubagentStop")])
    root = tmp_path / "runs"
    store = HookStore(root)
    for item in discover(tmp_path, value.config.hooks, store):
        review(tmp_path, value.config.hooks, store, HookReview(key=item["key"], fingerprint=item["fingerprint"], action="trust"))
    outcome = asyncio.run(run_task(value, root, "offline", model_client=ParallelModel()))
    assert outcome.result.execution_status == "completed", outcome.result
    events = (tmp_path / "agent-events").read_text().splitlines()
    assert events.count("SubagentStart") == events.count("SubagentStop") == 2
    from agent_harness.subagents.service import read_child
    for child in outcome.result.subagents:
        history = read_child(root, value.run_id, child.run_id)
        assert history.result.hooks["calls"] == 0


def test_started_hook_without_receipt_is_not_replayed(tmp_path):
    """持久阶段已有开始而无结束时，拒绝重放脚本和原工具，并明确待核对原因。"""
    from uuid import uuid4
    from agent_harness.storage.sessions import payload_digest
    value, root = configured(tmp_path, "PreToolUse", 'from pathlib import Path\nPath("marker").touch()\nprint("{}")', tools=["write_file"])
    args = '{"path":"result.txt","content":"x"}'
    origin = {"kind": "tool", "logical_run": str(value.run_id), "model_turn": 1, "call_id": "one", "name": "write_file", "arguments_json": args}
    HookStore(root).put("operation:" + payload_digest(origin), {"session_id": str(value.run_id), "events": {"PreToolUse:pre:host:check": {"id": str(uuid4()), "status": "running"}}})
    outcome, _, _ = run_script(value, root, [response(call("one", "write_file", args))])
    assert outcome.result.stop_reason == "hook_result_unknown"
    assert not (value.task.workspace / "marker").exists()
    assert not (value.task.workspace / "result.txt").exists()


def test_development_scope_inherits_review_without_loading_root_only_paths(tmp_path):
    """显式 develop 规则继承父审核，根专用目录缺失不阻止子初始化；实际工作区指纹仍独立。"""
    from types import SimpleNamespace
    from uuid import uuid4
    from agent_harness.hooks.controller import HookController
    parent_path, child_path = tmp_path / "parent", tmp_path / "child"
    parent_path.mkdir()
    child_path.mkdir()
    (parent_path / "root-only").mkdir()
    script = tmp_path / "hook.py"
    script.write_text('print("{}")', encoding="utf-8")
    policy = HookPolicy(rules=[HookRule(id="parent", event="RunStart", argv=[sys.executable, str(script)], cwd="root-only"), HookRule(id="child", event="RunStart", argv=[sys.executable, str(script)], agent_scopes=["develop"])])
    store = HookStore(tmp_path / "records")
    for item in discover(parent_path, policy, store):
        review(parent_path, policy, store, HookReview(key=item["key"], fingerprint=item["fingerprint"], action="trust"))

    def context(workspace, delegation=None, parent=None):
        """构造 workspace/角色/父关系的最小只读上下文，返回命名空间，不运行模型或工具。"""
        return SimpleNamespace(runs_root=tmp_path / "records", result=SimpleNamespace(hooks={}), budget_parent=parent,
                manifest=SimpleNamespace(run_id=uuid4(), task=SimpleNamespace(workspace=workspace), config=RunConfig(hooks=policy), delegation=delegation, automation=None))

    parent = HookController(context(parent_path), None)
    child = HookController(context(child_path, SimpleNamespace(role="develop"), SimpleNamespace(hooks=parent)), None)
    assert [item["key"] for item in child.definitions] == ["host:child"]
    assert child.definitions[0]["trusted"] and child.definitions[0]["inherited_trust"]
    assert child.definitions[0]["fingerprint"] != parent.definitions[1]["fingerprint"]


def test_cli_reviews_exact_definition_without_running_script(tmp_path, capsys):
    """CLI list/trust 只审核相同配置的指纹，不启动脚本或制造 marker。"""
    from agent_harness.cli import main
    value, root = configured(tmp_path, "RunStart", 'from pathlib import Path\nPath("marker").touch()\nprint("{}")', trust=False)
    config = tmp_path / "hooks.toml"
    config.write_text('[[hooks.rules]]\nid = "check"\nevent = "RunStart"\nargv = ' + json.dumps(value.config.hooks.rules[0].argv) + '\n', encoding="utf-8")
    args = ["--workspace", str(value.task.workspace), "--runs-dir", str(root), "--config", str(config)]
    assert main(["hooks", "list", *args]) == 0
    item = json.loads(capsys.readouterr().out)["items"][0]
    assert not item["trusted"]
    assert main(["hooks", "trust", *args, "--key", item["key"], "--fingerprint", item["fingerprint"]]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["trusted"]
    assert not (value.task.workspace / "marker").exists()
