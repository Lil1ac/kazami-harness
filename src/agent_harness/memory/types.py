"""长期记忆的类型结构和确定性策略；来源、适用范围与证据分别建模。"""

from datetime import UTC, datetime
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
import sys
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from agent_harness.schemas import DataModel


MemoryType = Literal["rule", "decision", "fact", "reference", "experience"]
MemoryPhase = Literal["planning", "execution", "verification"]


def relative_path(value: str, *, glob: bool = False) -> str:
    """校验工作区相对 value；glob=True 允许通配符，返回 POSIX 路径，越界或空值抛 ValueError。"""
    normalized = value.replace("\\", "/")
    if not normalized.strip() or "\x00" in normalized or PureWindowsPath(value).drive or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise ValueError("记忆路径必须是工作区内的相对路径。")
    if glob and any(char in normalized for char in "[]"):
        raise ValueError("适用路径支持 *、**、?，不支持字符集合。")
    if not glob and any(char in normalized for char in "*?[]"):
        raise ValueError("证据和入口须指向具体路径。")
    return normalized.removeprefix("./")


class MemoryScope(DataModel):
    """确定性触发范围；空集合表示不限制该维度，不将路径当作文件证据。"""

    paths: list[str] = Field(default_factory=list, max_length=8, description="相对路径 glob；空列表覆盖整个项目，文件变化不使约定失效。")
    phases: list[MemoryPhase] = Field(default_factory=list, max_length=3, description="允许的工作阶段；空列表不限阶段。")
    platforms: list[Literal["win32", "linux", "darwin"]] = Field(default_factory=list, max_length=3, description="实际宿主平台；空列表不限，不从自然语言猜测环境。")

    @model_validator(mode="after")
    def validate_paths(self):
        """归一化 paths 并去重范围值，返回自身；父级路径和绝对路径抛 ValueError。"""
        self.paths = list(dict.fromkeys(relative_path(value, glob=True) for value in self.paths))
        self.phases = list(dict.fromkeys(self.phases))
        self.platforms = list(dict.fromkeys(self.platforms))
        return self


class RuleDetails(DataModel):
    """用户已确认的行为约定；不授予权限，也不覆盖当前要求或 AGENTS.md。"""

    topic: str = Field(min_length=1, max_length=160, description="稳定约定主题，用于同范围冲突审阅。")
    behavior: str = Field(min_length=1, max_length=4000, description="触发时应采用的具体行为。")
    reason: str = Field(default="", max_length=2000, description="约定理由；空字符串表示未提供。")
    exceptions: str = Field(default="", max_length=2000, description="明确的例外；空字符串表示未声明例外。")


class DecisionDetails(DataModel):
    """项目方案选择及原因；代码修改不会自动撤销该决定。"""

    topic: str = Field(min_length=1, max_length=160, description="稳定决策主题；同主题重叠范围的新选择需要审阅。")
    choice: str = Field(min_length=1, max_length=4000, description="已选择的具体方案。")
    rationale: str = Field(min_length=1, max_length=2000, description="选择理由，避免只保存没有上下文的结论。")
    revisit_when: str = Field(default="", max_length=2000, description="重新评估条件，仅供判断，不自动推翻决策。")


class FactDetails(DataModel):
    """有来源的知识断言；确认不等于永远正确，文件证据变化后须复核。"""

    subject: str = Field(min_length=1, max_length=160, description="事实描述的对象。")
    attribute: str = Field(min_length=1, max_length=160, description="对象属性，与 subject 共同组成冲突键。")
    value: str = Field(min_length=1, max_length=4000, description="观察或明确告知的属性值，不保存推测。")
    observed_at: str | None = Field(default=None, description="带时区 ISO 观察时间；None 表示未明确提供，不冒充保存时间。")

    @model_validator(mode="after")
    def validate_time(self):
        """核验 observed_at 的时区，返回自身；不合法时间抛 ValueError。"""
        if self.observed_at is not None and datetime.fromisoformat(self.observed_at).tzinfo is None:
            raise ValueError("事实观察时间必须带时区。")
        return self


class ReferenceDetails(DataModel):
    """信息查阅入口；正文按现有工具权限读取，不在保存或列表读取时联网。"""

    target: str = Field(min_length=1, max_length=2000, description="工作区相对路径或 http/https URL；不是文件证据。")
    purpose: str = Field(min_length=1, max_length=2000, description="入口能解决的问题。")
    consult_when: str = Field(default="", max_length=2000, description="需要查阅的情境；空值表示按用途判断。")

    @model_validator(mode="after")
    def validate_target(self):
        """验证 target；URL 禁止凭据，其他入口须为安全相对路径，返回自身。"""
        if "://" in self.target:
            url = urlsplit(self.target)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("信息入口须为无凭据的 HTTP/HTTPS 地址。")
        else:
            self.target = relative_path(self.target)
        return self


