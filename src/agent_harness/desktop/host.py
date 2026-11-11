"""后台任务与 Electron 原生能力的边界，复用现有 HTTP/SSE 与目录登记链路。"""

import asyncio
import hmac
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from starlette.requests import Request

from agent_harness.config import ConfigurationError
from agent_harness.schemas import RunConfig
from agent_harness.model_selection import CustomProvider, CustomProviderSettings, ModelProvider, valid_model_key
from agent_harness.web.project_catalog import CatalogError
from agent_harness.web.service import WebServiceError
from agent_harness.desktop.bridge import NativeClient
from agent_harness.desktop.storage import DesktopPreferences, DesktopRunSettings, PreferenceStore

if TYPE_CHECKING:
    from agent_harness.web.service import WebRunManager


class DesktopHost:
    """保存会话和任务凭据内存副本，原生操作全部交给 Electron 父进程。"""

    def __init__(self, store: PreferenceStore, native: NativeClient, token: str) -> None:
        """绑定公开 store、原生 native 和会话 token；凭据在原生桥接就绪后加载。"""
        self.store = store  # 公开设置的稳定存储。
        self.native = native  # 当前 Electron 父进程提供的受控能力。
        self.token = token  # 父进程生成并通过标准输入传入的会话随机值。
        self.cookie_name = "harness_desktop"  # Electron 为当前隔离会话设置的 HttpOnly Cookie。
        self.manager: WebRunManager | None = None  # create_app 绑定统一任务调度器。
        self._model_keys: dict[str, str] = {}  # 按端点绑定分区的内存凭据，未配置的键省略。
        self._settings_write = asyncio.Lock()  # 序列化公开配置和密钥变更，防止凭据绑定到过期定义。
        self._picker_active = False  # 同时只允许一个原生选择请求。

    @property
    def available(self) -> bool:
        """返回桌面模式支持原生目录选择；真正打开时由父进程验证窗口仍存在。"""
        return True

    def api_key(self, config: RunConfig) -> str:
        """返回 config 对应任务的内存凭据；只传 Runner，不写入环境或配置。"""
        definition = config.custom_providers.get(config.provider)
        return self._model_keys.get(definition.credential_slot(config.provider), "") if definition is not None else ""

    async def load_model_keys(self, config: RunConfig) -> None:
        """为已登记的 config 读取当前凭据分区。加密库不可用时允许启动供用户修复。"""
        async def load(slot: str) -> None:
            """加载 slot 的密文对应值到内存。读取失败时保留未配置状态。"""
            try:
                value = await asyncio.wait_for(self.native.request("credential.read", {"provider": slot}), 5)
            except (TimeoutError, ConfigurationError):
                return
            if isinstance(value, str):
                self._model_keys[slot] = value

        await asyncio.gather(*(load(definition.credential_slot(provider)) for provider, definition in config.custom_providers.items()
                               if definition.auth == "bearer"))

    def authorized(self, request: Request) -> bool:
        """验证页面 Cookie 或主进程 Bearer token；其他浏览器不能访问桌面服务。"""
        # 用字节比较同时处理非 ASCII 的无效输入，避免恶意 Cookie 触发 TypeError 而变成500。
        return hmac.compare_digest(request.cookies.get(self.cookie_name, "").encode("utf-8"), self.token.encode("utf-8")) or hmac.compare_digest(request.headers.get("authorization", "").encode("utf-8"), ("Bearer " + self.token).encode("utf-8"))

    def public_settings(self) -> dict:
        """返回桌面能力与公开目录；不返回会话令牌、密钥或 IPC 细节。"""
        return {"downloads_directory": str(self.store.value.downloads_directory), "data_directory": str(self.store.directory), "credential_source": "electron_safe_storage"}

    async def configure(self, settings: DesktopRunSettings, downloads: Path) -> dict:
        """保存 settings 和绝对 downloads，返回公开状态；活动任务继续使用原快照。"""
        if self.manager is None:
            raise ConfigurationError("桌面服务尚未就绪。")
        preferences = DesktopPreferences(downloads_directory=downloads)
        async with self._settings_write:
            config = settings.apply(self.manager.run_config)
            await asyncio.to_thread(self.store.update, config=settings, downloads_directory=preferences.downloads_directory)
            self.manager.run_config = config
        return self.public_settings()

    def _custom_definition(self, provider: ModelProvider, version: str | None) -> CustomProvider:
        """读取 provider 的当前自定义定义并核对 version，过期编辑抛冲突。"""
        definition = self.manager.run_config.custom_providers.get(provider)
        if definition is None:
            raise WebServiceError(404, "model_provider_unknown", "该自定义服务商不存在。")
        if version != definition.version:
            raise WebServiceError(409, "model_provider_changed", "服务商连接已变更，请刷新后重试。")
        return definition

    async def configure_provider(self, provider: ModelProvider | None, version: str | None,
                                 settings: CustomProviderSettings, secret: str | None) -> ModelProvider:
        """保存连接和可选 secret，返回稳定标识。空字符串明确清除，None 保留当前分区。"""
        async with self._settings_write:
            previous = self._custom_definition(provider, version) if provider is not None else None
            provider = provider or f"custom-{uuid4().hex}"
            generation = previous.generation if previous and (previous.base_url, previous.auth, previous.api_key_env) == (settings.base_url, settings.auth, None) else uuid4().hex
            definition = CustomProvider(**settings.model_dump(), generation=generation)
            current = self.manager.run_config
            catalog = {**current.custom_providers, provider: definition}
            if len(catalog) > 16:
                raise WebServiceError(400, "model_provider_limit", "最多登记 16 个自定义服务商。")
            if secret and (settings.auth != "bearer" or not valid_model_key(secret)):
                raise WebServiceError(422, "model_credentials_invalid", "请检查认证方式和 API Key 格式。")
            main = catalog.get(current.provider)
            keep_provider = main is not None and main.enabled
            roles = {role: selection for role, selection in current.role_models.items()
                     if catalog[selection.provider].enabled and selection.requested_model in catalog[selection.provider].model_ids}
            selection = {"provider": current.provider if keep_provider else None,
                         "requested_model": current.requested_model if keep_provider and current.requested_model in main.model_ids else None,
                         "role_models": roles}
            config = RunConfig.model_validate({**current.model_dump(), **selection, "custom_providers": catalog})
            stored = self.store.value.config
            if stored is not None:
                stored = stored.model_copy(update=selection)
            slot = definition.credential_slot(provider)
            old_secret = self._model_keys.get(slot, "")
            change_secret = secret is not None and settings.auth == "bearer"
            if change_secret:
                await self._write_model_key(slot, secret)
            try:
                await asyncio.to_thread(self.store.update, config=stored,
                                        custom_providers={**self.store.value.custom_providers, provider: definition})
            except ConfigurationError:
                # 公开设置写入失败时，撤回该请求已写入的凭据。
                if change_secret:
                    await self._write_model_key(slot, old_secret)
                raise
            if change_secret:
                self._model_keys[slot] = secret
            self.manager.run_config = config
            if previous and previous.connection_digest != definition.connection_digest:
                old_slot = previous.credential_slot(provider)
                self._model_keys.pop(old_slot, None)
                try:
                    await self._write_model_key(old_slot, "")
                except ConfigurationError:
                    pass  # 旧分区已失效。清理失败不会将它绑定到新端点。
            return provider

    async def _write_model_key(self, slot: str, secret: str) -> None:
        """在五秒内加密写入 slot。secret 为空删除密文，失败抛固定配置错误。"""
        try:
            await asyncio.wait_for(self.native.request("credential.write", {"provider": slot, "secret": secret}), 5)
        except TimeoutError:
            raise ConfigurationError("本机密钥存储响应超时。") from None

    async def mcp_credential(self, identifier: str) -> str:
        """读取 identifier 分区的系统加密凭据；返回内存明文，未配置为空，不回传页面。"""
        value = await self.native.request("mcp.credential.read", {"key": identifier})
        if not isinstance(value, str):
            raise ConfigurationError("外部凭据读取失败。")
        return value

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
        integration = re.fullmatch(r"/api/integrations/([0-9a-fA-F-]{36})/checks/([0-9a-fA-F-]{36})/artifacts", url.path)
        if integration is not None and self.manager is not None and not (url.scheme or url.netloc or url.fragment):
            try:
                query = parse_qs(url.query, strict_parsing=True)
                if set(query) != {"path"} or len(query["path"]) != 1:
                    raise ValueError
                value = self.manager.integrations.store.get(str(UUID(integration[1])))
                self.manager.management.check_run(value.get("parent_run_id") or value["run_id"])
                return self.manager.integrations.store.check_artifact(str(UUID(integration[1])), str(UUID(integration[2])), query["path"][0])
            except ValueError:
                raise WebServiceError(400, "invalid_artifact", "检查输出地址无效。") from None
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
            parent_id = UUID(query["parent_run_id"][0]) if "parent_run_id" in query and len(query["parent_run_id"]) == 1 else None
            self.manager.management.check_run(str(parent_id or identifier))  # 子附件也从父会话校验回收站访问。
        except ValueError:
            raise WebServiceError(400, "invalid_artifact", "产物地址无效。") from None
        if match[2] == "patch" and set(query) <= {"parent_run_id"} and all(len(values) == 1 for values in query.values()):
            return await self.manager.project_patch(identifier, parent_id) if parent_id else await self.manager.project_patch(identifier)
        if match[2] != "artifacts" or set(query) not in ({"location", "path"}, {"location", "path", "parent_run_id"}) or any(len(values) != 1 for values in query.values()) or query["location"][0] not in {"workspace", "run"}:
            raise WebServiceError(400, "invalid_artifact", "产物地址无效。")
        return self.manager.artifact_path(identifier, query["location"][0], query["path"][0], parent_id) if parent_id else self.manager.artifact_path(identifier, query["location"][0], query["path"][0])

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
