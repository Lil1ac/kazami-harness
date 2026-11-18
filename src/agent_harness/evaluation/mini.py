"""固定 mini-SWE-agent 的原生循环、提示与工具格式，接入统一环境和请求账本。"""

import asyncio
import hashlib
import json
import os
import ssl
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
import httpx
import yaml

from agent_harness.evaluation.budget import USD_PICOS
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.execution.processes import native_io_path
from agent_harness.runtime.runner import AgentStopped
from agent_harness.schemas import RunConfig, RunResult


def native_components():
    """延迟读取固定版本上游组件；返回实现和原生配置，不读取任何模型凭据。"""
    os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(Path(".runtime/ev3/mini-config").resolve()))
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    from minisweagent import __version__, package_dir
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.exceptions import FormatError, InterruptAgentFlow
    from minisweagent.environments.docker import DockerEnvironment
    from minisweagent.models.utils.actions_toolcall import BASH_TOOL, parse_toolcall_actions, format_toolcall_observation_messages
    if __version__ != "2.4.6":
        raise ValueError("外部基线必须固定为 mini-SWE-agent 2.4.6")
    native = yaml.safe_load((package_dir / "config/benchmarks/swebench.yaml").read_text(encoding="utf-8"))
    return SimpleNamespace(agent=DefaultAgent, format_error=FormatError, interrupt=InterruptAgentFlow,
        finished=DockerEnvironment._check_finished, tool=BASH_TOOL, parse=parse_toolcall_actions,
        observations=format_toolcall_observation_messages, config=native, package=package_dir)


class MiniModel:
    """保留上游工具解析和观察格式；传输通过宿主异步循环、统一计费和硬门禁。"""

    def __init__(self, bridge, native):
        """绑定 bridge 宿主与 native 上游组件；不发请求，模型配置仅保存非秘密参数。"""
        self.bridge = bridge  # 单试次宿主，持有账本、期限和传输。
        self.native = native  # 固定版本的工具模板和格式解析器。

    def query(self, messages: list[dict], **kwargs) -> dict:
        """提交 messages 并按原生协议解析动作；格式错误仍保存收费回执，kwargs 不改变冻结配置。"""
        payload, cost = self.bridge.sync(self.bridge.query(messages))
        choice = payload["choices"][0]
        message = choice["message"]
        calls = [SimpleNamespace(id=call["id"], function=SimpleNamespace(**call["function"]))
                 for call in message.get("tool_calls") or []]
        extra = {"cost": cost, "response": payload, "timestamp": time.time()}
        try:
            actions = self.native.parse(calls, format_error_template=self.native.config["model"]["format_error_template"],
                                        template_kwargs={"finish_reason": choice.get("finish_reason")})
        except self.native.format_error as error:
            error.messages[0]["extra"].update(extra)
            raise
        return {**message, "extra": {**extra, "actions": actions}}

    def format_message(self, **kwargs) -> dict:
        """返回原生文本消息字段；本轮不启用多模态展开。"""
        return kwargs

    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None) -> list[dict]:
        """用原生模板格式化 message 对应的 outputs；返回配对工具消息，不改截断规则。"""
        return self.native.observations(actions=message.get("extra", {}).get("actions", []), outputs=outputs,
            observation_template=self.native.config["model"]["observation_template"], template_vars=template_vars)

    def get_template_vars(self, **kwargs) -> dict:
        """返回冻结模型模板变量；kwargs 不覆盖获批配置。"""
        return {**self.native.config["model"], "model_name": self.bridge.config.requested_model}

    def serialize(self) -> dict:
        """返回可审计模型配置，禁止保存 API Key。"""
        return {"info": {"config": {"model": {"model_name": self.bridge.config.requested_model,
            "generation": self.bridge.config.generation.model_dump(), "max_tokens": 8192, "http_attempts": 1}}}}


