"""后台任务与 Electron 原生能力的边界，复用现有 HTTP/SSE 与目录登记链路。"""

import hmac
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from starlette.requests import Request

from agent_harness.config import ConfigurationError
from agent_harness.schemas import RunConfig
from agent_harness.web.project_catalog import CatalogError
from agent_harness.web.service import WebServiceError
from agent_harness.desktop.bridge import NativeClient
from agent_harness.desktop.storage import DesktopPreferences, DesktopRunSettings, PreferenceStore

if TYPE_CHECKING:
    from agent_harness.web.service import WebRunManager


class DesktopHost:
    """保存会话和任务凭据内存副本，原生操作全部交给 Electron 父进程。"""

    def __init__(self, store: PreferenceStore, native: NativeClient, token: str, api_key: str) -> None:
        """绑定公开 store、原生 native 和私有 token/api_key；不回显或落盘凭据。"""
        self.store = store  # 公开设置的稳定存储。
        self.native = native  # 当前 Electron 父进程提供的受控能力。
        self.token = token  # 父进程生成并通过标准输入传入的会话随机值。
        self.cookie_name = "harness_desktop"  # Electron 为当前隔离会话设置的 HttpOnly Cookie。
        self.manager: WebRunManager | None = None  # create_app 绑定统一任务调度器。
        self._key = api_key  # 只驻留内存；加密文件由 Electron 管理。
        self._picker_active = False  # 同时只允许一个原生选择请求。

    @property
    def available(self) -> bool:
        """返回桌面模式支持原生目录选择；真正打开时由父进程验证窗口仍存在。"""
        return True

    def api_key(self, config: RunConfig) -> str:
        """返回 config 对应任务的内存凭据；只传 Runner，不写入环境或配置。"""
        return self._key

    def authorized(self, request: Request) -> bool:
        """验证页面 Cookie 或主进程 Bearer token；其他浏览器不能访问桌面服务。"""
        # 用字节比较同时处理非 ASCII 的无效输入，避免恶意 Cookie 触发 TypeError 而变成500。
        return hmac.compare_digest(request.cookies.get(self.cookie_name, "").encode("utf-8"), self.token.encode("utf-8")) or hmac.compare_digest(request.headers.get("authorization", "").encode("utf-8"), ("Bearer " + self.token).encode("utf-8"))

    def public_settings(self) -> dict:
        """返回桌面能力与公开目录；不返回会话令牌、密钥或 IPC 细节。"""
        return {"downloads_directory": str(self.store.value.downloads_directory), "data_directory": str(self.store.directory), "credential_source": "electron_safe_storage"}

    def configure(self, settings: DesktopRunSettings, downloads: Path) -> dict:
        """保存 settings 和绝对 downloads，返回公开状态；活动任务继续使用原快照。"""
        if self.manager is None:
            raise ConfigurationError("桌面服务尚未就绪。")
        preferences = DesktopPreferences(downloads_directory=downloads)
        config = settings.apply(self.manager.run_config)
        self.store.update(config=settings, downloads_directory=preferences.downloads_directory)
        self.manager.run_config = config
        return self.public_settings()

    async def save_key(self, secret: str) -> None:
        """请求 Electron 加密保存 secret 后更新内存；空字符串清除，不修改活动调用。"""
        value = secret.strip()
        await self.native.request("credential.write", {"secret": value})
        self._key = value

    async def choose(self) -> Path | None:
        """请求 Electron 选择已有目录；取消返回 None，并发打开抛409，不阻塞其他请求。"""
        if self._picker_active:
            raise CatalogError("folder_picker_busy", "文件夹选择框已打开，请先完成或取消选择。", 409)
        self._picker_active = True
        try:
            selected = await self.native.request("folder.choose", {})
            if selected is None:
                return None
            if not isinstance(selected, str) or not Path(selected).is_absolute():
                raise ConfigurationError("选择器返回了无效目录。")
            return Path(selected).resolve()
        except ConfigurationError:
            raise CatalogError("folder_picker_failed", "原生文件夹选择未完成，请重试。", 503) from None
        finally:
            self._picker_active = False

    async def artifact(self, relative_url: str) -> Path:
        """将产物、补丁或记忆导出 relative_url 核对为固定路径；拒绝任意文件和网络地址。"""
        url = urlsplit(relative_url)
        memory = re.fullmatch(r"/api/projects/([a-zA-Z0-9_-]{1,64})/memories/export", url.path)
        if memory is not None and self.manager is not None and not (url.scheme or url.netloc or url.query or url.fragment):
            from agent_harness.web.memory import export_project_memory
            return export_project_memory(self.manager, memory[1])
        match = re.fullmatch(r"/api/runs/([0-9a-fA-F-]{36})/(artifacts|patch)", url.path)
        if url.scheme or url.netloc or url.fragment or match is None or self.manager is None:
            raise WebServiceError(400, "invalid_artifact", "请选择当前任务中已登记的产物。")
        try:
            identifier = UUID(match[1])
            query = parse_qs(url.query, strict_parsing=True)
        except ValueError:
            raise WebServiceError(400, "invalid_artifact", "产物地址无效。") from None
        if match[2] == "patch" and not query:
            return await self.manager.project_patch(identifier)
        if match[2] != "artifacts" or set(query) != {"location", "path"} or any(len(values) != 1 for values in query.values()) or query["location"][0] not in {"workspace", "run"}:
            raise WebServiceError(400, "invalid_artifact", "产物地址无效。")
        return self.manager.artifact_path(identifier, query["location"][0], query["path"][0])

    async def download(self, source: Path) -> dict:
        """让 Electron 保存已核对的 source 并返回路径；目标来自公开设置，不覆盖同名文件。"""
        return await self.native.request("file.download", {"source": str(source), "directory": str(self.store.value.downloads_directory)})

    async def reveal(self, target: Path) -> None:
        """请求资源管理器定位已核对的 target；不执行文件，不接受页面任意宿主路径。"""
        await self.native.request("file.reveal", {"path": str(target)})

    async def open_external(self, url: str) -> None:
        """把合法网页或邮件 url 交给默认应用；拒绝脚本、本地文件及带凭据地址。"""
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "mailto"} or (parsed.scheme != "mailto" and not parsed.hostname) or parsed.username or parsed.password or any(char in url for char in "\r\n\x00"):
            raise WebServiceError(400, "invalid_external_url", "只支持打开网页或邮件链接。")
        await self.native.request("external.open", {"url": url})
