"""四道取材于本机 CPython 标准库源码的多轮需求变更题。"""

from pathlib import Path


# 只读使用冻结的发行版源码。其他 Python 解释器也应评测这份原件。
CPYTHON_LIB = Path("D:/anaconda3/Lib")

# 本机 Anaconda 安装提供的版本。题面为本次新拟需求，不是上游 issue。
SOURCE = "CPython 3.11.5 标准库（本机 Anaconda 发行版）"

# 首轮和末轮共同构成任务；证据锚点独立保存在实际源码中。
CASES = [
    {
        "id": "multiturn-textwrap-indent",
        "category": "multiturn",
        "title": "为选中的首行设置特殊前缀，并支持按段重新应用",
        "source": SOURCE,
        "workspace": CPYTHON_LIB,
        "evidence_file": "textwrap.py",
        "anchor": "for line in text.splitlines(True):",
        "early": (
            "我们在工作区副本里用 textwrap.indent 给命令行帮助做标记。"
            "拟为 indent 增加仅限关键字的 first_prefix=None。提供该值时，"
            "第一条通过 predicate 的行使用它，其余通过 predicate 的行仍使用原 prefix。"
            "空字符串是有效的首行前缀，None 完全保留原行为。"
            "保持现有 predicate 的输入和默认规则，原始换行符和未选中行必须原样保留。"
            "先阅读源码、核对行拆分和回调契约，本轮先不要改文件。"
        ),
        "final": (
            "产品改成了多段帮助。保留首轮接口和兼容约定，再为 indent 加入仅限关键字的 "
            "restart_on_blank=False。仅当首行前缀已设置且该开关开启时，连续空白行将段落隔开，"
            "每段的第一条被 predicate 选中的非空白行使用首行前缀。"
            "自定义 predicate 即使选中了空白行，也只给它加普通前缀，不消耗下一段的首行前缀。"
            "请在工作区副本实现并验证。"
        ),
        "history_files": [
            "argparse.py",
            "pydoc.py",
            "pprint.py",
            "string.py",
            "shlex.py",
            "difflib.py",
            "csv.py",
            "codecs.py",
        ],
    },
    {
        "id": "multiturn-fnmatch-filter",
        "category": "multiturn",
        "title": "为文件名筛选添加排除模式和保留例外",
        "source": SOURCE,
        "workspace": CPYTHON_LIB,
        "evidence_file": "fnmatch.py",
        "anchor": "# normcase on posix is NOP. Optimize it away from the loop.",
        "early": (
            "我们用 fnmatch.filter 从清单里选文件名。希望增加仅限关键字的 exclude=()，"
            "使命中主模式且未命中任何排除模式的名字保留下来。"
            "主模式、排除模式和输入名沿用模块当前的平台规范化规则，支持 str 组或 bytes 组。"
            "模式集合和输入名可以来自一次性迭代器，结果保留输入顺序及重复项。"
            "先核对现有实现怎样编译模式和处理平台差异，本轮先不要改文件。"
        ),
        "final": (
            "现在排除规则还需要少量例外。请保留首轮契约并加上仅限关键字的 keep=()。"
            "命中主模式的名字，若命中排除模式，可以再由任一 keep 模式恢复；"
            "没有命中主模式的名字永远不进入结果。keep 与 exclude 使用相同的"
            "平台规范化、单次消费和类型规则。直接在工作区副本实现并验证。"
        ),
        "history_files": [
            "glob.py",
            "pathlib.py",
            "ntpath.py",
            "posixpath.py",
            "os.py",
            "filecmp.py",
            "shutil.py",
            "re/__init__.py",
            "re/_parser.py",
            "re/_compiler.py",
            "functools.py",
            "difflib.py",
        ],
    },
    {
        "id": "multiturn-heapq-merge",
        "category": "multiturn",
        "title": "合并已排序流时按键去重并允许保留最后一项",
        "source": SOURCE,
        "workspace": CPYTHON_LIB,
        "evidence_file": "heapq.py",
        "anchor": "Similar to sorted(itertools.chain(*iterables)) but returns a generator,",
        "early": (
            "我们把若干已排序的记录流交给 heapq.merge，希望增加仅限关键字的 unique=False。"
            "开启后把合并结果中相邻的相同排序键视为一组，只输出每组第一条。"
            "未传 key 时比较原值；已传 key 时比较 key 的返回值，"
            "每个输入元素最多调用一次 key。保持已有顺序、reverse 规则与生成器的惰性，"
            "不得把输入提前读完。先阅读源码确认单流尾段和相等键的行为，本轮先不要改文件。"
        ),
        "final": (
            "新接入的一路数据约定同键组的最后一条是最新记录。保留首轮兼容性，增加仅限关键字的 "
            "keep_last=False。unique=True 且 keep_last=True 时，按现有合并顺序保留每组的最后一条；"
            "unique=False 时 keep_last 对结果没有影响。仍需惰性处理，不缓存整组或整批记录，"
            "最后一条输入流的尾段也要正确去重。直接在工作区副本实现并验证。"
        ),
        "history_files": [
            "bisect.py",
            "functools.py",
            "operator.py",
            "collections/__init__.py",
            "statistics.py",
            "queue.py",
            "graphlib.py",
            "sched.py",
            "random.py",
            "enum.py",
            "inspect.py",
        ],
    },
    {
        "id": "multiturn-configparser-items",
        "category": "multiturn",
        "title": "列出本节配置项并可选择包含临时覆盖键",
        "source": SOURCE,
        "workspace": CPYTHON_LIB,
        "evidence_file": "configparser.py",
        "anchor": "orig_keys = list(d.keys())",
        "early": (
            "导出 INI 配置时，我们需要 RawConfigParser.items(section, ...) 能通过仅限关键字的 "
            "include_defaults=False 只枚举本节直接定义的键，按本节原有次序。"
            "默认仍为 True，保持原行为。DEFAULT 的键仍可作为本节选项的插值来源，"
            "vars 仍可覆盖参与求值的键，但默认不额外枚举 vars 中的键。"
            "不传 section 时仍返回原有映射视图；显式列 DEFAULT 时仍把其自身键视为直接定义。"
            "先阅读实际实现，确认原有键顺序和 vars 的参与方式，本轮先不要改文件。"
        ),
        "final": (
            "临时覆盖配置也需要参与一次展示。请继续遵守首轮契约，再加仅限关键字的 "
            "include_vars=False。显式传入命名节（含 DEFAULT）且开关为 True 时，"
            "把 vars 中额外键按传入顺序追加到已选择的键之后，键使用现有 optionxform 规范化；"
            "重复的规范化键只枚举一次，沿用 vars 的最后一次覆盖值。"
            "raw=True 仍不插值；不传 section 时仍返回映射视图。直接在工作区副本实现并验证。"
        ),
        "history_files": [
            "argparse.py",
            "tomllib/_parser.py",
            "csv.py",
            "shlex.py",
            "string.py",
            "os.py",
            "pathlib.py",
            "collections/__init__.py",
            "textwrap.py",
            "dataclasses.py",
        ],
    },
]
