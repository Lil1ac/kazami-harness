"""固定来自 Jinja 与 Marshmallow 的新维护任务和独立行为断言。"""

from pathlib import Path


SOURCES = Path(__file__).resolve().parents[1] / "memory-repository-20261117" / "sources"
"""既有公开源码快照根目录。各题只读取它对应的发布版文本。"""

MODULES = {
    "jinja": ("jinja2.filters", SOURCES / "reporting/src/jinja2/filters.py"),
    "marshmallow": ("marshmallow.fields", SOURCES / "importer/src/marshmallow/fields.py"),
}
"""评测标签到 Python 模块名和固定源文件的映射。"""


def _case(
    project: str,
    identifier: str,
    title: str,
    symbol: str,
    first_marker: str,
    next_marker: str,
    setup: str,
    prompt: str,
    edits: list[tuple[str, str]],
    check: str,
) -> dict[str, str]:
    """从两个模块级标志间取得完整目标实现，逐项应用冻结的参考替换。

    project 是 MODULES 中的快照标签，identifier 和 title 分别是稳定题号及人读标题。
    symbol 指定位源码的函数或类成员。first_marker 和 next_marker 分别是源码起止边界，
    后者不进入修改。setup 是检查点前的约定，prompt 是后续维护要求。edits 依顺序替换
    唯一旧片段，check 是引用候选模块 m 的独立 Python 断言。返回冻结题目字典。
    任一片段歧义时抛出 ValueError，以便预检阻止过期参考补丁。
    """
    module, path = MODULES[project]
    text = path.read_text(encoding="utf-8")
    start = text.index(first_marker)
    end = text.index(next_marker, start + len(first_marker))
    old = text[start:end]
    if text.count(old) != 1:
        raise ValueError(f"{identifier} 的源码边界不唯一")
    new = old
    for before, after in edits:
        if new.count(before) != 1:
            raise ValueError(f"{identifier} 的参考替换片段不唯一")
        new = new.replace(before, after, 1)
    return {
        "project": project, "id": identifier, "title": title, "module": module,
        "symbol": symbol, "setup": setup, "prompt": prompt,
        "edit_old": old, "edit_new": new, "check": check.strip(),
    }


