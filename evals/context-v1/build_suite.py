"""生成自建上下文实验夹具；这是合成开发任务，不能报告为真实仓库或公开基准成绩。"""

import json
from pathlib import Path

# 每项包含职责、参考实现和边界断言；按题目划分开发/测试，模型副本不含参考解及评分脚本。
TASKS = [
    ("textkit", "words", "按任意空白分词，忽略空串，保留原顺序。", "return text.split()", "text", [([" a\t中\n b "], ["a", "中", "b"]), ([""], [])]),
    ("textkit", "unique", "大小写敏感地去重词列表，保留首次出现顺序。", "return list(dict.fromkeys(items))", "items", [([["a", "b", "a", "A"]], ["a", "b", "A"]), ([[]], [])]),
    ("textkit", "counts", "统计字符串列表各值次数，返回字典。", "from collections import Counter\nreturn dict(Counter(items))", "items", [([["中", "中", "a"]], {"中": 2, "a": 1}), ([[]], {})]),
    ("ledger", "sum_cents", "把十进制金额字符串列表求和，返回整数分；输入至多两位小数。", "from decimal import Decimal\nreturn int(sum((Decimal(x) for x in items), Decimal(0)) * 100)", "items", [([["0.10", "0.20", "-0.01"]], 29), ([[]], 0)]),
    ("ledger", "format_money", "把整数分格式化为元字符串，严格保留两位小数。", "from decimal import Decimal\nreturn format(Decimal(cents) / 100, '.2f')", "cents", [([125], "1.25"), ([-1], "-0.01"), ([0], "0.00")]),
    ("ledger", "balance", "汇总每个账户整数金额；每条为account与amount字段，结果保留零余额账户。", "result = {}\nfor row in rows:\n    key = row['account']\n    result[key] = result.get(key, 0) + row['amount']\nreturn result", "rows", [([[{"account": "a", "amount": 3}, {"account": "a", "amount": -3}]], {"a": 0}), ([[]], {})]),
    ("eventflow", "latest", "返回每个id时间戳ts最大的事件；同时间后出现的覆盖前者，结果按id排序。", "result = {}\nfor row in rows:\n    if row['id'] not in result or row['ts'] >= result[row['id']]['ts']:\n        result[row['id']] = row\nreturn [result[k] for k in sorted(result)]", "rows", [([[{"id": "a", "ts": 2}, {"id": "a", "ts": 1}]], [{"id": "a", "ts": 2}]), ([[]], [])]),
    ("eventflow", "batches", "把items切成最多size个元素的连续批次；size为正整数，空输入返回空列表。", "return [items[i:i + size] for i in range(0, len(items), size)]", "items, size", [([[1, 2, 3], 2], [[1, 2], [3]]), ([[], 3], [])]),
    ("eventflow", "retry", "返回第attempt次重试延迟min(base*2**attempt, cap)，attempt从0开始，所有参数非负。", "return min(base * 2 ** attempt, cap)", "base, attempt, cap", [([2, 3, 10], 10), ([2, 0, 10], 2)]),
    ("textkit", "slug", "把字符串转小写，将连续非ASCII字母数字替换为一个短横线，移除两端短横线。", "import re\nreturn re.sub('[^a-z0-9]+', '-', text.lower()).strip('-')", "text", [([" A + B!!"], "a-b"), (["中文"], "")]),
    ("textkit", "normalize", "采用Unicode NFKC标准化并casefold，前后去空白。", "import unicodedata\nreturn unicodedata.normalize('NFKC', text).casefold().strip()", "text", [([" ＡStraße "], "astrasse"), ([""], "")]),
    ("textkit", "csv_row", "用标准CSV规则解析一行，支持逗号引号和双引号转义，返回字段列表。", "import csv\nreturn next(csv.reader([text]))", "text", [(['a,"b,c","d""e"'], ["a", "b,c", 'd"e']), (["a,,b"], ["a", "", "b"])]),
    ("textkit", "query", "解析URL查询字符串为键到所有值的列表，保留空值并解码加号与百分号。", "from urllib.parse import parse_qs\nreturn parse_qs(text, keep_blank_values=True)", "text", [(["a=1&a=2&b=&q=a+b"], {"a": ["1", "2"], "b": [""], "q": ["a b"]}), ([""], {})]),
    ("textkit", "suffixes", "从路径列表取最后扩展名的小写值，省略无扩展名路径，保留重复项和顺序。", "from pathlib import PurePosixPath\nreturn [PurePosixPath(x).suffix.lower() for x in paths if PurePosixPath(x).suffix]", "paths", [([["a.TXT", "foo", "a.tar.gz", ".gitignore"]], [".txt", ".gz"]), ([[]], [])]),
    ("textkit", "lines", "统一CRLF/CR为LF，删除空白行，保留非空行的原始空白。", "return [line for line in text.replace('\\r\\n', '\\n').replace('\\r', '\\n').split('\\n') if line.strip()]", "text", [([" a\r\n\r b \n"], [" a", " b "]), (["  "], [])]),
    ("textkit", "truncate", "按Unicode字符截断到limit总长度；超长时末尾加省略号…且计入总长度，limit为非负整数。", "return text if len(text) <= limit else (text[:limit - 1] + '…' if limit else '')", "text, limit", [(["中文abc", 3], "中文…"), (["abc", 0], ""), (["abc", 3], "abc")]),
    ("textkit", "html", "HTML转义文本，含单双引号。", "import html\nreturn html.escape(text, quote=True)", "text", [(['<a "x">&\''], '&lt;a &quot;x&quot;&gt;&amp;&#x27;'), (["中"], "中")]),
    ("textkit", "flatten", "把嵌套列表递归展开；其他值包括None、字符串不拆分。", "out = []\nfor value in items:\n    out.extend(solve(value) if isinstance(value, list) else [value])\nreturn out", "items", [([[1, [2, [], [3]], None, "ab"]], [1, 2, 3, None, "ab"]), ([[]], [])]),
    ("textkit", "overlap", "统计子串pattern重叠出现次数；空pattern返回0。", "return sum(text.startswith(pattern, i) for i in range(len(text))) if pattern else 0", "text, pattern", [(["aaaa", "aa"], 3), (["abc", ""], 0)]),
    ("textkit", "group_prefix", "按首个/之前的前缀分组字符串；无/则用整个字符串，保留每组输入顺序。", "out = {}\nfor item in items:\n    out.setdefault(item.split('/', 1)[0], []).append(item)\nreturn out", "items", [([["a/x", "b", "a/y"]], {"a": ["a/x", "a/y"], "b": ["b"]}), ([[]], {})]),
    ("ledger", "round_money", "把金额字符串按四舍五入ROUND_HALF_UP保留两位小数，返回字符串，支持负数。", "from decimal import Decimal, ROUND_HALF_UP\nreturn str(Decimal(text).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))", "text", [(["1.005"], "1.01"), (["-1.005"], "-1.01"), (["0"], "0.00")]),
    ("ledger", "allocate", "将非负整数total均分到正整数n份，余数从第一份起加1。", "q, r = divmod(total, n)\nreturn [q + (i < r) for i in range(n)]", "total, n", [([10, 3], [4, 3, 3]), ([2, 4], [1, 1, 0, 0])]),
    ("ledger", "discount", "整数分price按整数百分比percent折扣；返回折后整数分，半分向上取整。", "from decimal import Decimal, ROUND_HALF_UP\nreturn int((Decimal(price) * (100 - percent) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))", "price, percent", [([101, 50], 51), ([100, 100], 0)]),
    ("ledger", "tax", "给定整数分net与整数百分比rate，税额半分向上取整后加回net。", "from decimal import Decimal, ROUND_HALF_UP\nreturn net + int((Decimal(net) * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))", "net, rate", [([105, 10], 116), ([100, 0], 100)]),
    ("ledger", "totals", "按category汇总price乘qty，字段为整数，保留负qty退货。", "out = {}\nfor row in rows:\n    key = row['category']\n    out[key] = out.get(key, 0) + row['price'] * row['qty']\nreturn out", "rows", [([[{"category": "a", "price": 12, "qty": 2}, {"category": "a", "price": 12, "qty": -1}]], {"a": 12}), ([[]], {})]),
    ("ledger", "running", "返回整数交易列表每一步的累计余额，起始为initial。", "out = []\nfor amount in amounts:\n    initial += amount\n    out.append(initial)\nreturn out", "amounts, initial", [([[3, -4, 2], 10], [13, 9, 11]), ([[], 1], [])]),
    ("ledger", "reconcile", "合并两个余额字典，输出仅非零差额left减right，键不限于交集。", "return {k: left.get(k, 0) - right.get(k, 0) for k in left.keys() | right.keys() if left.get(k, 0) != right.get(k, 0)}", "left, right", [([{"a": 2, "b": 1}, {"a": 2, "c": 3}], {"b": 1, "c": -3}), ([{}, {}], {})]),
    ("ledger", "top", "按金额降序、同金额名称升序取前n个(name,amount)记录，返回列表的列表。", "return [list(x) for x in sorted(items, key=lambda x: (-x[1], x[0]))[:n]]", "items, n", [([[["b", 3], ["a", 3], ["c", 1]], 2], [["a", 3], ["b", 3]]), ([[], 2], [])]),
    ("ledger", "overdue", "返回due日期早于today且paid为False的id，按输入顺序；日期都是ISO YYYY-MM-DD。", "return [row['id'] for row in rows if not row['paid'] and row['due'] < today]", "rows, today", [([[{"id": "a", "due": "2026-01-01", "paid": False}, {"id": "b", "due": "2026-01-02", "paid": False}], "2026-01-02"], ["a"]), ([[], "2026-01-01"], [])]),
    ("ledger", "median", "返回非空整数列表的中位数；偶数长度返回中间两值平均数，不修改输入。", "values = sorted(items)\nn = len(values)\nreturn values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2", "items", [([[3, 1, 2]], 2), ([[1, 4, 2, 3]], 2.5)]),
    ("eventflow", "dedup", "按事件id去重，保留第一次出现的完整事件和输入顺序。", "seen = set()\nout = []\nfor row in rows:\n    if row['id'] not in seen:\n        seen.add(row['id'])\n        out.append(row)\nreturn out", "rows", [([[{"id": "a", "v": 1}, {"id": "a", "v": 2}]], [{"id": "a", "v": 1}]), ([[]], [])]),
    ("eventflow", "window", "返回时间戳ts属于[start,end)的事件，保持顺序。", "return [row for row in rows if start <= row['ts'] < end]", "rows, start, end", [([[{"ts": 1}, {"ts": 2}, {"ts": 3}], 1, 3], [{"ts": 1}, {"ts": 2}]), ([[], 0, 1], [])]),
    ("eventflow", "histogram", "以width宽度分桶整数时间戳，返回桶起点字符串到数量；负数向下取整，width为正。", "out = {}\nfor value in times:\n    key = str(value // width * width)\n    out[key] = out.get(key, 0) + 1\nreturn out", "times, width", [([[-1, 0, 9, 10], 10], {"-10": 1, "0": 2, "10": 1}), ([[], 1], {})]),
    ("eventflow", "merge", "合并闭区间，端点相接也合并；输入无序，返回按起点排序的区间列表。", "out = []\nfor start, end in sorted(intervals):\n    if out and start <= out[-1][1]:\n        out[-1][1] = max(out[-1][1], end)\n    else:\n        out.append([start, end])\nreturn out", "intervals", [([[[3, 5], [1, 3], [8, 9]]], [[1, 5], [8, 9]]), ([[]], [])]),
    ("eventflow", "diff", "比较两个字典，返回added/removed/changed键列表，均按名称排序；changed只含共同键且值不同。", "return {'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()), 'changed': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}", "before, after", [([{"a": 1, "b": 2}, {"a": 2, "c": 3}], {"added": ["c"], "removed": ["b"], "changed": ["a"]}), ([{}, {}], {"added": [], "removed": [], "changed": []})]),
    ("eventflow", "priority", "按priority降序、created升序排列任务，完全相同保持原顺序。", "return sorted(rows, key=lambda row: (-row['priority'], row['created']))", "rows", [([[{"priority": 1, "created": 2}, {"priority": 2, "created": 3}]], [{"priority": 2, "created": 3}, {"priority": 1, "created": 2}]), ([[]], [])]),
    ("eventflow", "expiry", "给定now整数秒，筛出expires_at为None或严格大于now的记录，保留顺序。", "return [row for row in rows if row['expires_at'] is None or row['expires_at'] > now]", "rows, now", [([[{"expires_at": None}, {"expires_at": 2}, {"expires_at": 3}], 2], [{"expires_at": None}, {"expires_at": 3}]), ([[], 1], [])]),
    ("eventflow", "dependencies", "返回依赖deps都在completed中的任务id，排除自身已完成项，保持输入顺序。", "return [row['id'] for row in rows if row['id'] not in completed and set(row['deps']).issubset(completed)]", "rows, completed", [([[{"id": "a", "deps": []}, {"id": "b", "deps": ["a"]}], ["a"]], ["b"]), ([[], []], [])]),
    ("eventflow", "paginate", "对items从offset起取limit条，返回items和next_offset；取完后next_offset=None；offset非负、limit为正。", "page = items[offset:offset + limit]\nreturn {'items': page, 'next_offset': offset + len(page) if offset + len(page) < len(items) else None}", "items, offset, limit", [([[1, 2, 3], 0, 2], {"items": [1, 2], "next_offset": 2}), ([[1], 5, 2], {"items": [], "next_offset": None})]),
    ("eventflow", "redact", "递归处理字典与列表，将键名大小写不敏感为password/token的值替换为***，不修改其他值。", "if isinstance(value, dict):\n    return {k: '***' if k.lower() in {'password', 'token'} else solve(v) for k, v in value.items()}\nif isinstance(value, list):\n    return [solve(v) for v in value]\nreturn value", "value", [([{"Password": "secret", "rows": [{"token": "x", "n": 1}]}], {"Password": "***", "rows": [{"token": "***", "n": 1}]}), ([None], None)]),
]