class ExperienceDetails(DataModel):
    """实际发生的历史案例；成功和失败均可保存，不把单次结果推广为通用规则。"""

    situation: str = Field(min_length=1, max_length=2000, description="当时的具体情境及关键条件。")
    action: str = Field(min_length=1, max_length=2000, description="实际采取的行动，不能只写泛泛建议。")
    outcome: str = Field(min_length=1, max_length=2000, description="已观察的实际结果，包含失败时如实记录。")
    limitations: str = Field(min_length=1, max_length=2000, description="适用限制或尚未验证的边界，不代表当前验收通过。")


DETAIL_MODELS = {"rule": RuleDetails, "decision": DecisionDetails, "fact": FactDetails,
                 "reference": ReferenceDetails, "experience": ExperienceDetails}  # 唯一的类型结构注册表。


class MemoryContent(DataModel):
    """公共元数据和专属 details；content 是补充笔记，知识主值只在 details 中保存。"""

    title: str = Field(min_length=1, max_length=160, description="简短主题。")
    type: MemoryType = Field(description="决定内容校验、触发、呈现及复核方式，不决定权限。")
    details: RuleDetails | DecisionDetails | FactDetails | ReferenceDetails | ExperienceDetails = Field(description="必须匹配 type 的结构化知识。")
    content: str = Field(default="", max_length=8192, description="可选补充说明；reference 自动注入时不展开此字段。")
    origin: Literal["user_correction", "user_confirmation", "tool_observation", "run_experience"] = Field(default="user_confirmation", description="知识形成方式，与类型和宿主核验来源分开，不代表权限或置信度。")
    tags: list[str] = Field(default_factory=list, max_length=12, description="搜索关键词，单项最多64字符，不作为适用条件。")
    applies_to: MemoryScope = Field(default_factory=MemoryScope, description="确定性适用范围，空对象表示整个项目。")
    evidence: list[str] = Field(default_factory=list, max_length=8, description="保存时捕获指纹的相对证据文件；仅 fact 的证据变化阻止当前事实使用。")
    branch: str | None = Field(default=None, min_length=1, max_length=200, description="None 不限实际工作树分支。")
    commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40,64}$", description="None 不限实际 HEAD；非空须完整匹配。")
    expires_at: str | None = Field(default=None, description="带时区 ISO 到期时间；None 不按时间失效，不按类型预置 TTL。")

    @model_validator(mode="after")
    def validate_content(self):
        """核验 type/details、文本和字节限额，返回自身；凭据、空必填字段和非法范围抛 ValueError。"""
        if not isinstance(self.details, DETAIL_MODELS[self.type]):
            raise ValueError("记忆类型与结构化内容不匹配。")
        values = self.details.model_dump()
        if not self.title.strip() or any(isinstance(value, str) and not value.strip() for key, value in values.items() if type(self.details).model_fields[key].is_required()):
            raise ValueError("记忆标题和类型必填内容不能为空白。")
        if any(not value.strip() or len(value) > 64 for value in self.tags):
            raise ValueError("记忆关键词须为1至64字符。")
        self.evidence = list(dict.fromkeys(relative_path(value) for value in self.evidence))
        if self.expires_at is not None and datetime.fromisoformat(self.expires_at).tzinfo is None:
            raise ValueError("记忆到期时间必须带时区。")
        raw = json.dumps(self.model_dump(mode="json", include=set(MemoryContent.model_fields)), ensure_ascii=False)
        if len(raw.encode("utf-8")) > 8192:
            raise ValueError("一条记忆的内容与条件最多8 KiB。")
        if re.search(r"-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}|(?:api[_-]?key|password|密码)\s*[:=]\s*\S{8,}", raw, re.I):
            raise ValueError("记忆不能保存凭据正文。")
        return self