# 每仓前两题输入较低，随后两题接近上限，最后两题用于超限压力。
CASES = (
    _case(
        "jinja", "jinja-dictsort-option-case", "字典排序配置兼容大小写选项",
        "do_dictsort", "def do_dictsort(", "\n\n@pass_environment\ndef do_sort(",
        "旧模板依赖键排序和反向值排序。管理端下发的排序字段是 ASCII 选项名，可能使用全大写或混合大小写。",
        "让 dictsort 的 by 参数无视选项名大小写，继续接受 key 和 value。"
        "不改键值本身的比较规则、反向参数和非法选项的异常类型。",
        [('    if by == "key":', '    if isinstance(by, str):\n        by = by.lower()\n    if by == "key":')],
        """data = {"Zulu": 1, "alpha": 3, "Gamma": 2}
assert m.do_dictsort(data, by="KeY") == [("alpha", 3), ("Gamma", 2), ("Zulu", 1)]
assert m.do_dictsort(data, by="VALUE", reverse=True) == [("alpha", 3), ("Gamma", 2), ("Zulu", 1)]
assert m.do_dictsort(data, case_sensitive=True) == [("Gamma", 2), ("Zulu", 1), ("alpha", 3)]
try:
    m.do_dictsort(data, by="rank")
except m.FilterArgumentError:
    pass
else:
    raise AssertionError("unsupported selector was accepted")""",
    ),
    _case(
        "jinja", "jinja-batch-positive-width", "表格分批禁止零或负列数",
        "do_batch", "def do_batch(", "\n\ndef do_round(",
        "后台按配置列数把行元素分批。正常批次必须保留顺序，已显式设置的填充值仍补齐尾批。",
        "给 batch 过滤器增加列数校验，0 或负数时抛出 FilterArgumentError，连空输入也一致。"
        "正整数和生成器的分批行为保持原样。",
        [
            (
                "    tmp: t.List[V] = []",
                '    if linecount <= 0:\n        raise FilterArgumentError("batch line count must be positive")\n\n'
                "    tmp: t.List[V] = []",
            )
        ],
        """assert list(m.do_batch(iter([1, 2, 3]), 2, 0)) == [[1, 2], [3, 0]]
assert list(m.do_batch([], 2)) == []
for source, width in (([], 0), (iter([1]), -2)):
    try:
        list(m.do_batch(source, width))
    except m.FilterArgumentError:
        pass
    else:
        raise AssertionError("nonpositive width was accepted")""",
    ),
    _case(
        "jinja", "jinja-filesize-precision", "文件体积标签支持自定义小数位数",
        "do_filesizeformat", "def do_filesizeformat(", "\n\ndef do_pprint(",
        "报表的十进制 kB 和二进制 KiB 都依赖原格式。小于换算基数的字节标签必须保持整数和单复数行为。",
        "为 filesizeformat 增加关键字参数 precision，默认 1，范围 0 到 6 的整数。"
        "它只调节 kB、KiB 等已换算结果的小数位数。非法值抛出 FilterArgumentError，原单位切换阈值不变。",
        [
            (
                'def do_filesizeformat(value: t.Union[str, float, int], binary: bool = False) -> str:',
                "def do_filesizeformat(\n"
                "    value: t.Union[str, float, int], binary: bool = False, *, precision: int = 1\n"
                ") -> str:",
            ),
            (
                "    bytes = float(value)",
                "    if type(precision) is not int or not 0 <= precision <= 6:\n"
                '        raise FilterArgumentError("filesize precision must be an integer from 0 to 6")\n'
                "    bytes = float(value)",
            ),
            (
                '                return f"{base * bytes / unit:.1f} {prefix}"',
                '                return f"{base * bytes / unit:.{precision}f} {prefix}"',
            ),
            (
                '        return f"{base * bytes / unit:.1f} {prefix}"',
                '        return f"{base * bytes / unit:.{precision}f} {prefix}"',
            ),
        ],
        """assert m.do_filesizeformat(1250) == "1.2 kB"
assert m.do_filesizeformat("1250", precision=2) == "1.25 kB"
assert m.do_filesizeformat(1280, True, precision=0) == "1 KiB"
assert m.do_filesizeformat(1, precision=6) == "1 Byte"
assert m.do_filesizeformat(123, precision=2) == "123 Bytes"
for invalid in (-1, 7, True, 1.5):
    try:
        m.do_filesizeformat(1200, precision=invalid)
    except m.FilterArgumentError:
        pass
    else:
        raise AssertionError("invalid precision was accepted")""",
    ),
    _case(
        "jinja", "jinja-wordcount-min-chars", "摘要关键词计数支持最短词长度",
        "do_wordcount", "def do_wordcount(", "\n\ndef do_int(",
        "沿用现有过滤器对词的定义和 Unicode 处理。只在新版报表中选用最短长度筛选。",
        "让 wordcount 接收关键字参数 min_chars，默认为 1。仍用原过滤器的分词规则，"
        "每个词按 Python 字符数长度判断。min_chars 必须是正整数，非法值抛出 FilterArgumentError。",
        [
            (
                'def do_wordcount(s: str) -> int:\n'
                '    """Count the words in that string."""\n'
                "    return len(_word_re.findall(soft_str(s)))",
                "def do_wordcount(s: str, *, min_chars: int = 1) -> int:\n"
                '    """Count words with at least min_chars Unicode characters."""\n'
                "    if type(min_chars) is not int or min_chars < 1:\n"
                '        raise FilterArgumentError("minimum word length must be a positive integer")\n'
                "    return sum(len(word) >= min_chars for word in _word_re.findall(soft_str(s)))",
            )
        ],
        """value = "a le mot 中文_2，长词；résumé"
assert m.do_wordcount(value) == 6
assert m.do_wordcount(value, min_chars=3) == 3
assert m.do_wordcount("你好 è", min_chars=2) == 1
assert m.do_wordcount("", min_chars=2) == 0
for invalid in (0, -2, True, 1.5):
    try:
        m.do_wordcount("ordinary words", min_chars=invalid)
    except m.FilterArgumentError:
        pass
    else:
        raise AssertionError("invalid minimum length was accepted")""",
    ),
    _case(
        "jinja", "jinja-indent-crlf-style", "缩进输出支持 Windows 换行",
        "do_indent", "def do_indent(", "\n\n@pass_environment\ndef do_truncate(",
        "现有缩进过滤器的 first、blank 和 Markup 类型规则必须保留，任务来自 Windows 交接文档的 CRLF 导出。",
        "为 indent 增加关键字参数 newline_sequence。支持 LF 和 CRLF，默认 LF。"
        "输入行重组使用指定换行符，保留尾随换行、空行规则和安全字符串类型。其他换行序列抛出 FilterArgumentError。",
        [
            (
                "    s: str, width: t.Union[int, str] = 4, first: bool = False, blank: bool = False",
                '    s: str, width: t.Union[int, str] = 4, first: bool = False, blank: bool = False,\n'
                '    *, newline_sequence: str = "\\n",',
            ),
            (
                '    newline = "\\n"',
                '    if newline_sequence not in ("\\n", "\\r\\n"):\n'
                '        raise FilterArgumentError("indent supports LF and CRLF output")\n'
                "    newline = newline_sequence",
            ),
            (
                "    s += newline  # this quirk is necessary for splitlines method",
                '    s += Markup("\\n") if isinstance(s, Markup) else "\\n"',
            ),
        ],
        r"""assert m.do_indent("a\n\nb\n", width=2) == "a\n\n  b\n"
assert m.do_indent("a\r\n\r\nb\r\n", width="|", first=True, newline_sequence="\r\n") == "|a\r\n\r\n|b\r\n"
markup = m.do_indent(m.Markup("<a>\nb"), width="|", blank=True, newline_sequence="\r\n")
assert isinstance(markup, m.Markup) and str(markup) == "<a>\r\n|b"
try:
    m.do_indent("a\nb", newline_sequence="|")
except m.FilterArgumentError:
    pass
else:
    raise AssertionError("invalid separator was accepted")""",
    ),
    _case(
        "jinja", "jinja-truncate-whitespace-mode", "截断支持 Unicode 空白边界",
        "do_truncate", "def do_truncate(", "\n\n@pass_environment\ndef do_wordwrap(",
        "默认截断保留老模板的 ASCII 空格分词。新版说明表可能由制表符或不换行空格分列。",
        "给 truncate 增加 unicode_whitespace 开关，默认关闭。开启且按词截断时按 Python 的 Unicode 空白拆词，"
        "接受制表符和不换行空格。保留 killwords、leeway、原默认切分和后缀行为。",
        [
            (
                "    leeway: t.Optional[int] = None,\n",
                "    leeway: t.Optional[int] = None,\n    *, unicode_whitespace: bool = False,\n",
            ),
            (
                '    result = s[: length - len(end)].rsplit(" ", 1)[0]',
                "    remaining = s[: length - len(end)]\n"
                "    if unicode_whitespace:\n"
                "        words = remaining.rsplit(None, 1)\n"
                "        result = words[0] if words else \"\"\n"
                "    else:\n"
                '        result = remaining.rsplit(" ", 1)[0]',
            ),
        ],
        r"""from jinja2 import Environment
env = Environment()
assert m.do_truncate(env, "alpha\tbeta gamma", 11, leeway=0) == "alpha\tbe..."
assert m.do_truncate(env, "alpha\tbeta gamma", 11, leeway=0, unicode_whitespace=True) == "alpha..."
assert m.do_truncate(env, "alpha\u00a0beta gamma", 11, leeway=0, unicode_whitespace=True) == "alpha..."
assert m.do_truncate(env, "alpha\tbeta gamma", 11, True, leeway=0, unicode_whitespace=True) == "alpha\tbe..."
assert m.do_truncate(env, "small", 4, leeway=1, unicode_whitespace=True) == "small" """,
    ),
    _case(
        "marshmallow", "marshmallow-string-strip-option", "导入时按字段配置修剪文本边缘",
        "String._deserialize", "class String(", "\n\nclass UUID(",
        "旧字符串字段继续保留用户提供的空白。导入端仅对明确开启 strip 的字段清理首尾空白。",
        "给 String 增加默认关闭的 strip 关键字选项，只影响反序列化。先按原规则校验并解码，"
        "开启时去掉首尾 Unicode 空白。序列化、允许 None 和原有非法值处理保持原行为。",
        [
            (
                "    def _serialize(self, value, attr, obj, **kwargs) -> str | None:",
                "    def __init__(self, *, strip: bool = False, **kwargs: Unpack[_BaseFieldKwargs]) -> None:\n"
                "        self.strip = strip\n"
                "        super().__init__(**kwargs)\n\n"
                "    def _serialize(self, value, attr, obj, **kwargs) -> str | None:",
            ),
            (
                "        try:\n            return utils.ensure_text_type(value)",
                "        try:\n            text = utils.ensure_text_type(value)\n"
                "            return text.strip() if self.strip else text",
            ),
        ],
        r"""assert m.String().deserialize("  文档 \t") == "  文档 \t"
assert m.String(strip=True).deserialize("\u3000 文档 \t") == "文档"
assert m.String(strip=True).deserialize("  文档  ".encode()) == "文档"
assert m.String(strip=True, allow_none=True).deserialize(None) is None
assert m.String(strip=True).serialize("title", {"title": "  文档  "}) == "  文档  "
for invalid in (123, b"\xff"):
    try:
        m.String(strip=True).deserialize(invalid)
    except m.ValidationError:
        pass
    else:
        raise AssertionError("invalid text was accepted")""",
    ),
    _case(
        "marshmallow", "marshmallow-uuid-buffer-types", "UUID 字段兼容二进制缓存视图",
        "UUID._validated", "class UUID(", "\n\n_NumT =",
        "UUID 已识别字符串、uuid.UUID 对象和 16 字节 bytes。导入器也可能拿到 bytearray 或 memoryview。",
        "对 UUID 反序列化支持长度正好 16 字节的 bytearray 和 memoryview。"
        "对象、既有文本和 bytes 路径保持原样，不合规字节长度仍返回 ValidationError。",
        [
            (
                "            if isinstance(value, bytes) and len(value) == 16:\n                return uuid.UUID(bytes=value)",
                "            if isinstance(value, (bytes, bytearray, memoryview)):\n"
                "                raw = bytes(value)\n"
                "                if len(raw) == 16:\n"
                "                    return uuid.UUID(bytes=raw)",
            ),
        ],
        """from uuid import UUID
token = UUID("497f6eca-6276-4993-bfeb-53cbbbba6f08")
field = m.UUID()
for value in (token, str(token), token.bytes, bytearray(token.bytes), memoryview(token.bytes)):
    assert field.deserialize(value) == token
for invalid in (b"x", bytearray(15), memoryview(bytes(17)), "bad"):
    try:
        field.deserialize(invalid)
    except m.ValidationError:
        pass
    else:
        raise AssertionError("invalid UUID was accepted")""",
    ),
    _case(
        "marshmallow", "marshmallow-integer-base", "导入整数按字段指定文本进制",
        "Integer._validated", "class Integer(", "\n\nclass Float(",
        "严格模式仍只接受原先允许的整型。进制只用于文本读入，序列化输出数值语义保持不变。",
        "给 Integer 增加 base 参数默认十进制，支持 0 自动识别和 2 至 36。"
        "非严格反序列化按它解析字符串，普通数字不受影响，严格模式与布尔值校验、as_string 的十进制输出保持原样。",
        [
            (
                "        strict: bool = False,\n",
                "        strict: bool = False,\n        base: int = 10,\n",
            ),
            (
                "        self.strict = strict\n",
                "        if type(base) is not int or (base != 0 and not 2 <= base <= 36):\n"
                '            raise ValueError("integer base must be 0 or between 2 and 36")\n'
                "        self.strict = strict\n        self.base = base\n",
            ),
            (
                "        return super()._validated(value)",
                "        if isinstance(value, str):\n"
                "            try:\n"
                "                return int(value, self.base)\n"
                "            except ValueError as error:\n"
                '                raise self.make_error("invalid", input=value) from error\n'
                "        return super()._validated(value)",
            ),
        ],
        """assert m.Integer().deserialize("25") == 25
assert m.Integer(base=16).deserialize("fF") == 255
assert m.Integer(base=0).deserialize("0b10100") == 20
assert m.Integer(base=16).deserialize(25) == 25
assert m.Integer(base=16, as_string=True).serialize("n", {"n": 255}) == "255"
for field, invalid in ((m.Integer(base=16, strict=True), "ff"), (m.Integer(base=2), "3"), (m.Integer(base=2), True)):
    try:
        field.deserialize(invalid)
    except m.ValidationError:
        pass
    else:
        raise AssertionError("invalid integer was accepted")
for invalid in (1, 37, True):
    try:
        m.Integer(base=invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid base was accepted")""",
    ),
    _case(
        "marshmallow", "marshmallow-date-normalize-datetime", "日期反序列化统一返回日历日期",
        "Date._make_object_from_format", "class Date(", "\n\nclass TimeDelta(",
        "日期字段的原生类型应为日期。保留传入日期对象、ISO 日期和客户日期模板的行为。",
        "当 Date 反序列化接收到 datetime 对象时，返回它本身的日历日期，去掉时间和时区信息。"
        "不要发生时区换算，普通 DateTime 字段不受影响，原有日期字符串格式仍可用。",
        [
            (
                '    SCHEMA_OPTS_VAR_NAME = "dateformat"\n',
                '    SCHEMA_OPTS_VAR_NAME = "dateformat"\n\n'
                "    def _deserialize(self, value, attr, data, **kwargs) -> dt.date:\n"
                "        if isinstance(value, dt.datetime):\n"
                "            return value.date()\n"
                "        return super()._deserialize(value, attr, data, **kwargs)\n",
            ),
        ],
        """from datetime import date, datetime, timedelta, timezone
value = datetime(2024, 3, 4, 23, 30, tzinfo=timezone(-timedelta(hours=8)))
normalized = m.Date().deserialize(value)
assert type(normalized) is date and normalized == date(2024, 3, 4)
assert m.Date().deserialize(date(2023, 2, 1)) == date(2023, 2, 1)
assert m.Date().deserialize("2025-01-07") == date(2025, 1, 7)
assert m.Date(format="%d/%m/%Y").deserialize("07/01/2025") == date(2025, 1, 7)
assert m.DateTime().deserialize(value) is value""",
    ),
    _case(
        "marshmallow", "marshmallow-timedelta-precision-alias", "时间差字段兼容配置单位简称",
        "TimeDelta.__init__", "class TimeDelta(", "\n\n_MappingT =",
        "内部时间单位继续使用现有完整英文名称，保持序列化和反序列化单位一致。简写与大小写来自外部项目配置。",
        "TimeDelta 的 precision 新增 s、ms、us、min、h 五种简称，对应秒、毫秒、微秒、分、时。"
        "允许输入大小写和边缘空白，完整单位照旧，未知单位保留 ValueError。",
        [
            (
                "        precision = precision.lower()",
                "        precision = precision.strip().lower()\n"
                '        precision = {"s": self.SECONDS, "ms": self.MILLISECONDS, "us": self.MICROSECONDS,\n'
                '                     "min": self.MINUTES, "h": self.HOURS}.get(precision, precision)',
            ),
        ],
        """from datetime import timedelta
for label, unit in ((" s ", "seconds"), ("MS", "milliseconds"), ("us", "microseconds"), ("MIN", "minutes"), ("h", "hours")):
    field = m.TimeDelta(precision=label)
    value = timedelta(**{unit: 6})
    assert field.deserialize("6") == value
    assert field.serialize("value", {"value": value}) == 6.0
assert m.TimeDelta(precision="Weeks").deserialize("1") == timedelta(days=7)
try:
    m.TimeDelta(precision="fortnight")
except ValueError:
    pass
else:
    raise AssertionError("invalid unit was accepted")""",
    ),
    _case(
        "marshmallow", "marshmallow-enum-folded-name", "枚举字段可选名称大小写兼容",
        "Enum._deserialize", "class Enum(", "\n\nclass Method(",
        "默认枚举名称区分大小写；已有别名和按值序列化保留。使用者希望为部分人类输入开启宽松名称模式。",
        "给 Enum 增加默认开启的 case_sensitive。设为 False 时仅对按名称反序列化启用 Unicode 大小写折叠，"
        "精确匹配优先，折叠到多个不同成员时报现有 ValidationError。多个别名指向同一成员时可读，按值模式不变。",
        [
            (
                "        by_value: bool | Field | type[Field] = False,\n",
                "        by_value: bool | Field | type[Field] = False,\n        case_sensitive: bool = True,\n",
            ),
            (
                "        self.by_value = by_value\n",
                "        self.by_value = by_value\n        self.case_sensitive = case_sensitive\n",
            ),
            (
                "        except KeyError as error:\n"
                '            raise self.make_error("unknown", choices=self.choices_text) from error\n'
                "        return ret",
                "        except KeyError as error:\n"
                "            if not self.case_sensitive:\n"
                "                normalized = val.casefold()\n"
                "                matches = {member for name, member in self.enum.__members__.items()\n"
                "                           if name.casefold() == normalized}\n"
                "                if len(matches) == 1:\n"
                "                    return matches.pop()\n"
                '            raise self.make_error("unknown", choices=self.choices_text) from error\n'
                "        return ret",
            ),
        ],
        """from enum import Enum
class Modes(Enum):
    Ready = "text"
    rEADY = "text"
    POST = "upper"
    post = "lower"
field = m.Enum(Modes, case_sensitive=False)
assert field.deserialize("ready") is Modes.Ready
assert field.deserialize("POST") is Modes.POST
assert field.serialize("state", {"state": Modes.Ready}) == "Ready"
assert m.Enum(Modes, by_value=True, case_sensitive=False).deserialize("text") is Modes.Ready
for field, invalid in ((field, "PoSt"), (m.Enum(Modes), "ready"),
                       (m.Enum(Modes, by_value=True, case_sensitive=False), "TEXT")):
    try:
        field.deserialize(invalid)
    except m.ValidationError:
        pass
    else:
        raise AssertionError("ambiguous or invalid enum alias was accepted")""",
    ),
)
