"""定义四道使用本机 Click 8.5.0 源码的跨文件修改评测题。"""

from pathlib import Path


# 使用评测运行环境已安装的真实源码，离线预检另行复制，安装目录只用于取证。
WORKSPACE = (
    Path(__file__).resolve().parents[3] / ".venv" / "Lib" / "site-packages" / "click"
)
SOURCE = "Pallets Click 8.5.0（本地安装包，BSD-3-Clause）"
SOURCE_FILES = tuple(path.name for path in sorted(WORKSPACE.glob("*.py")))


def case(
    identifier: str, title: str, early: str, final: str, evidence_file: str, anchor: str
) -> dict:
    """返回供回放读取的单题数据。

    identifier 和 title 分别标识与展示题目。early、final 分别是首轮和收尾的用户要求。
    evidence_file 为 Click 包内的相对路径，anchor 为该文件中唯一的原文片段。
    """
    return {
        "id": identifier,
        "category": "cross_file",
        "title": title,
        "source": SOURCE,
        "early": early,
        "final": final,
        "workspace": WORKSPACE,
        "evidence_file": evidence_file,
        "anchor": anchor,
        "history_files": [name for name in SOURCE_FILES if name != evidence_file],
    }


# 首轮保留最终轮未复述的边界条件，评测可判断旧需求是否仍参与后续实现决策。
CASES = [
    case(
        "cross-click-usage-prefix",
        "命令与格式器之间传递用法前缀",
        "先调查 Click 的用法前缀支持。子命令未设置时继承父设置，显式空字符串则表示不要显示任何前缀。格式器的单次 write_usage 调用若传入前缀，应优先于格式器默认设置，其显式空字符串也有效。请定位格式器的旧默认前缀以及 Context 如何创建格式器，暂时别改。",
        "现在实现刚才讨论的用法前缀。公开 Context 的 usage_prefix 配置，可通过 context_settings 设置；让它作为 HelpFormatter.write_usage 的默认前缀，同时保留既有未配置时的行为。请指出需要改动的源码位置和验证方式。",
        "formatting.py",
        'prefix = "{usage} ".format(usage=_("Usage:"))',
    ),
    case(
        "cross-click-choice-candidates",
        "保留 Choice 错误的结构化候选值",
        "先调查无效 Choice 的错误处理。候选需要是用于错误文案的规范化字符串，保持声明顺序，枚举和大小写忽略规则也要走现有的规范化逻辑。其他 BadParameter 的新字段为 None，既有人类可读文本和上下文不变。请先定位现有错误文本和异常的构建位置，暂时别改。",
        "给无效 Choice 的 BadParameter 增加类型为 tuple[str, ...] | None 的 choice_candidates 字段，供调用方直接消费。按前面约定同时处理转换与异常定义，并保留已有 CLI 的行为。请指出需要改动的源码位置和验证方式。",
        "types.py",
        'choices_str = ", ".join(map(repr, self._normalized_mapping(ctx=ctx).values()))',
    ),
    case(
        "cross-click-exit-message",
        "将退出说明从 Context 传递到命令入口",
        "先梳理 Click 的正常退出机制。退出说明在 standalone 模式下一律输出到 stderr，即使退出码为零；省略说明保持静默，显式空字符串输出一个空行。非 standalone 模式必须仍只返回原退出码且不输出说明。清理回调仍先于返回或退出。请定位退出异常和命令接收方，暂时别改。",
        "把可选、仅能通过关键字设置的 message 接到 Context.exit 和 Exit，让命令入口按前面的口径处理说明。保留普通退出的兼容行为。请指出需要改动的源码位置和验证方式。",
        "exceptions.py",
        '__slots__ = ("exit_code",)',
    ),
    case(
        "cross-click-command-suggestion-limit",
        "为命令拼写提示增加上下文配置",
        "先调查 Click 的不存在命令提示。子命令未配置时继承父设置，0 要完全关闭候选提示且不能进入相似度计算，正整数表示保留原来的排名与相似度规则并改变最大候选数，负数应抛 ValueError。默认依旧最多三个，也要兼容调用方直接构造无上下文异常。请定位底层候选计算，暂时别改。",
        "通过 Context 新增 command_suggestion_limit，可由 context_settings 设置，用来控制 NoSuchCommand 的拼写候选。按前面约定处理子命令和边界情况。请指出需要改动的源码位置和验证方式。",
        "exceptions.py",
        "possibilities_ = get_close_matches(command_name, possibilities)",
    ),
]
