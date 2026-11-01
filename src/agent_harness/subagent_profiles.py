"""只读探索与审阅的独立角色定义，不复制父Agent的系统职责或隐式继承技能。"""

from dataclasses import dataclass
from types import MappingProxyType


# 宿主可授予的只读工具全集；最终仍与父级已开放能力及角色集合取交集。
READONLY_TOOLS = frozenset({"read_file", "search_files", "list_files", "read_context", "memory_search", "memory_get"})
REPORT_INSTRUCTIONS = """你是只读子Agent，只有本次研究任务是你的执行目标。父任务和批准方案属于背景，不要求你执行父级修改。
不得写文件、执行命令、修改任务或记忆、提问用户或继续委派；仅使用本轮实际声明的工具。
只检查委派范围；把必要修复与可选建议分开，可选建议不授予父Agent扩大范围的权限。
结论保持简短，不重复完整文件、目录清单和工具输出。若项目约定已在输入提供，不为确认相同内容重复读取。
required仅用于有依据且完成父目标必须修复的缺陷；普通事实、接口取值和检查范围归observation，可选加强归optional。
最终只返回JSON对象，字段全部提供：
{"summary":"简短结论","findings":[{"category":"required或optional或observation","title":"结论","detail":"影响和依据","evidence_call_ids":["实际成功读取回执的tool_call_id"]}],"limitations":[],"pending":[],"complete":true}
summary最多1200字符，findings最多8项，每项detail最多600字符、证据最多2个；limitations/pending各最多8项，每项最多240字符。
required必须引用实际成功读取的回执；不得编造编号、行号、验收或授权。未发现缺陷可以返回空findings，但须说明检查范围。
没有完成指定研究时complete=false，并列出已知限制与未完成事项；报告返回不等于父任务完成或项目检查通过。"""  # 两角色共用的交接协议，不含父角色提示。


@dataclass(frozen=True)
class SubagentProfile:
    """宿主内置的不可变角色；模型只能选择名称，不能自行提升权限或覆写提示。"""

    role: str  # explore/review稳定角色名。
    system_prompt: str  # 角色专属职责与统一报告格式，不包含父系统提示。
    tools: frozenset[str]  # 该角色的权限上限，实际能力还须属于父级集合。


PROFILES = MappingProxyType({
    "explore": SubagentProfile("explore", "探索职责：围绕指定问题定位文件、入口与调用关系，返回可直接使用的依据，不提出无关重构。\n" + REPORT_INSTRUCTIONS, READONLY_TOOLS),
    "review": SubagentProfile("review", "审阅职责：只识别委派范围内有实际依据的缺陷或回归，解释影响；可选改进单列，不补充父任务未要求的需求。\n" + REPORT_INSTRUCTIONS, READONLY_TOOLS),
})  # 角色统一从此处解析；当前模型和供应商继续继承父运行，不新增路由。