class MiniEnvironment:
    """将原生 Bash 动作交统一隔离仓库环境，保留上游提交标记判断。"""

    def __init__(self, bridge, native):
        """绑定 bridge 和 native 组件；不创建额外共享工作区。"""
        self.bridge = bridge  # 执行限额和当前独立仓库副本。
        self.native = native  # 原生观察与提交语义。

    def execute(self, action: dict, cwd: str = "") -> dict:
        """在当前工作副本执行 action；cwd 仅允许空或 /testbed，返回输出或原生 Submitted。"""
        if cwd not in {"", "/testbed"}:
            raise ValueError("基线工作目录必须固定")
        output = self.bridge.sync(self.bridge.command(action))
        self.native.finished(self, output)
        return output

    def get_template_vars(self, **kwargs) -> dict:
        """返回固定的 Linux 环境变量供上游提示渲染，不读取宿主环境变量。"""
        return {**self.native.config["environment"], "system": "Linux", "machine": "x86_64"}

    def serialize(self) -> dict:
        """返回本题容器身份及资源上限，不含评分脚本或参考补丁。"""
        return {"info": {"config": {"environment": {"image": self.bridge.environment.image,
            "network": "none", "cpus": 2, "memory_bytes": 4294967296, "timeout": 120}}}}


class MiniBridge:
    """在主事件循环处理请求和工具；原生同步控制循环在一个受控线程执行。"""

    def __init__(self, config: RunConfig, environment, workspace: Path, run_id: str, budget, client, native):
        """冻结单题 config、环境、目录及账本；client 是已配置认证的传输，不将凭据落盘。"""
        self.config, self.environment, self.workspace = config, environment, workspace  # 固定的试次环境。
        self.run_id, self.budget, self.client, self.native = run_id, budget, client, native  # 身份、账本和原生组件。
        self.loop = asyncio.get_running_loop()  # 所有费用和进程操作回到同一写入线程。
        self.started = self.loop.time()  # Agent 时钟不包含准备和评分。
        self.deadline = self.started + config.limits.run_timeout_seconds  # 绝对截止时间。
        self.cancelled = False  # 用户取消时阻止同步线程发起下一次操作。
        self.result = RunResult(run_id=UUID(run_id), execution_status="running", usage_complete=True)  # 通用执行事实。
        self.input_sizes: list[int] = []  # 实际请求输入字节，单独保留峰值和末次值。
        self.timings = {"model_seconds": 0.0, "tool_seconds": 0.0}  # 同一 Agent 时钟上的分段耗时。

    def sync(self, operation):
        """从原生线程等待宿主协程 operation；异常原样回到上游循环，不增加重试。"""
        return asyncio.run_coroutine_threadsafe(operation, self.loop).result()

    def remaining(self) -> float:
        """返回剩余 Agent 秒数；取消或到期抛停止异常，禁止继续执行。"""
        if self.cancelled:
            raise AgentStopped("cancelled", "cancelled")
        remaining = self.deadline - self.loop.time()
        if remaining <= 0:
            raise AgentStopped("timed_out", "run_timeout")
        return remaining

    async def query(self, messages: list[dict]) -> tuple[dict, float]:
        """发送一条原生请求并结算，返回响应和峰时美元；超时或未知用量保留预留并停轮。"""
        prepared = [{key: value for key, value in message.items() if key != "extra"} for message in messages]
        tools = [self.native.tool]
        size = len(json.dumps({"messages": prepared, "tools": tools}, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False).encode())
        if size > self.config.context.max_input_bytes:
            raise AgentStopped("limit_exceeded", "max_model_input_bytes")
        seconds = min(self.remaining(), self.config.limits.model_turn_timeout_seconds)
        turn = self.result.model_turns + 1
        if turn > self.config.limits.max_model_turns:
            raise AgentStopped("limit_exceeded", "max_model_turns")
        self.budget.authorize(self.run_id, turn, "execution")
        self.result.model_turns = turn
        self.input_sizes.append(size)
        started = self.loop.time()
        payload = None
        try:
            self.result.http_attempts += 1
            async with asyncio.timeout(seconds):
                response = await self.client.post("https://api.deepseek.com/v1/chat/completions", json={
                    "model": self.config.requested_model, "messages": prepared, "tools": tools,
                    "max_tokens": self.config.limits.model_max_output_tokens, "stream": False,
                    **self.config.generation.request_fields()})
            response.raise_for_status()
            payload = response.json()
        except (Exception, asyncio.CancelledError) as error:
            atomic_json(self.environment.directory / (self.run_id + "-transport.json"), {
                "stage": "request", "exception_type": type(error).__name__, "model_turn": turn,
                "usage": "unknown"})  # 只存白名单字段，不能据此释放未知用量预留。
            self.budget.settle(self.run_id, turn, None, None, "model_interrupted")
            self.result.usage_complete = False
            raise AgentStopped("failed", "unknown_usage") from None
        finally:
            self.timings["model_seconds"] += self.loop.time() - started
        usage, model = payload.get("usage"), payload.get("model")
        reason = self.budget.settle(self.run_id, turn, usage, model)
        self.result.response_model = model
        if reason:
            self.result.usage_complete = False
            raise AgentStopped("failed", reason)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            setattr(self.result.usage, key, getattr(self.result.usage, key) + usage[key])
        charge = self.budget.state["requests"][-1]["known_usd_picos"] / USD_PICOS
        return payload, charge

    async def command(self, action: dict) -> dict:
        """执行 action 的 Bash 命令并返回原生观察字段；含槽位等待在内受剩余期限限制。"""
        seconds = min(self.remaining(), self.config.limits.command_timeout_seconds)
        if self.result.tool_calls >= self.config.limits.max_tool_calls:
            raise AgentStopped("limit_exceeded", "max_tool_calls")
        self.result.tool_calls += 1
        started = self.loop.time()
        try:
            # 在容器内合并双流，保留原生提交检测依赖的真实首行顺序，不能先拼stdout再拼stderr。
            output = await self.environment.command(self.workspace, ["-c",
                '{ source /opt/miniconda3/bin/activate testbed && cd /testbed && exec bash -c "$1"; } 2>&1',
                "mini", action["command"]], seconds, deadline=self.deadline, env=self.native.config["environment"].get("env"))
            self.result.cleanup_status = "complete"
        except RuntimeError:
            self.result.cleanup_status = "unknown"
            raise AgentStopped("failed", "cleanup_unconfirmed") from None
        finally:
            self.timings["tool_seconds"] += self.loop.time() - started
        stdout = self.environment.full_stdout(output)
        stderr_ref = output.get("stderr_ref")
        stderr = native_io_path(self.environment.directory / "processes" / Path(stderr_ref).relative_to("artifacts")).read_text(
            encoding="utf-8", errors="replace") if stderr_ref else output.get("stderr", "")
        return {"output": stdout + stderr, "returncode": output.get("exit_code", -1),
                "exception_info": output.get("error_code") or ""}


