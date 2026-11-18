"""记录四个公开项目的独立维护需求、参考变更和行为评分。"""

from __future__ import annotations

from textwrap import dedent


def case(project: str, identifier: str, title: str, module: str, symbol: str, setup: str,
         request: str, old: str, new: str, check: str, related: tuple[str, ...] = ()) -> dict:
    """构造一道冻结题；old/new 为精确文本变更，check 以 m 指向候选模块。"""
    return {"project": project, "id": identifier, "title": title, "module": module,
            "symbol": symbol, "setup": setup, "prompt": f"继续维护 {module} 中的 {symbol}。{request}"
            " 按此前的约定完成，只修改必要代码，保留其他行为，最后说明改动。",
            "edit_old": old, "edit_new": new, "check": dedent(check).strip() + "\n", "related": related}


# 每组前两题低压力，中间两题临界，最后两题超限。检查源码与阅卷材料仅在宿主使用。
CASES = (
    case("click", "cx-click-table-width", "多行帮助说明按最长一行计宽", "click.formatting", "measure_table",
         "帮助表格的一格可以多行，保留现有对终端颜色控制序列的宽度处理。",
         "让表格计量采用各单元格中最长一行的宽度，空单元格宽度为零，仍支持一遍式输入。",
         "widths[idx] = max(widths.get(idx, 0), term_len(col))",
         "widths[idx] = max(widths.get(idx, 0), max(map(term_len, col.splitlines()), default=0))",
         """
         assert m.measure_table(iter([("hi\\nhey", ""), ("A", "\\x1b[31m1234\\x1b[0m")])) == (3, 4)
         assert m.measure_table([("abc", "de"), ("g", "2")]) == (3, 2)
         assert m.measure_table([]) == ()
         """, ("click/core.py", "click/_compat.py", "click/_textwrap.py")),
    case("click", "cx-click-unique-alias", "格式化帮助别名时合并重复项", "click.formatting", "join_options",
         "选项别名按前缀宽度分组，同组沿用声明顺序，并返回是否出现斜线前缀。",
         "来自组合装饰器的完全相同别名只展示一次，保留首次出现的别名位置。",
         "for opt in options:\n        prefix = _split_opt(opt)[0]",
         "for opt in dict.fromkeys(options):\n        prefix = _split_opt(opt)[0]",
         """
         assert m.join_options(iter(["--check", "-c", "--check", "-c", "/v"])) == ("-c, /v, --check", True)
         assert m.join_options(iter(["-a", "-b", "--z"])) == ("-a, -b, --z", False)
         assert m.join_options([]) == ("", False)
         """, ("click/decorators.py", "click/core.py", "click/parser.py")),
    case("click", "cx-click-state-words", "布尔环境值接受运维模式用词", "click.types", "BoolParamType.str_to_bool",
         "导入器将外部字符串大小写和首尾空白统一处理，未知值交还调用方。",
         "将 enabled / disabled 作为新的真、假别名。让已有布尔值与其他解析结果保持一致。",
         '        "off": False,\n        "t": True,',
         '        "off": False,\n        "enabled": True,\n        "disabled": False,\n        "t": True,',
         """
         parse = m.BoolParamType.str_to_bool
         assert parse(" Enabled ") is True and parse("dISABLED ") is False
         assert parse("maybe enable") is None and parse("no") is False and parse(True) is True
         """, ("click/core.py", "click/decorators.py", "click/_utils.py")),
    case("click", "cx-click-display-directory", "目录简短显示保留最后一段", "click.utils", "format_filename",
         "展示路径与访问路径分离，格式化不得读取或修改磁盘。平台路径语义应保留。",
         "shorten=True 时把末尾的目录分隔符视为修饰，返回最后一个目录名；绝对根目录仍有可显示名称。",
         "filename = os.path.basename(filename)\n    else:\n        filename = os.fspath(filename)",
         "path = os.fspath(filename)\n        filename = os.path.basename(os.path.normpath(path)) or os.path.normpath(path)\n    else:\n        filename = os.fspath(filename)",
         """
         import os
         path = os.path.join("tmp", "reports", "")
         assert m.format_filename(path, shorten=True) == "reports"
         assert m.format_filename(path) == path
         assert m.format_filename(os.path.abspath(os.sep), shorten=True)
         assert m.format_filename(os.path.join("company", "a.txt"), shorten=True) == "a.txt"
         """, ("click/termui.py", "click/types.py", "click/_compat.py")),
    case("click", "cx-click-buffer-string", "终端短文档支持字节缓冲对象", "click.utils", "make_str",
         "解码的首选编码和替换模式沿用现有 bytes 分支，普通对象继续通过 str 表示。",
         "让 bytearray 和 memoryview 跟 bytes 一样解码，涵盖非法编码和普通对象。",
         '    """Converts a value into a valid string."""\n    if isinstance(value, bytes):',
         '    """Converts a value into a valid string."""\n    if isinstance(value, (bytearray, memoryview)):\n        value = bytes(value)\n    if isinstance(value, bytes):',
         """
         assert m.make_str(bytearray(b"ticket-42")) == "ticket-42"
         assert m.make_str(memoryview(b"start")) == "start"
         assert m.make_str(memoryview(b"\\xff")) == m.make_str(b"\\xff")
         assert m.make_str(42) == "42"
         """, ("click/termui.py", "click/_compat.py", "click/testing.py")),
    case("click", "cx-click-casecolor", "兼容混合大小写的调色板名称", "click.termui", "_interpret_color",
         "颜色名称只需做大小写兼容，既有 256 色数值和 RGB 元组的编码规则保留。",
         "允许用户在颜色配置中使用颜色名称的混合大小写，未知名称仍报原类型错误。",
         'if isinstance(color, str) and color in _ansi_colors:\n        return str(_ansi_colors[color] + offset)',
         'if isinstance(color, str) and color.lower() in _ansi_colors:\n        return str(_ansi_colors[color.lower()] + offset)',
         """
         assert m.style("ok", fg="BrIgHt_BlUe", bg="RED") == m.style("ok", fg="bright_blue", bg="red")
         assert m.style("x", fg=1, bg=(3, 4, 5)).endswith("x\\x1b[0m")
         try:
             m.style("x", fg="NOT-A-COLOR")
         except ValueError:
             pass
         else:
             raise AssertionError("unknown")
         """, ("click/_compat.py", "click/_termui_impl.py", "click/utils.py")),
    case("pytest", "cx-pytest-section-label", "读取捕获区段时保留完整标签", "_pytest.reports", "BaseReport.get_sections",
         "同一前缀可能对应 setup、call、teardown 多阶段，段内容和段顺序均须保留。",
         "使前缀查找返回原始区段名称，方便收集器在结果中注明所属阶段。",
         "if name.startswith(prefix):\n                yield prefix, content",
         "if name.startswith(prefix):\n                yield name, content",
         """
         report = m.BaseReport()
         report.sections = [("Captured stdout setup", "set"), ("Captured stderr call", "err"), ("Captured stdout call", "out")]
         assert list(report.get_sections("Captured stdout")) == [("Captured stdout setup", "set"), ("Captured stdout call", "out")]
         assert list(report.get_sections("missing")) == []
         assert report.capstdout == "setout"
         """, ("_pytest/terminal.py", "_pytest/capture.py", "_pytest/runner.py")),
    case("pytest", "cx-pytest-exit-blank", "空退出原因显示默认说明", "_pytest.outcomes", "Exit.__init__",
         "退出码是调用者明确指定的值，包括 0。非空说明文本保持原样。",
         "当退出消息仅包含空白时，为异常属性和字符串表示提供同一默认说明。",
         "        self.msg = msg\n        self.returncode = returncode\n        super().__init__(msg)",
         '        if not msg.strip():\n            msg = "unknown reason"\n        self.msg = msg\n        self.returncode = returncode\n        super().__init__(msg)',
         """
         event = m.Exit(" \\t", returncode=0)
         assert event.msg == "unknown reason" and str(event) == event.msg and event.returncode == 0
         assert m.Exit("  explicit  ").msg == "  explicit  "
         assert m.Exit().returncode is None
         """, ("_pytest/main.py", "_pytest/runner.py", "_pytest/config/__init__.py")),
    case("pytest", "cx-pytest-crlf-summary", "故障短摘要支持 CRLF 第一行", "_pytest.terminal", "_format_trimmed",
         "仍要应用既有宽度限制，并保留原先处理 LF 的输出。",
         "当输入来自 Windows 文本流时，短摘要首行不应带回车字符。",
         '    i = msg.find("\\n")\n    if i != -1:\n        msg = msg[:i]',
         '    i = msg.find("\\n")\n    if i != -1:\n        msg = msg[:i].removesuffix("\\r")',
         """
         assert m._format_trimmed("[{}]", "first\\r\\nsecond", 20) == "[first]"
         assert m._format_trimmed("[{}]", "first\\nsecond", 20) == "[first]"
         assert m._format_trimmed("[{}]", "abcdef", 7) == "[ab...]"
         """, ("_pytest/reports.py", "_pytest/_io/terminalwriter.py", "_pytest/main.py")),
    case("pytest", "cx-pytest-warning-default", "诊断插件兼容单数 warning 标签", "_pytest.terminal", "pluralize",
         "插件可能使用 warning 或 warnings；既有 error/test 的单复数和 failed/passed 计数标签不改。",
         "使 warning 和 warnings 两种输入都根据数量得到相同的展示结果。",
         'if noun not in ["error", "warnings", "test"]:',
         'if noun not in ["error", "warning", "warnings", "test"]:',
         """
         assert m.pluralize(0, "warning") == (0, "warnings")
         assert m.pluralize(1, "warnings") == (1, "warning")
         assert m.pluralize(2, "warning") == (2, "warnings")
         assert m.pluralize(3, "failed") == (3, "failed")
         assert m.pluralize(1, "error") == (1, "error")
         """, ("_pytest/reports.py", "_pytest/warnings.py", "_pytest/runner.py")),
    case("pytest", "cx-pytest-clean-path", "比较相同目录时支持空路径约定", "_pytest.pathlib", "bestrelpath",
         "显示模式调用者使用空片段表示同一目录。默认继续返回当前目录标记，其他相对路径规则不变。",
         "新增关键字参数 empty_same，默认 False，True 时相同的起止路径结果为字符串空串。",
         'def bestrelpath(directory: Path, dest: Path) -> str:\n    """Return a string which is a relative path from directory to dest such\n    that directory/bestrelpath == dest.\n\n    The paths must be either both absolute or both relative.\n\n    If no such path can be determined, returns dest.\n    """\n    assert isinstance(directory, Path)\n    assert isinstance(dest, Path)\n    if dest == directory:\n        return os.curdir',
         'def bestrelpath(directory: Path, dest: Path, *, empty_same: bool = False) -> str:\n    """Return a relative path to dest or an empty string for identical paths when requested."""\n    assert isinstance(directory, Path)\n    assert isinstance(dest, Path)\n    if dest == directory:\n        return "" if empty_same else os.curdir',
         """
         from pathlib import Path
         root = Path("work/demo")
         assert m.bestrelpath(root, root, empty_same=True) == ""
         assert m.bestrelpath(root, root) == m.os.curdir
         assert m.bestrelpath(root, root / "new.txt", empty_same=True) == "new.txt"
         """, ("_pytest/main.py", "_pytest/config/__init__.py", "_pytest/nodes.py")),
    case("pytest", "cx-pytest-cache-schema", "缓存读取兼容可选结果类型检查", "_pytest.cacheprovider", "Cache.get",
         "缺失和损坏缓存使用调用者默认值；原有调用仍可以保存与读回任意 JSON 数据。",
         "为 Cache.get 添加可选仅关键字 expected_type，命中值不是指定类型时回退默认值。支持 isinstance 接收的类型元组。",
         '    def get(self, key: str, default):\n',
         '    def get(self, key: str, default, *, expected_type=None):\n',
         """
         from pathlib import Path
         from tempfile import TemporaryDirectory
         with TemporaryDirectory() as tmp:
             folder = Path(tmp)
             (folder / "v" / "plugin").mkdir(parents=True)
             (folder / "v" / "plugin" / "entry").write_text("[1, 2]")
             cache = m.Cache(folder, object(), _ispytest=True)
             assert cache.get("plugin/entry", "fallback", expected_type=dict) == "fallback"
             assert cache.get("plugin/entry", None, expected_type=(tuple, list)) == [1, 2]
             assert cache.get("plugin/entry", None) == [1, 2]
             assert cache.get("plugin/missing", "fallback", expected_type=list) == "fallback"
         """, ("_pytest/config/__init__.py", "_pytest/pathlib.py", "_pytest/main.py"))
    | {"patches": (('                return json.load(f)\n        except (ValueError, OSError):\n            return default',
                   '                value = json.load(f)\n            return value if expected_type is None or isinstance(value, expected_type) else default\n        except (ValueError, OSError):\n            return default'),)},
    case("rich", "cx-rich-measure-negative", "版面宽度测量拒绝负结果", "rich.measure", "Measurement.with_maximum",
         "合法非负上限沿用原有裁剪，负值由版面计算下溢产生；调用方期望保留空测量。",
         "让负的最大宽度等效于零，结果两端都归零，保留原本的返回类型。",
         "        return Measurement(min(minimum, width), min(maximum, width))",
         "        width = max(0, width)\n        return Measurement(min(minimum, width), min(maximum, width))",
         """
         assert m.Measurement(2, 7).with_maximum(-5) == m.Measurement(0, 0)
         assert m.Measurement(2, 7).with_maximum(4) == m.Measurement(2, 4)
         assert m.Measurement(2, 7).with_maximum(9) == m.Measurement(2, 7)
         """, ("rich/console.py", "rich/table.py", "rich/layout.py")),
    case("rich", "cx-rich-blend-edge", "渐变越过端点时保持端点颜色", "rich.color", "blend_rgb",
         "中间插值保持当前按通道取整的规则，越界比例来自动画取样。",
         "将混合比例限制在 [0,1]，越界分别返回首尾颜色。",
         "    r1, g1, b1 = color1\n    r2, g2, b2 = color2",
         "    cross_fade = min(1.0, max(0.0, cross_fade))\n    r1, g1, b1 = color1\n    r2, g2, b2 = color2",
         """
         start = m.ColorTriplet(10, 40, 30)
         end = m.ColorTriplet(19, 31, 48)
         assert m.blend_rgb(start, end, -0.25) == start
         assert m.blend_rgb(start, end, 1.75) == end
         assert m.blend_rgb(start, end, 0.5) == m.ColorTriplet(14, 35, 39)
         """, ("rich/style.py", "rich/color_triplet.py", "rich/theme.py")),
    case("rich", "cx-rich-json-compact", "JSON renderable 支持单行紧凑编码", "rich.json", "JSON.from_data",
         "紧凑模式用于行式日志，排序、非 ASCII 和是否高亮仍由现有选项控制，默认格式保持。",
         "给 from_data 增加关键字 compact，True 时无缩进且分隔符无附加空格。构造函数保持现有默认。",
         '        sort_keys: bool = False,\n    ) -> "JSON":',
         '        sort_keys: bool = False,\n        *, compact: bool = False,\n    ) -> "JSON":',
         """
         x = {"b": [1, 2], "a": "样例"}
         assert m.JSON.from_data(x, highlight=False, sort_keys=True, compact=True).text.plain == '{"a":"样例","b":[1,2]}'
         assert "\\n" in m.JSON.from_data(x, highlight=False).text.plain
         assert "\\\\u6837" in m.JSON.from_data(x, ensure_ascii=True, compact=True).text.plain
         """, ("rich/highlighter.py", "rich/text.py", "rich/console.py"))
    | {"patches": (('        json_instance: "JSON" = cls.__new__(cls)\n        json = dumps(\n            data,\n            indent=indent,',
                   '        json_instance: "JSON" = cls.__new__(cls)\n        json = dumps(\n            data,\n            indent=None if compact else indent,\n            separators=(",", ":") if compact else None,'),)},
    case("rich", "cx-rich-segment-cut", "负列偏移按起始列切分", "rich.segment", "Segment.split_cells",
         "切分保留样式和控制元数据，既有双宽字符边界行为不变。",
         "将负的切分位置视为从起点切分，结果等效于 split_cells(0)，同时支持普通文本和双宽文本。",
         "        text, style, control = self\n        assert cut >= 0",
         "        text, style, control = self\n        cut = max(0, cut)",
         """
         from rich.style import Style
         style = Style(color="green")
         for text in ("abcd", "界abc"):
             item = m.Segment(text, style)
             assert item.split_cells(-3) == item.split_cells(0)
         assert m.Segment("abcd", style).split_cells(2) == (m.Segment("ab", style), m.Segment("cd", style))
         """, ("rich/cells.py", "rich/text.py", "rich/console.py")),
    case("rich", "cx-rich-join-empty", "连接带样式片段时可省略空项", "rich.text", "Text.join",
         "调用者可能提供只能遍历一次的迭代器。保留实质片段和分隔符的样式跨度。",
         "Text.join 增加仅关键字 skip_empty，打开后过滤空文本，默认行为保持。",
         '    def join(self, lines: Iterable["Text"]) -> "Text":',
         '    def join(self, lines: Iterable["Text"], *, skip_empty: bool = False) -> "Text":',
         """
         divider = m.Text("/", style="blue")
         parts = [m.Text("A", style="bold"), m.Text(""), m.Text("Z", style="red")]
         compact = divider.join(iter(parts), skip_empty=True)
         assert compact.plain == "A/Z"
         assert [(s.start, s.end, s.style) for s in compact.spans] == [(0, 1, "bold"), (1, 2, "blue"), (2, 3, "red")]
         assert divider.join(parts).plain == "A//Z"
         """, ("rich/_loop.py", "rich/markup.py", "rich/style.py"))
    | {"patches": (('        new_text = self.blank_copy()\n\n        def iter_text() -> Iterable["Text"]:',
                   '        new_text = self.blank_copy()\n        if skip_empty:\n            lines = (line for line in lines if line.plain)\n\n        def iter_text() -> Iterable["Text"]:'),)},
    case("rich", "cx-rich-markup-escape-independent", "允许将普通数值作为标记文本插入", "rich.markup", "escape",
         "插值后的文本仍交给原有方括号和反斜杠逻辑。非字符串值采用已有 Python 文本表示。",
         "让 escape 兼容字符串之外的普通值，仍按字面转义含样式标记的字符串。",
         '    markup = _escape(escape_backslashes, markup)',
         '    markup = _escape(escape_backslashes, str(markup))',
         """
         assert m.escape(0) == "0" and m.escape(None) == "None"
         assert m.render(m.escape("[blue]literal[/blue]")).plain == "[blue]literal[/blue]"
         assert m.escape("raw") == "raw"
         """, ("rich/text.py", "rich/console.py", "rich/style.py")),
    case("cpython", "cx-cpy-calendar-column", "文本周标题支持自定义分隔符", "calendar", "TextCalendar.formatweekheader",
         "调用方可能以周日或周一开周。保留星期顺序、宽度及默认分隔符。",
         "给周标题添加只接受关键字的 separator，默认空格。用它连接每个格式化的列名。",
         '    def formatweekheader(self, width):',
         '    def formatweekheader(self, width, *, separator=" "):',
         """
         cal = m.TextCalendar(firstweekday=6)
         expected = [cal.formatweekday(day, 2) for day in cal.iterweekdays()]
         assert cal.formatweekheader(2, separator=" | ") == " | ".join(expected)
         assert cal.formatweekheader(2, separator="") == "".join(expected)
         assert cal.formatweekheader(2) == " ".join(expected)
         """, ("Lib/argparse.py", "Lib/string.py", "Lib/textwrap.py"))
    | {"patches": (("        return ' '.join(self.formatweekday(i, width) for i in self.iterweekdays())",
                   "        return separator.join(self.formatweekday(i, width) for i in self.iterweekdays())"),)},
    case("cpython", "cx-cpy-match-dedup", "文件名匹配提供稳定的可选去重", "fnmatch", "filter",
         "调用者可能传入单次迭代器。默认保留重复命中，大小写判断仍按平台路径规则。",
         "给 filter 增加仅关键字 deduplicate，默认为 False。True 时保留每个命中文件名的第一次出现。",
         "def filter(names, pat):", "def filter(names, pat, *, deduplicate=False):",
         """
         selected = ["b.txt", "readme", "a.txt", "b.txt", "c.txt", "a.txt"]
         assert m.filter(iter(selected), "*.txt", deduplicate=True) == ["b.txt", "a.txt", "c.txt"]
         assert m.filter(iter(selected), "*.txt") == ["b.txt", "a.txt", "b.txt", "c.txt", "a.txt"]
         assert m.filter([b"x.a", b"x.a"], b"*.a", deduplicate=True) == [b"x.a"]
         """, ("Lib/glob.py", "Lib/pathlib.py", "Lib/re/__init__.py"))
    | {"patches": (("    return result\n\ndef fnmatchcase", "    return list(dict.fromkeys(result)) if deduplicate else result\n\ndef fnmatchcase"),)},
    case("cpython", "cx-cpy-diff-order", "近似文本分数同分时保留原次序", "difflib", "get_close_matches",
         "数据源的候选顺序有业务含义。同分保持原顺序，分数不同仍保持降序。",
         "调整近似建议排序的同分规则，保留 cutoff 与 n 的输入验证和效果。",
         "    result = _nlargest(n, result)", "    result = _nlargest(n, result, key=lambda item: item[0])",
         """
         assert m.get_close_matches("cat", ["cap", "can", "cat", "car"], n=4) == ["cat", "cap", "can", "car"]
         assert m.get_close_matches("cat", ["car", "cab", "cat", "bird"], n=2) == ["cat", "car"]
         assert m.get_close_matches("cat", ["dog"], cutoff=0.9) == []
         """, ("Lib/fnmatch.py", "Lib/textwrap.py", "Lib/string.py")),
    case("cpython", "cx-cpy-config-unicode", "配置项键名支持 Unicode 大小写折叠", "configparser", "RawConfigParser.optionxform",
         "默认规则只处理选项键。节名区分大小写，自定义 optionxform 接口保持。",
         "让默认的选项标准化使用 Unicode casefold，使 Straße 和 STRASSE 可以表示同一选项。",
         "        return optionstr.lower()", "        return optionstr.casefold()",
         """
         parser = m.ConfigParser()
         parser.read_string("[Section]\\nStraße = value\\nABC = z\\n")
         assert parser.get("Section", "STRASSE") == "value"
         assert parser.get("Section", "abc") == "z"
         assert not parser.has_section("section")
         parser.optionxform = str
         parser.read_string("[Other]\\nTitle = v\\n")
         assert parser.has_option("Other", "Title") and not parser.has_option("Other", "title")
         """, ("Lib/argparse.py", "Lib/pathlib.py", "Lib/string.py")),
    case("cpython", "cx-cpy-shorten-spacing", "可选保留标注中的连续空格", "textwrap", "shorten",
         "内部标签的连续空格在显示中有意义；两端仍裁掉，默认旧行为继续折叠空白。",
         "增加仅关键字 preserve_spaces，默认 False。True 时保留普通连续空格，仍执行一行宽度及占位符限制。",
         "def shorten(text, width, **kwargs):",
         "def shorten(text, width, *, preserve_spaces=False, **kwargs):",
         """
         assert m.shorten("  A   B  ", 12, preserve_spaces=True) == "A   B"
         assert m.shorten("  A   B  ", 12) == "A B"
         assert m.shorten("abc  def  ghi", 8, preserve_spaces=True, placeholder=" ...") == "abc ..."
         """, ("Lib/difflib.py", "Lib/string.py", "Lib/argparse.py"))
    | {"patches": (("    return w.fill(' '.join(text.strip().split()))",
                   "    text = text.strip()\n    return w.fill(text if preserve_spaces else ' '.join(text.split()))"),)},
    case("cpython", "cx-cpy-shell-path-display", "路径对象兼容命令预览的字面显示", "shlex", "quote",
         "预览只生成可读文本。必须保留已有字符串、空串和普通带空格值的规则。",
         "字符串 PathLike 对象先转为路径文本，再用原有规则得到展示字符串；文件不需要存在。",
         '    """Return a shell-escaped version of the string *s*."""\n    if not s:',
         '    """Return a shell-escaped version of the string *s*."""\n    if isinstance(s, os.PathLike):\n        s = os.fspath(s)\n    if not s:',
         """
         from pathlib import PurePosixPath
         path = PurePosixPath("output/my file")
         assert m.quote(path) == m.quote(str(path))
         assert m.quote("safe.txt") == "safe.txt"
         assert m.quote("") == "''"
         """, ("Lib/argparse.py", "Lib/pathlib.py", "Lib/string.py")),
)

