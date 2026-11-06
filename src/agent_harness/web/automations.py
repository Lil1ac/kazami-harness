"""自动化管理同源API；只接受公开模板和人工决定，不接受宿主路径、内部来源或模型授权。"""

from agent_harness.automations.models import AutomationDefinition, RuleEdit, RuleAction, GrantRequest, Schedule, preview
from datetime import datetime, UTC


def install_automation_routes(app, manager) -> None:
    """向app安装manager自动化路由；返回None，安装不创建规则或启动执行。"""
    service = manager.automations

    @app.get("/api/automations")
    async def list_rules() -> dict:
        """返回当前规则及触发服务状态，不启动或恢复任何任务。"""
        return service.list()

    @app.post("/api/automations/preview")
    async def schedule_preview(body: Schedule) -> dict:
        """按body计算当前之后五次带时区时间，不保存或触发规则。"""
        current = datetime.now(UTC)
        return {"items": preview(body, current, current)}

    @app.post("/api/automations", status_code=201)
    async def create_rule(body: AutomationDefinition) -> dict:
        """保存body为暂停规则并返回公开视图；启动必须另行明确选择。"""
        async with service.lock:
            return service.save(body)

    @app.put("/api/automations/{identifier}")
    async def edit_rule(identifier: str, body: RuleEdit) -> dict:
        """以body.revision替换identifier定义；编辑使持续授权失效并取消未开始请求。"""
        async with service.lock:
            return service.save(body.definition, identifier, body.revision)

    @app.get("/api/automations/{identifier}/review")
    async def review_rule(identifier: str) -> dict:
        """返回identifier授权审核快照；读取不批准任何动作。"""
        return service.review(identifier)

    @app.post("/api/automations/{identifier}/grant")
    async def grant_rule(identifier: str, body: GrantRequest) -> dict:
        """为identifier保存body精确持续许可；宿主关闭、版本或指纹不符则拒绝。"""
        async with service.lock:
            return service.grant(identifier, body)

    @app.post("/api/automations/{identifier}/actions/{action}")
    async def rule_action(identifier: str, action: str, body: RuleAction) -> dict:
        """对identifier执行body绑定的启停/撤销/试跑action；返回新视图，重试幂等。"""
        return await service.action(identifier, body.revision, body.request_id, action)

    @app.get("/api/automations/{identifier}/history")
    async def rule_history(identifier: str) -> dict:
        """返回identifier触发、作业及验收历史；不重放旧请求。"""
        return service.history(identifier)

    @app.get("/api/automation-notifications")
    async def notifications() -> dict:
        """返回持久通知及已读状态；刷新不重复推送桌面弹窗。"""
        return service.notifications()

    @app.post("/api/automation-notifications/{identifier}/read")
    async def mark_read(identifier: str) -> dict:
        """将identifier通知明确标为已读；重复调用无额外副作用。"""
        return service.acknowledge(identifier)
