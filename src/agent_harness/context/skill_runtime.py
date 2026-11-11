"""运行内按需加载已登记的技能和资料，持久化实际使用的快照。"""

import json
from typing import TYPE_CHECKING

from pydantic import Field

from agent_harness.context.skill_library import SkillLibrary
from agent_harness.context.skills import SkillError
from agent_harness.schemas import DataModel, SkillResourceSnapshot, SkillSnapshot

if TYPE_CHECKING:
    from agent_harness.schemas import RunEvent


class ActivateSkill(DataModel):
    """按本轮可用目录中的名称启用一个技能。"""

    name: str = Field(min_length=1, max_length=64)  # 本轮目录中展示的技能名。


class ReadSkillResource(ActivateSkill):
    """读取已启用技能中实际登记的资料。"""

    path: str = Field(min_length=1, max_length=240)  # 技能目录内 references/ 或 assets/ 的相对路径。


SKILL_TOOLS = {
    "activate_skill": (ActivateSkill, "按名称启用本轮目录中与任务相关的技能。成功后宿主把 SKILL.md 正文加入下一次输入。"),
    "read_skill_resource": (ReadSkillResource, "读取已启用技能声明的资料。使用资源相对路径。成功后宿主把资料正文加入下一次输入。"),
}  # 宿主控制工具，实际调用仍受只读策略和工具预算约束。


def recorded_skills(events: list["RunEvent"]) -> tuple[list[SkillSnapshot], list[SkillResourceSnapshot]]:
    """按 events 的持久顺序还原正文及资料，历史无快照时返回空集合。"""
    skills, resources = [], []
    for event in events:
        if event.type == "context_prepared":
            skills = [SkillSnapshot.model_validate(item) for item in event.payload.get("skills", [])]
            resources = [SkillResourceSnapshot.model_validate(item) for item in event.payload.get("skill_resources", [])]
        elif event.type == "skill_activated":
            skills.append(SkillSnapshot.model_validate(event.payload))
        elif event.type == "skill_resource_read":
            resources.append(SkillResourceSnapshot.model_validate(event.payload))
    return skills, resources


def skill_text(skill: SkillSnapshot) -> str:
    """将 skill 正文标为流程指导并列出可读资料名，返回模型使用的文本。"""
    text = f"以下是本轮{'手动选择' if skill.activation == 'manual' else '自动启用'}的技能 {skill.name}（流程指导）：\n<selected_skill>\n{skill.content}\n</selected_skill>"
    if skill.resources:
        text += "\n可通过 read_skill_resource 读取：" + json.dumps(list(skill.resources), ensure_ascii=False)
    return text


class SkillRuntime:
    """维护交互主运行的可见目录、已启用技能与已读资源。"""

    MAX_CATALOG_BYTES = 8 * 1024  # 仅目录名称和描述的初始 UTF-8 上限。
    MAX_RESOURCE_BYTES = 128 * 1024  # 一轮保留到模型上下文的资料正文总量。
    MAX_READ_RESOURCES = 16  # 限制空文本等资料也会占用的消息包装开销。

    def __init__(self, library: SkillLibrary, skills: list[SkillSnapshot], resources: list[SkillResourceSnapshot], *, automatic: bool, emit) -> None:
        """绑定 library、已有快照与持久 emit。automatic 为 True 时仅发现名称及描述。"""
        self.library = library  # 使用本地宿主版本校验的目录。
        self.skills = skills  # 初始及本轮动态加入的正文。
        self.resources = resources  # 已读资料的固定正文，重复调用直接复用。
        self.emit = emit  # 先记录事实再让后续模型输入引用。
        self.catalog = {}  # 可自动激活名称到初始目录项的映射。
        self.catalog_limited = False  # 目录预算达到时提示模型仅展示部分选项。
        self.initial_count = len(skills)  # 已写入固定前缀的快照个数。
        if automatic:
            remaining = self.MAX_CATALOG_BYTES - 2
            for item in library.discover():
                if item.available and not item.manual_only and not any(skill.name == item.name for skill in skills):
                    cost = len(json.dumps({"name": item.name, "description": item.description}, ensure_ascii=False).encode("utf-8")) + 2
                    if cost > remaining:
                        self.catalog_limited = True
                        continue
                    remaining -= cost
                    self.catalog[item.name] = item

    @property
    def catalog_text(self) -> str:
        """返回目录的模型文本，空目录返回空字符串。"""
        if not self.catalog:
            return ""
        payload = json.dumps([{"name": item.name, "description": item.description} for item in self.catalog.values()], ensure_ascii=False)
        return ("以下是按需可启用的技能目录，描述仅用于匹配。任务明显匹配时先调用 activate_skill。\n"
                + ("仅展示预算内的目录项。\n" if self.catalog_limited else "") + payload)

    @property
    def names(self) -> list[str]:
        """列出固定的工具入口，保留已启用技能的重复调用及资料读取能力。"""
        names = ["activate_skill"] if self.catalog else []
        if any(skill.resources for skill in self.skills) or self.catalog:
            names.append("read_skill_resource")
        return names

    def projection(self) -> str:
        """返回运行中新增的正文和已读资料，宿主将其加入受压缩保护的固定前缀。"""
        parts = [skill_text(skill) for skill in self.skills[self.initial_count:]]
        parts.extend(f"技能 {item.name} 资料 {item.path}（流程参考）：\n<skill_resource>\n{item.content}\n</skill_resource>"
                     for item in self.resources)
        return "\n\n".join(parts)

    def activate(self, name: str) -> dict:
        """加载 name 对应的冻结目录版本，返回摘要。重复启用不重新读取或记事件。"""
        skill = next((item for item in self.skills if item.name == name), None)
        if skill is None:
            option = self.catalog.get(name)
            if option is None:
                raise SkillError("skill_unavailable", "该技能未列入本轮自动目录。", name)
            if len(self.skills) >= self.library.host.MAX_SELECTED:
                raise SkillError("skill_selection_limit", "本轮技能数量已达到上限。", name)
            skill = self.library.load([name], resources=True, expected=option.revision)[0]
            if skill.size_bytes + sum(item.size_bytes for item in self.skills) > self.library.host.MAX_TOTAL_BYTES:
                raise SkillError("skill_total_too_large", "本轮技能正文合计超过字节上限。", name)
            skill.activation = "automatic"
            self.emit("skill_activated", skill.model_dump(mode="json"))
            self.skills.append(skill)
        return {"name": skill.name, "activation": skill.activation, "version": skill.version, "resources": list(skill.resources)}

    def read(self, name: str, path: str) -> dict:
        """按 name 和 path 读取并固定已启用资料，返回正文指纹和大小。"""
        skill = next((item for item in self.skills if item.name == name), None)
        if skill is None:
            raise SkillError("skill_resource_denied", "请先启用对应技能。", name)
        record = next((item for item in self.resources if item.name == name and item.path == path), None)
        if record is None:
            if len(self.resources) >= self.MAX_READ_RESOURCES:
                raise SkillError("skill_resource_limit", "本轮技能资料数量已达到上限。", name)
            record = self.library.read_resource(skill, path)
            if record.size_bytes + sum(item.size_bytes for item in self.resources) > self.MAX_RESOURCE_BYTES:
                raise SkillError("skill_resource_limit", "本轮技能资料合计超过字节上限。", name)
            self.emit("skill_resource_read", record.model_dump(mode="json"))
            self.resources.append(record)
        return {"name": name, "path": path, "sha256": record.sha256, "size_bytes": record.size_bytes}
