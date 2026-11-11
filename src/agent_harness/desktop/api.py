"""仅在经过会话认证的桌面服务中安装原生操作接口。"""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from pydantic import Field, SecretStr, ValidationError

from agent_harness.config import ConfigurationError
from agent_harness.model_selection import CustomProviderSettings, ModelProvider, ModelVersion
from agent_harness.schemas import DataModel
from agent_harness.web.service import WebRunManager, WebServiceError
from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import DesktopRunSettings


class SettingsRequest(DataModel):
    """桌面公开设置的完整保存请求，不接受内部运行策略或密钥。"""

    config: DesktopRunSettings  # 模型、工具和总体预算。
    downloads_directory: str = Field(min_length=1, max_length=4096)  # 用户选择的绝对下载目录。


class ProviderWrite(DataModel):
    """一次提交连接定义和可选凭据，密钥不进入公开设置。"""

    definition: CustomProviderSettings  # 用户填写的地址、认证方式与可选模型目录。
    secret: SecretStr | None = Field(default=None, max_length=2560)  # None 保留当前分区，空字符串明确清除。


class ProviderUpdate(ProviderWrite):
    """按页面已审阅的版本保存完整自定义服务商定义。"""

    version: ModelVersion  # 更新前页面读取的端点版本。


class UrlRequest(DataModel):
    """用户明确点击的链接；下载接口仅接受宿主可核验的产物或项目记忆导出相对 URL。"""

    url: str = Field(min_length=1, max_length=16384)  # 根据原生操作分别校验地址范围。


def install_routes(app: FastAPI, host: DesktopHost, manager: WebRunManager) -> None:
    """向 app 安装 host 原生路由；manager 提供统一的产物校验和公开状态，返回 None。"""

    @app.put("/api/desktop/settings")
    async def settings(body: SettingsRequest) -> dict:
        """保存 body 中的公开设置并返回最新选项；非法预算、路径或写盘失败返回受控错误。"""
        try:
            await host.configure(body.config, Path(body.downloads_directory))
        except (ConfigurationError, ValidationError, OSError):
            raise WebServiceError(422, "desktop_settings_failed", "设置保存失败，请检查预算、绝对下载路径和目录权限。") from None
        result = await asyncio.to_thread(manager.options)
        result["desktop"] = host.public_settings()
        return result

    @app.post("/api/desktop/model-providers", status_code=201)
    async def create_provider(body: ProviderWrite) -> dict:
        """登记 body 的连接和密钥，返回稳定标识及更新后的公开选项。"""
        try:
            secret = body.secret.get_secret_value().strip() if body.secret is not None else None
            provider = await host.configure_provider(None, None, body.definition, secret)
            return {"provider": provider, "options": {**await asyncio.to_thread(manager.options), "desktop": host.public_settings()}}
        except ConfigurationError:
            raise WebServiceError(503, "model_provider_save_failed", "模型服务保存失败，请检查本机加密存储和目录权限。") from None

    @app.put("/api/desktop/model-providers/{provider}")
    async def update_provider(provider: ModelProvider, body: ProviderUpdate) -> dict:
        """按 body 中的版本更新 provider，返回标识和公开选项。地址变化时使用新凭据分区。"""
        try:
            secret = body.secret.get_secret_value().strip() if body.secret is not None else None
            await host.configure_provider(provider, body.version, body.definition, secret)
            return {"provider": provider, "options": {**await asyncio.to_thread(manager.options), "desktop": host.public_settings()}}
        except ConfigurationError:
            raise WebServiceError(503, "model_provider_save_failed", "模型服务保存失败，请检查本机加密存储和目录权限。") from None

    @app.post("/api/desktop/download")
    async def download(body: UrlRequest) -> dict:
        """将 body 指向的已登记产物保存到下载目录，返回文件名和路径，不覆盖已有文件。"""
        try:
            source = await host.artifact(body.url)
            return await host.download(source)
        except (ConfigurationError, OSError, ValueError):
            raise WebServiceError(422, "download_failed", "无法保存产物，请检查文件和下载目录权限。") from None

    @app.post("/api/desktop/reveal")
    async def reveal(body: UrlRequest) -> dict:
        """在资源管理器中定位 body 对应的固化产物；不接受任意宿主路径，不执行文件。"""
        target = await host.artifact(body.url)
        await host.reveal(target)
        return {"opened": True}

    @app.post("/api/desktop/external")
    async def external(body: UrlRequest) -> dict:
        """把用户点击的 body 网页或邮件链接交给系统默认应用，返回 opened=True。"""
        await host.open_external(body.url)
        return {"opened": True}