# 生成函数文档的业务参数说明；任务需求进一步约束输入范围，全部参数均为必填。
PARAMETERS = {
    "text": "待处理字符串，空串按上述需求处理", "items": "按上述元素约束提供的输入列表，允许空列表",
    "cents": "整数分金额，可为零或负数", "rows": "含上述字段的记录列表，允许空列表",
    "size": "每批元素数，正整数", "base": "重试基础延迟，非负且与cap同单位",
    "attempt": "从0开始的重试序号，非负整数", "cap": "延迟上限，非负且与base同单位",
    "limit": "返回元素或字符数上限，范围按上述需求", "pattern": "需计数的子串，空串按需求返回0",
    "paths": "路径字符串列表，允许空列表", "total": "待分配的非负整数总量",
    "n": "分配份数或排名上限，范围按上述需求", "price": "折扣前整数分金额",
    "percent": "折扣百分数，100表示全部折扣", "net": "税前整数分金额",
    "rate": "整数税率百分数", "amounts": "可正可负的整数交易额列表",
    "initial": "第一笔交易之前的整数余额", "left": "差额计算的左侧余额字典",
    "right": "差额计算的右侧余额字典", "today": "用于比较的ISO日期YYYY-MM-DD",
    "times": "整数时间戳列表，允许负数", "width": "时间桶宽度，正整数且与时间戳同单位",
    "intervals": "起点不大于终点的闭区间列表", "before": "变更前字典", "after": "变更后字典",
    "now": "当前时间，整数秒", "completed": "已完成任务id列表，允许空列表",
    "offset": "从0开始的分页起点，非负整数", "value": "待脱敏的任意值，None及非容器值原样返回",
    "start": "时间窗口起点，包含边界且与事件ts同单位", "end": "时间窗口终点，不包含边界且与事件ts同单位",
}


