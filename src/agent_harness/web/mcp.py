"""外部连接管理 API；保存、信任启动、发现及调用授权分别表达。"""

import os
from uuid import UUID

from fastapi import Query
from pydantic import Field, SecretStr

from agent_harness.mcp.client import McpError, local_identity, perform
from agent_harness.mcp.models import McpModel, McpReview, McpServerConfig
from agent_harness.work.tasks import TaskError
from agent_harness.config import ConfigurationError


class McpSave(McpModel):
    """保存公开配置的显式请求；更新必须提供页面看到的原指纹。"""

    config: McpServerConfig  # 本地程序或远程端点及凭据引用。
    fingerprint: str | None = None  # 新增为空；更新时用于乐观锁，不能覆盖其他编辑。


class McpCredential(McpModel):
    """仅桌面接收的凭据写入请求；正文不可回读，清除使用空字符串。"""

    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")  # 写入前重新核验对应公开连接版本。
    secret: SecretStr = Field(max_length=2560)  # 不记录输入；写入或清除都会撤销旧连接审核。


def install_mcp_routes(app, manager, desktop=None) -> None:
    """为 app 安装连接管理；manager 共享存储和宿主上限，desktop=None 只支持环境凭据。"""
    store = manager.mcp
    credentials_pending: set[str] = set()  # 原生凭据写入跨 await；期间禁止重新审核或启用同连接。

    @app.get("/api/runs/{run_id}/mcp")
    async def run_evidence(run_id: UUID) -> dict:
        """读取 run_id 完整持久外部证据；不受前端事件窗口裁剪，不启动历史服务。"""
        manager.get_run(run_id)
        history = manager._read_history(run_id)
        return {"tools": [event.payload for event in history.events if event.type == "mcp_prepared"],
                "items": [{"seq": event.seq, "type": event.type, **event.payload} for event in history.events if event.type in {"mcp_call_started", "mcp_call_result", "mcp_call_failed"}]}

    @app.get("/api/mcp")
    async def list_connections() -> dict:
        """返回连接与工具选择目录；无副作用，不因查看设置页启动服务。"""
        return {"items": store.list(), "tools": store.options(), "desktop_credentials": desktop is not None,
                "policy": manager.run_config.permission_policy.external}

    @app.put("/api/mcp")
    async def save_connection(body: McpSave) -> dict:
        """保存 body 配置并撤销旧审核；不运行程序，不自动启用。"""
        return store.save(body.config, body.fingerprint)

    @app.post("/api/mcp/{identifier}/review")
    async def review_connection(identifier: str, body: McpReview) -> dict:
        """审核 identifier 的固定 body 版本；信任允许连接发现，启用才开放任务选择。"""
        record = store.get(identifier)
        if identifier in credentials_pending and body.action != "disable":
            raise TaskError("mcp_credential_busy", "连接凭据正在更新，请完成后重新审核。")
        if body.fingerprint != record["fingerprint"]:
            raise TaskError("mcp_stale", "页面版本已变化，请刷新后重新审核。")
        if body.action == "disable":
            changes = {"enabled": False, "status": "disabled"}
        elif body.action == "trust":
            if manager.run_config.permission_policy.external == "deny":
                raise TaskError("mcp_host_denied", "宿主禁止外部连接，不能启动或连接服务。")
            identity = local_identity(McpServerConfig.model_validate(record["config"]))
            changes = {"trusted": True, "enabled": False, "status": "disconnected", "trusted_identity": identity}
        elif body.action == "research":
            if not record["trusted"] or not set(body.research_tools).issubset({item["name"] for item in record["catalog"]}):
                raise TaskError("mcp_review_required", "请先审核连接并发现当前工具目录。")
            changes = {"research_tools": sorted(set(body.research_tools)), "enabled": False, "status": "disabled"}
        else:
            if manager.run_config.permission_policy.external == "deny":
                raise TaskError("mcp_host_denied", "宿主禁止外部工具，不能启用连接。")
            if not record["trusted"] or not record["catalog"] or record["status"] == "needs_review":
                raise TaskError("mcp_review_required", "请先核对连接并重新发现工具；未知结果须先人工核对远端。")
            changes = {"enabled": True, "status": "ready", "error": None}
        return store.update(identifier, body.fingerprint, changes)

    @app.post("/api/mcp/{identifier}/discover")
    async def discover_connection(identifier: str, fingerprint: str = Query(pattern=r"^[a-f0-9]{64}$")) -> dict:
        """连接 identifier 并发现工具；fingerprint 绑定已审核配置，失败不覆盖原目录。"""
        record = store.get(identifier)
        config = McpServerConfig.model_validate(record["config"])
        if identifier in credentials_pending:
            raise TaskError("mcp_credential_busy", "连接凭据正在更新，暂不能连接服务。")
        if manager.run_config.permission_policy.external == "deny":
            raise TaskError("mcp_host_denied", "宿主禁止连接外部服务。")
        if record["fingerprint"] != fingerprint or not record["trusted"]:
            raise TaskError("mcp_review_required", "必须先审核当前连接配置，才能启动或连接服务。")
        if local_identity(config) != record.get("trusted_identity", {}):
            raise TaskError("mcp_target_changed", "本机入口已变化，请重新审核程序和脚本。")
        try:
            secret = "" if config.transport == "stdio" else os.environ.get(config.credential_env, "") if config.credential_env else (await manager.mcp_credential_provider(identifier) if manager.mcp_credential_provider else "")
            if config.credential_env and not secret or any(name not in os.environ for name in config.env_vars):
                raise McpError("mcp_auth_required", "连接需要的凭据或环境变量尚未配置。")
            if store.get(identifier)["fingerprint"] != fingerprint:
                raise TaskError("mcp_stale", "等待凭据期间连接已被修改或撤权，尚未连接。")
            from agent_harness.execution.scheduling import host_limits
            token = host_limits.set(manager.coordinator.limits)  # 手动发现也共享宿主进程容量，不占用无归属额外进程。
            try:
                result = await perform(config, secret=secret)
            finally:
                host_limits.reset(token)
        except McpError as error:
            return store.update(identifier, fingerprint, {"enabled": False, "status": "auth_required" if error.code == "mcp_auth_required" else "error", "error": error.message})
        except ConfigurationError:
            return store.update(identifier, fingerprint, {"enabled": False, "status": "auth_required", "error": "桌面凭据暂时无法解密，请检查原生凭据服务。"})
        changed = record["catalog"] != result["catalog"]
        return store.update(identifier, fingerprint, {"catalog": result["catalog"], "protocol_version": result["protocol_version"],
            "research_tools": [] if changed else record["research_tools"], "enabled": False, "status": "discovered", "error": None})

    @app.put("/api/mcp/{identifier}/credential")
    async def save_credential(identifier: str, body: McpCredential) -> dict:
        """把 identifier 的 body 密钥交桌面加密保存，撤销旧审核；非桌面拒绝，不回显密钥。"""
        if desktop is None:
            raise TaskError("mcp_credentials_unavailable", "独立 Web 和 CLI 请使用宿主环境变量引用。")
        record = store.get(identifier)
        if identifier in credentials_pending:
            raise TaskError("mcp_credential_busy", "连接凭据正在更新，请勿重复提交。")
        if record["fingerprint"] != body.fingerprint:
            raise TaskError("mcp_stale", "连接版本已变化，未修改凭据。")
        # 先撤销调用授权，再更换秘密；原生保存失败也不会让旧批准调用新凭据。
        credentials_pending.add(identifier)
        try:
            updated = store.save(McpServerConfig.model_validate(record["config"]), body.fingerprint)
            await desktop.native.request("mcp.credential.write", {"key": identifier, "secret": body.secret.get_secret_value()})
            return updated
        finally:
            credentials_pending.discard(identifier)