async def run_mini(config: RunConfig, environment, workspace: Path, run_id: str, task: str, budget,
                   api_key: str, trajectory_path: Path, *, transport=None) -> tuple[RunResult, dict]:
    """运行固定外部基线，返回通用结果与原生轨迹索引；费用守卫必须提供，transport 仅供离线夹具。"""
    native = native_components()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + api_key}, transport=transport,
                                 timeout=config.limits.model_turn_timeout_seconds, trust_env=False, follow_redirects=False,
                                 verify=ssl.create_default_context()) as client:
        bridge = MiniBridge(config, environment, workspace, run_id, budget, client, native)
        agent = native.agent(MiniModel(bridge, native), MiniEnvironment(bridge, native), **{
            **native.config["agent"], "step_limit": config.limits.max_model_turns, "cost_limit": 0,
            "wall_time_limit_seconds": int(config.limits.run_timeout_seconds), "output_path": trajectory_path})
        future = asyncio.create_task(asyncio.to_thread(agent.run, task))
        try:
            terminal = await asyncio.shield(future)
            bridge.result.execution_status = "completed" if terminal.get("exit_status") == "Submitted" else "failed"
            bridge.result.stop_reason = None if bridge.result.execution_status == "completed" else terminal.get("exit_status")
            bridge.result.final_answer = terminal.get("submission")
        except AgentStopped as error:
            bridge.result.execution_status, bridge.result.stop_reason = error.status, error.reason
        except asyncio.CancelledError:
            bridge.cancelled = True
            try:
                await asyncio.shield(future)
            except Exception:
                pass
            bridge.result.execution_status, bridge.result.stop_reason = "cancelled", "cancelled"
        except Exception:
            bridge.result.execution_status, bridge.result.stop_reason = "failed", "mini_adapter_error"
        bridge.result.active_duration_seconds = bridge.loop.time() - bridge.started
        trajectory = agent.serialize()
        trajectory["harness_receipt"] = bridge.result.model_dump(mode="json")
        atomic_json(trajectory_path, trajectory)
        return bridge.result, {"trajectory_sha256": hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
            "trajectory_available": True, "input_bytes_peak": max(bridge.input_sizes, default=0),
            "last_admitted_input_bytes": bridge.input_sizes[-1] if bridge.input_sizes else None,
            "execution_calls": bridge.result.model_turns, "summary_calls": 0, **bridge.timings}
