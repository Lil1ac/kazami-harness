"""宿主认可的模型供应商、默认模型和凭据来源。"""

from hashlib import sha256
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

ModelProvider = Annotated[str, StringConstraints(pattern=r"^custom-[a-z0-9][a-z0-9-]{0,31}$")]  # 用户登记的稳定服务商标识。
ModelId = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f])?$")]  # 发送给服务商的模型 ID。
ModelVersion = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]  # 自定义端点版本，不含地址或凭据正文。
ModelRole = Literal["explore", "review"]  # 首版允许独立选择模型的委派角色。

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})  # 允许明文 HTTP 和免密请求的本机地址。


class CustomProviderSettings(BaseModel):
    """用户可编辑的兼容接口定义，不包含认证密钥或内部版本。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)  # 定义只通过整体替换更新。
    name: str = Field(min_length=1, max_length=80)  # 页面显示名称，可与其他连接同名。
    base_url: str = Field(min_length=1, max_length=2048)  # 兼容接口根地址，末尾可包含 /v1 或网关前缀。
    default_model: ModelId  # 未明确选择时提交给服务商的模型 ID。
    models: list[ModelId] = Field(default_factory=list, max_length=31)  # 同连接可选的其他模型，不重复默认模型。
    auth: Literal["bearer", "none"] = "bearer"  # none 只适用于回环地址。
    enabled: bool = True  # 停用后不能接纳或继续使用该版本的任务。

    @field_validator("name", "default_model", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> object:
        """去掉 value 的外侧空白并返回，拒绝空值及内嵌控制字符。"""
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value or re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("名称和模型 ID 必须为单行文本")
        return value

    @field_validator("base_url")
    @classmethod
    def normalize_address(cls, value: str) -> str:
        """将 value 规范为固定服务根地址。拒绝凭据、查询、片段、转义歧义和远程明文。"""
        value = value.strip()
        try:
            if re.search(r"[\s\\?#\x00-\x1f\x7f]", value) or "%" in urlsplit(value).netloc:
                raise ValueError
            parts = urlsplit(value)
            url = httpx.URL(value)
            if parts.username is not None or parts.password is not None or not parts.hostname or not url.host or parts.port == 0:
                raise ValueError
            if url.scheme != "https" and not (url.scheme == "http" and url.host in LOOPBACK_HOSTS):
                raise ValueError
            if url.path.rstrip("/").endswith("/chat/completions"):
                raise ValueError
            if url.scheme == "http" and url.host == "localhost":
                url = url.copy_with(host="127.0.0.1")  # 将明文 localhost 固定为回环地址，不依赖 DNS。
            return str(url).rstrip("/")
        except (ValueError, httpx.InvalidURL):
            raise ValueError("请填写 HTTPS 服务根地址，或本机回环 HTTP 地址") from None

    @model_validator(mode="after")
    def check_local_auth(self) -> "CustomProviderSettings":
        """核对免密地址与模型去重规则，返回定义本身。"""
        if self.auth == "none" and httpx.URL(self.base_url).host not in LOOPBACK_HOSTS:
            raise ValueError("免密接入仅限本机回环地址")
        if len(set(self.model_ids)) != len(self.model_ids):
            raise ValueError("同一服务商的模型 ID 不能重复")
        return self

    @property
    def model_ids(self) -> list[str]:
        """按默认模型在前的顺序返回全部登记的模型 ID。"""
        return [self.default_model, *self.models]


class CustomProvider(CustomProviderSettings):
    """带版本和凭据引用的宿主定义，可作为运行快照持久化。"""

    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")  # TOML 部署的环境变量，桌面留空。
    generation: str = Field(default="", max_length=32, pattern=r"^[0-9a-f]*$")  # 桌面在地址或认证变更时生成。宿主可显式更新。

    @model_validator(mode="after")
    def check_auth(self) -> "CustomProvider":
        """校验无密钥模式与回环地址、环境变量之间的约束，返回定义本身。"""
        if self.auth == "none" and self.api_key_env is not None:
            raise ValueError("免密连接不能指定密钥环境变量")
        return self

    @property
    def endpoint(self) -> str:
        """返回在固定 Base URL 下的 Chat Completions 路径。"""
        return self.base_url + "/chat/completions"

    @property
    def version(self) -> str:
        """返回地址与认证绑定的摘要，供队列和运行核对。显示名称和默认模型不改变绑定。"""
        return sha256(f"{self.connection_digest}:{self.enabled}".encode("ascii")).hexdigest()

    @property
    def connection_digest(self) -> str:
        """返回地址和认证专属摘要，停用或恢复同一个端点时继续使用该密钥分区。"""
        binding = [self.base_url, self.auth, self.api_key_env, self.generation]
        return sha256(json.dumps(binding).encode("utf-8")).hexdigest()

    def credential_slot(self, provider: str) -> str:
        """返回 provider 当前连接版本专属的密文标识，新端点无法读到旧端点的密钥。"""
        return f"{provider}-{self.connection_digest}"


def valid_model_key(value: str) -> bool:
    """判断 value 是否为非空可打印 ASCII 且无空白，返回布尔值，不记录凭据。"""
    return isinstance(value, str) and bool(value) and all(33 <= ord(char) <= 126 for char in value)