def build() -> None:
    """生成当前目录下40个夹具、完整清单和单题复验清单；无参数/网络/模型调用，返回None，覆盖生成文件。"""
    root = Path(__file__).resolve().parent
    cases = []
    for index, (project, name, requirement, code, signature, checks) in enumerate(TASKS):
        identifier = project + "-" + name
        # 评分器要求的返回容器必须写进需求，避免把合法的另一种表示误判为模型失败。
        shape = {"ledger-balance": "字典：账户名为键、汇总整数金额为值",
                 "ledger-totals": "字典：category为键、汇总金额为值",
                 "textkit-group_prefix": "字典：前缀为键、该组字符串列表为值"}.get(identifier)
        kind = type(checks[0][1]).__name__
        requirement += " 返回结构必须为" + (shape or ("数值（int或float）" if kind in {"int", "float"} else kind)) + "。"
        prefix = '"""自建任务模块；输入输出契约见函数说明。"""\n\n'
        parameters = "；".join(parameter + "：" + PARAMETERS[parameter] for parameter in signature.split(", "))
        declaration = f'def solve({signature}):\n    """{requirement}\n\n    参数（均为必填）：{parameters}。\n    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。\n    """\n'
        reference = prefix + declaration + ''.join('    ' + line + '\n' for line in code.splitlines())
        initial = prefix + declaration + '    raise NotImplementedError("待实现")\n'
        # 普通/长资料交错，所有资料是明确的输入，不能把强制读长文本当生产能力证明。
        long = index % 3 == 2
        readme = '# ' + project + '\n\n' + requirement + '\n'
        for variant, content in (("source", initial), ("reference", reference)):
            directory = root / "cases" / identifier / variant
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "subject.py").write_text(content, encoding="utf-8")
            (directory / "README.md").write_text(readme, encoding="utf-8")
            if long:
                (directory / "history.txt").write_text(('历史记录：旧接口已废弃；本次行为以README为准。\n' * 700) + requirement, encoding="utf-8")
        grader = ('"""独立检查业务输出；参数为被验收副本目录，失败退出非零。"""\n'
                  'import importlib.util\nimport sys\nfrom pathlib import Path\n'
                  'spec = importlib.util.spec_from_file_location("subject", Path(sys.argv[1]) / "subject.py")\n'
                  'subject = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(subject)\n')
        for args, expected in checks:
            grader += f'assert subject.solve(*{args!r}) == {expected!r}\n'
        (root / "cases" / identifier / "grader.py").write_text(grader, encoding="utf-8")
        cases.append({"id": identifier, "project": project, "category": "long_input" if long else "implementation",
                      "split": "dev" if index < 10 else "test", "prompt": '实现subject.py的solve函数。' + requirement +
                      (' 请查阅history.txt及README，识别当前要求。' if long else '') + ' 可自行测试，直接完成这一范围明确的小任务。',
                      "source": f"cases/{identifier}/source", "reference": f"cases/{identifier}/reference", "grader": f"cases/{identifier}/grader.py"})
    suite = {"name": "上下文策略合成校准集", "version": "2", "provenance": "40个自建Python功能夹具，三个示例领域，10开发/30测试；不是三个真实仓库，也不是SWE-bench成绩。用于评分链路校准，正式面试结论需另接未见真实仓库任务。v2明确返回结构，v1存在题意歧义的对照无效。", "cases": cases}
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8")
    recheck = {**suite, "name": "返回契约修正复验", "provenance": "仅复验ledger-balance的明确返回契约，1题2策略；不用于发布总体提升。",
               "cases": [case for case in cases if case["id"] == "ledger-balance"]}
    (root / "recheck.json").write_text(json.dumps(recheck, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    build()