def path_matches(path: str, pattern: str) -> bool:
    """按完整相对 path 匹配 glob pattern；支持 ** 跨目录和零层目录，不让 * 跨目录。"""
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", pattern)
    expression = "".join({"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part)) for part in parts)
    return re.fullmatch(expression, path, flags=re.IGNORECASE if sys.platform == "win32" else 0) is not None


def scope_reason(item: dict, phase: str | None = None, paths: list[str] | None = None) -> str | None:
    """检查 item 的平台/阶段/路径；None 参数不检查该维度，空 paths 表示尚无实际路径。"""
    scope = item["applies_to"]
    if scope["platforms"] and sys.platform not in scope["platforms"]:
        return "平台不匹配"
    if phase is not None and scope["phases"] and phase not in scope["phases"]:
        return "工作阶段不匹配"
    if paths is not None and scope["paths"] and not any(path_matches(path, pattern) for path in paths for pattern in scope["paths"]):
        return "尚未触及适用路径"
    return None


def conflict_key(item: dict) -> tuple | None:
    """返回 item 可确定比较的主题键；reference/experience 无唯一主张键，返回 None。"""
    details = item["details"]
    values = [details["subject"], details["attribute"]] if item["type"] == "fact" else [details["topic"]] if item["type"] in {"rule", "decision"} else None
    return (item["type"], *(value.strip().casefold() for value in values)) if values else None


def scopes_overlap(left: dict, right: dict) -> bool:
    """保守判断两条记忆范围是否可能重叠；无法证明互斥时返回 True，只要求人工审阅。"""
    if any(item["expires_at"] and datetime.fromisoformat(item["expires_at"]) <= datetime.now(UTC) for item in (left, right)):
        return False
    for key in ("branch", "commit"):
        if left[key] and right[key] and left[key] != right[key]:
            return False
    for key in ("phases", "platforms"):
        a, b = left["applies_to"][key], right["applies_to"][key]
        if a and b and not set(a).intersection(b):
            return False
    a, b = left["applies_to"]["paths"], right["applies_to"]["paths"]
    if a and b:
        # 只以首个确定目录证明互斥，复杂 glob 不做不可靠的自动排除。
        roots_a = {value.split("/")[0].casefold() for value in a}
        roots_b = {value.split("/")[0].casefold() for value in b}
        if all(not re.search(r"[*?\[]", value) for value in roots_a | roots_b) and roots_a.isdisjoint(roots_b):
            return False
    return True


def conflicts(item: dict, others: list[dict]) -> list[dict]:
    """查找 item 与 others 中已生效的同键不同主张；返回可审阅摘要，不据语义相似自动覆盖。"""
    key = conflict_key(item)
    primary = {"rule": "behavior", "decision": "choice", "fact": "value"}.get(item["type"])
    if key is None:
        return []
    return [{"id": other["id"], "revision": other["revision"], "title": other["title"], "type": other["type"], "details": other["details"], "applies_to": other["applies_to"], "branch": other["branch"], "commit": other["commit"], "expires_at": other["expires_at"]}
            for other in others if other["status"] == "active" and other["id"] != item.get("id")
            and conflict_key(other) == key and scopes_overlap(item, other)
            and other["details"][primary].strip() != item["details"][primary].strip()]


def render_memory(item: dict) -> dict:
    """将 item 转为模型实际接收的类型视图；reference 只含入口，不带补充正文或文件内容。"""
    modes = {"rule": "条件与行为约定", "decision": "方案选择与理由", "fact": "有来源的事实", "reference": "按需查阅入口", "experience": "历史案例，不代表当前验收"}
    view = dict(item)
    view["presentation"] = modes[item["type"]]
    view["match_reasons"] = item.get("match_reasons", [])
    details = item["details"]
    if item["type"] == "rule":
        text = f"适用时遵循此行为约定：{details['behavior']}\n理由：{details['reason'] or '未提供'}\n例外：{details['exceptions'] or '未声明'}"
    elif item["type"] == "decision":
        text = f"已确认方案：{details['choice']}\n选择理由：{details['rationale']}\n重评条件：{details['revisit_when'] or '未声明'}"
    elif item["type"] == "fact":
        text = f"有来源的事实：{details['subject']} / {details['attribute']} = {details['value']}\n观察时间：{details['observed_at'] or '未明确提供'}"
    elif item["type"] == "reference":
        text = f"按需查阅：{details['target']}\n用途：{details['purpose']}\n查阅时机：{details['consult_when'] or '按用途判断'}；入口内容尚未读取。"
    else:
        text = f"历史情境：{details['situation']}\n当时行动：{details['action']}\n已观察结果：{details['outcome']}\n适用限制：{details['limitations']}\n这是历史案例，不代表当前验收通过。"
    view["usage_text"] = text + ("\n补充说明：" + item["content"] if item["content"] and item["type"] != "reference" else "")
    if item["type"] == "reference":
        view.update(content="", evidence=[], evidence_refs=[])
    return view
