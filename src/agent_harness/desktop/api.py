"""仅在经过会话认证的桌面服务中安装原生操作接口。"""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from pydantic import Field, SecretStr, ValidationError

from agent_harness.config import ConfigurationError
from agent_harness.schemas import DataModel
from agent_harness.web.service import WebRunManager, WebServiceError
from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import DesktopRunSettings


class SettingsRequest(DataModel):
    """桌面公开设置的完整保存请求，不接受内部运行策略或密钥。"""

    config: DesktopRunSettings  # 模型、工具和总体预算。
    downloads_directory: str = Field(min_length=1, max_length=4096)  # 用户选择的绝对下载目录。


class CredentialRequest(DataModel):
    """模型凭据写入请求；SecretStr 避免异常或调试表示意外输出密钥。"""

    secret: SecretStr = Field(max_length=2560)  # 空字符串表示明确清除，接口永不回读正文。


class UrlRequest(DataModel):
    """用户明确点击的链接；下载接口仅接受宿主可核验的产物或项目记忆导出相对 URL。"""

    url: str = Field(min_length=1, max_length=16384)  # 根据原生操作分别校验地址范围。


def install_routes(app: FastAPI, host: DesktopHost, manager: WebRunManager) -> None:
    """向 app 安装 host 原生路由；manager 提供统一的产物校验和公开状态，返回 None。"""

    @app.put("/api/desktop/settings")
    async def settings(body: SettingsRequest) -> dict:
        """保存 body 中的公开设置并返回最新选项；非法预算、路径或写盘失败返回受控错误。"""
        try:
            await asyncio.to_thread(host.configure, body.config, Path(body.downloads_directory))
        except (ConfigurationError, ValidationError, OSError):
            raise WebServiceError(422, "desktop_settings_failed", "设置保存失败，请检查预算、绝对下载路径和目录权限。") from None
        result = await asyncio.to_thread(manager.options)
        result["desktop"] = host.public_settings()
        return result

    @app.put("/api/desktop/credential")
    async def credential(body: CredentialRequest) -> dict:
        """把 body 密钥交给 Electron 加密保存；只返回存在标记，空字符串明确清除。"""
        try:
            await host.save_key(body.secret.get_secret_value())
        except ConfigurationError:
            raise WebServiceError(503, "credential_store_failed", "本机凭据加密保存失败，请检查当前用户权限。") from None
        return {"key_configured": bool(host.api_key(manager.run_config))}

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
