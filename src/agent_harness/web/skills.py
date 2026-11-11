"""技能管理的同源接口，页面只能上传显式选择的有界文本。"""

from typing import Literal

from fastapi import FastAPI, Request
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from agent_harness.context.skill_library import MAX_PACKAGE_BYTES, MAX_PACKAGE_FILES, SkillLibrary
from agent_harness.context.skills import SkillError
from agent_harness.schemas import DataModel, SkillOption, SkillResourceSnapshot, SkillSnapshot


class ImportFile(DataModel):
    """用户选择的单个 UTF-8 文本文件。"""

    path: str = Field(min_length=1, max_length=240)  # 相对技能目录的包内路径。
    content: str = Field(max_length=64 * 1024)  # 完整文本，目录层额外按 UTF-8 字节核验。


class ImportSkill(DataModel):
    """导入或显式替换一个本地技能副本。"""

    name: str = Field(min_length=1, max_length=64)  # 用户选择的目录名，必须与 frontmatter 一致。
    files: list[ImportFile] = Field(min_length=1, max_length=MAX_PACKAGE_FILES)  # 完整包内文本列表。
    revision: str | None = Field(default=None, max_length=64)  # None 新增，非空绑定待替换的旧版本。


class ChangeSkill(DataModel):
    """针对管理页当前版本的明确操作。"""

    revision: str = Field(min_length=1, max_length=64)  # 用户看到的管理版本。
    action: Literal["enable", "disable", "remove"]  # 启停或移除受管副本。


def install_skill_routes(app: FastAPI, library: SkillLibrary) -> None:
    """将 library 的管理接口安装到 app，共用既有同源认证及错误处理。"""
    @app.get("/api/skills")
    def list_skills() -> dict[str, list[SkillOption]]:
        """返回技能目录和启停诊断，正文通过单项预览读取。"""
        return {"items": library.discover()}

    @app.get("/api/skills/{identifier}")
    def preview_skill(identifier: str) -> SkillSnapshot:
        """返回 identifier 的完整正文和附属文件目录，不启用技能。"""
        return library.preview(identifier)

    @app.get("/api/skills/{identifier}/resource")
    def preview_resource(identifier: str, path: str) -> SkillResourceSnapshot:
        """只预览 identifier 当前资源清单中的 path，不记录为模型读取。"""
        return library.read_resource(library.preview(identifier), path)

    @app.post("/api/skills/import")
    async def import_skill(request: Request) -> SkillOption:
        """从 request 有界读取 JSON 包，校验成功后原子保存，返回管理目录项。"""
        body = bytearray()
        # JSON 转义可能放大原始文本；先限制传输尺寸，再按解码后的 UTF-8 核验包大小。
        async for chunk in request.stream():
            if len(body) + len(chunk) > 6 * MAX_PACKAGE_BYTES + 64 * 1024:
                raise SkillError("skill_invalid_package", "技能包请求超过传输大小上限。")
            body.extend(chunk)
        try:
            data = ImportSkill.model_validate_json(body)
        except ValidationError:
            raise SkillError("skill_invalid_package", "技能包格式或大小不符合要求。") from None
        return await run_in_threadpool(library.import_package, data.name,
                                       [(file.path, file.content) for file in data.files], data.revision)

    @app.post("/api/skills/{identifier}/actions")
    def change_skill(identifier: str, body: ChangeSkill) -> dict[str, bool]:
        """按 body 的版本对 identifier 执行指定操作并返回确认。"""
        library.change(identifier, body.revision, body.action)
        return {"ok": True}
