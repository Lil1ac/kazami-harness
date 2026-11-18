"""保存用于校验新题可解性的独立参考修改，每题只改一个标准库模块。"""


# 每组是原文与参考解的精确替换对，按原文件阅读顺序排列。
_REPLACEMENTS = {
    "multiturn-textwrap-indent": (
        (
            "def indent(text, prefix, predicate=None):",
            "def indent(text, prefix, predicate=None, *, first_prefix=None, restart_on_blank=False):",
        ),
        (
            "    consist solely of whitespace characters.\n"
            "    \"\"\"\n"
            "    if predicate is None:\n"
            "        def predicate(line):\n"
            "            return line.strip()\n"
            "\n"
            "    def prefixed_lines():\n"
            "        for line in text.splitlines(True):\n"
            "            yield (prefix + line if predicate(line) else line)",
            "    consist solely of whitespace characters.\n"
            "\n"
            "    返回逐行加前缀后的文本。first_prefix=None 沿用普通前缀；空字符串是有效值。\n"
            "    restart_on_blank 开启且设置首行前缀时，空白行分段且不使用首行前缀。\n"
            "    \"\"\"\n"
            "    if predicate is None:\n"
            "        def predicate(line):\n"
            "            \"\"\"返回含非空白字符的行内容，空白行返回空字符串。\"\"\"\n"
            "            return line.strip()\n"
            "\n"
            "    def prefixed_lines():\n"
            "        \"\"\"按原换行形态依次产生每行内容，无需缓存段落。\"\"\"\n"
            "        first_available = first_prefix is not None\n"
            "        for line in text.splitlines(True):\n"
            "            paragraph_break = restart_on_blank and first_prefix is not None and not line.strip()\n"
            "            if paragraph_break:\n"
            "                first_available = True\n"
            "            if not predicate(line):\n"
            "                yield line\n"
            "            elif first_available and not paragraph_break:\n"
            "                yield first_prefix + line\n"
            "                first_available = False\n"
            "            else:\n"
            "                yield prefix + line",
        ),
    ),
    "multiturn-fnmatch-filter": (
        (
            "def filter(names, pat):\n"
            "    \"\"\"Construct a list from those elements of the iterable NAMES that match PAT.\"\"\"\n"
            "    result = []\n"
            "    pat = os.path.normcase(pat)\n"
            "    match = _compile_pattern(pat)\n"
            "    if os.path is posixpath:\n"
            "        # normcase on posix is NOP. Optimize it away from the loop.\n"
            "        for name in names:\n"
            "            if match(name):\n"
            "                result.append(name)\n"
            "    else:\n"
            "        for name in names:\n"
            "            if match(os.path.normcase(name)):\n"
            "                result.append(name)\n"
            "    return result",
            "def filter(names, pat, *, exclude=(), keep=()):\n"
            "    \"\"\"按输入顺序返回匹配 pat 的 names，保留重复名称。\n"
            "\n"
            "    exclude 为会剔除主匹配项的模式集合，keep 为可恢复剔除项的模式集合。\n"
            "    任一集合为空时不触发对应规则；集合的元素类型应与 pat 和名称一致。\n"
            "    \"\"\"\n"
            "    match = _compile_pattern(os.path.normcase(pat))\n"
            "    excluded = tuple(_compile_pattern(os.path.normcase(rule)) for rule in exclude)\n"
            "    retained = tuple(_compile_pattern(os.path.normcase(rule)) for rule in keep)\n"
            "\n"
            "    def selected(normalized_name):\n"
            "        \"\"\"对已规范化的名称判断主匹配、排除和恢复，返回是否保留。\"\"\"\n"
            "        if not match(normalized_name):\n"
            "            return False\n"
            "        return (not any(rule(normalized_name) for rule in excluded)\n"
            "                or any(rule(normalized_name) for rule in retained))\n"
            "\n"
            "    if os.path is posixpath:\n"
            "        return [name for name in names if selected(name)]\n"
            "    return [name for name in names if selected(os.path.normcase(name))]",
        ),
    ),
    "multiturn-heapq-merge": (
        (
            "def merge(*iterables, key=None, reverse=False):",
            "def merge(*iterables, key=None, reverse=False, unique=False, keep_last=False):",
        ),
        (
            "    '''\n\n"
            "    h = []\n"
            "    h_append = h.append",
            "    unique 为真时，相同键的连续元素视为一组，仅依次返回各组第一项；\n"
            "    keep_last 为真且已开启去重时，改为返回各组最后一项。\n"
            "    '''\n"
            "    if unique:\n"
            "        from itertools import groupby\n"
            "        from operator import itemgetter\n"
            "\n"
            "        if key is None:\n"
            "            pairs = ((value, value) for value in merge(*iterables, reverse=reverse))\n"
            "        else:\n"
            "            def keyed(value):\n"
            "                \"\"\"为单个输入值计算一次排序键，保留原值作为可输出内容。\"\"\"\n"
            "                return key(value), value\n"
            "\n"
            "            # 惰性装饰各输入，合并和去重共用同一个排序键。\n"
            "            decorated = (map(keyed, stream) for stream in iterables)\n"
            "            pairs = merge(*decorated, key=itemgetter(0), reverse=reverse)\n"
            "\n"
            "        for _, group in groupby(pairs, key=itemgetter(0)):\n"
            "            for _, chosen in group:\n"
            "                if not keep_last:\n"
            "                    break\n"
            "            yield chosen\n"
            "        return\n"
            "\n"
            "    h = []\n"
            "    h_append = h.append",
        ),
    ),
    "multiturn-configparser-items": (
        (
            "    def items(self, section=_UNSET, raw=False, vars=None):",
            "    def items(self, section=_UNSET, raw=False, vars=None, *,\n"
            "              include_defaults=True, include_vars=False):",
        ),
        (
            "        The section DEFAULT is special.\n"
            "        \"\"\"\n"
            "        if section is _UNSET:\n"
            "            return super().items()\n"
            "        d = self._defaults.copy()\n"
            "        try:\n"
            "            d.update(self._sections[section])\n"
            "        except KeyError:\n"
            "            if section != self.default_section:\n"
            "                raise NoSectionError(section)\n"
            "        orig_keys = list(d.keys())\n"
            "        # Update with the entry specific variables\n"
            "        if vars:\n"
            "            for key, value in vars.items():\n"
            "                d[self.optionxform(key)] = value",
            "        The section DEFAULT is special.\n"
            "\n"
            "        include_defaults 仅控制是否枚举继承项，默认项仍可参与插值。\n"
            "        include_vars 为真时在节的键之后追加 vars 新键；省略 section 时忽略两项。\n"
            "        \"\"\"\n"
            "        if section is _UNSET:\n"
            "            return super().items()\n"
            "\n"
            "        d = self._defaults.copy()\n"
            "        try:\n"
            "            section_data = self._sections[section]\n"
            "        except KeyError:\n"
            "            if section != self.default_section:\n"
            "                raise NoSectionError(section)\n"
            "            section_data = self._defaults\n"
            "        else:\n"
            "            d.update(section_data)\n"
            "\n"
            "        # 返回的键顺序独立于插值数据，只追加显式要求列出的覆盖键。\n"
            "        orig_keys = dict.fromkeys(d if include_defaults else section_data)\n"
            "        if vars:\n"
            "            for key, value in vars.items():\n"
            "                option = self.optionxform(key)\n"
            "                d[option] = value\n"
            "                if include_vars:\n"
            "                    orig_keys.setdefault(option)",
        ),
    ),
}


def apply_solution(case_id: str, source: str) -> str:
    """将指定题的替换应用于源码文本，返回参考版本。

    case_id 是 cases.py 中的题号，source 是对应冻结源码；若目标原文不唯一，
    抛出 ValueError 并保留调用方的原件。未登记的题号抛出 KeyError。
    """
    for original, replacement in _REPLACEMENTS[case_id]:
        if source.count(original) != 1:
            raise ValueError(f"参考解找不到唯一原文：{case_id}")
        source = source.replace(original, replacement, 1)
    return source
