"""用独立行为检查核验四道题的冻结原件、参考改动或实际候选。"""

import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
from itertools import islice
import json
import ntpath
from pathlib import Path
import posixpath
import sys
from types import ModuleType, SimpleNamespace
import unittest

# 独立运行脚本和从批次入口导入时，均使用同一份冻结题目。
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multiturn.cases import CASES


# 按批次给出的题号定位需从候选目录加载的标准库文件。
_CASE_BY_ID = {case["id"]: case for case in CASES}


def _same(actual, expected) -> None:
    """比较实际结果 actual 与独立写定的 expected。不返回数据，差异以异常报告。"""
    if actual != expected:
        raise AssertionError(f"期望 {expected!r}，实际 {actual!r}")


def _indent_legacy(module: ModuleType) -> None:
    """检查候选 module 的旧签名、默认谓词与原样换行，失败抛出断言错误。"""
    _same(module.indent("first\r\n \t\r\nlast", ">"), ">first\r\n \t\r\n>last")
    _same(module.indent("", ">"), "")


def _indent_first(module: ModuleType) -> None:
    """检查候选 module 的首个选中行支持空前缀，失败抛出断言错误。"""
    source = "skip\r\nfirst\r\nsecond"

    def predicate(line: str) -> bool:
        """跳过以 skip 开头的原行，其他行参与本次选择。"""
        return not line.startswith("skip")

    _same(module.indent(source, ">", predicate, first_prefix=""), "skip\r\nfirst\r\n>second")
    _same(module.indent(source, ">", predicate, first_prefix=None), "skip\r\n>first\r\n>second")
    _same(module.indent("\nfirst", ">", bool, first_prefix="!"), "!\n>first")


def _indent_restart(module: ModuleType) -> None:
    """检查候选 module 的多段重启、选中空白行和回调输入，失败抛出断言错误。"""
    _same(module.indent("one\n\ntwo", ">", restart_on_blank=True), ">one\n\n>two")
    source = "\r\nskip\r\nfirst\r\n \t\r\n\t\nskip\nsecond\nthird"
    calls = []

    def predicate(line: str) -> bool:
        """记录收到的原行并跳过 skip，空白行也参与选择。"""
        calls.append(line)
        return not line.startswith("skip")

    actual = module.indent(source, ">", predicate, first_prefix="!", restart_on_blank=True)
    _same(actual, ">\r\nskip\r\n!first\r\n> \t\r\n>\t\nskip\n!second\n>third")
    _same(calls, source.splitlines(keepends=True))
    _same(module.indent("one\n\ntwo", ">", first_prefix="", restart_on_blank=True), "one\n\ntwo")


def _filter_legacy(module: ModuleType) -> None:
    """用候选 module 的旧签名核对顺序、重复项及 bytes 模式，失败抛出断言错误。"""
    _same(module.filter(iter(["note.txt", "x.py", "note.txt"]), "*.txt"), ["note.txt", "note.txt"])
    _same(module.filter([b"draft.txt", b"x.py"], b"*.txt"), [b"draft.txt"])


def _filter_exclude(module: ModuleType) -> None:
    """检查候选 module 的一次性迭代和排除结果，失败抛出断言错误。"""
    names = iter(["final.txt", "draft_1.txt", "draft_1.txt", "old.txt", "final.txt"])
    _same(module.filter(names, "*.txt", exclude=iter(["draft_*", "old.*"])), ["final.txt", "final.txt"])
    _same(module.filter([b"\xe9.txt", b"draft.txt"], b"*.txt", exclude=(b"draft.*",)), [b"\xe9.txt"])


def _filter_keep(module: ModuleType) -> None:
    """在隔离候选 module 验证两种平台的恢复规则，失败抛出断言错误。"""
    _same(module.filter(["stable.txt"], "*.txt", keep=("*.md",)), ["stable.txt"])
    original_os = module.os
    try:
        module.os = SimpleNamespace(path=ntpath)
        names = iter(["Ready.TXT", r"Draft\KEEP.TXT", r"Draft\DROP.TXT", "README.md", r"Draft\KEEP.TXT"])
        actual = module.filter(
            names,
            "*.txt",
            exclude=iter(["draft/*"]),
            keep=iter(["DRAFT/keep.txt", "*.md"]),
        )
        _same(actual, ["Ready.TXT", r"Draft\KEEP.TXT", r"Draft\KEEP.TXT"])
        _same(module.filter([b"DRAFT.TXT"], b"*.txt", exclude=(b"*",), keep=(b"draft.*",)), [b"DRAFT.TXT"])
        module.os = SimpleNamespace(path=posixpath)
        _same(module.filter(["FILE.TXT", "a.txt"], "*.txt", exclude=("*",), keep=("*",)), ["a.txt"])
    finally:
        module.os = original_os


def _merge_legacy(module: ModuleType) -> None:
    """检查候选 module 的旧排序、重复值和逆序行为，失败抛出断言错误。"""
    _same(list(module.merge([1, 3], [1, 2])), [1, 1, 2, 3])
    _same(list(module.merge([3, 1], [2, 1], reverse=True)), [3, 2, 1, 1])


def _merge_first(module: ModuleType) -> None:
    """检查候选 module 的首项、尾段去重和 key 调用上界，失败抛出断言错误。"""
    left = [(1, "l1"), (2, "l2")]
    right = [(1, "r1"), (3, "r3a"), (3, "r3b"), (4, "r4")]
    key_calls = Counter()

    def key(value: tuple[int, str]) -> int:
        """返回用于排序的数字，同时记录每个原始元素的求值次数。"""
        index, label = value
        key_calls[label] += 1
        return index

    actual = list(module.merge(iter(left), iter(right), key=key, unique=True))
    _same(actual, [(1, "l1"), (2, "l2"), (3, "r3a"), (4, "r4")])
    _same(key_calls, Counter(label for _, label in left + right))
    _same(list(module.merge(iter([1, 1, 2, 2]), unique=True)), [1, 2])


def _merge_last(module: ModuleType) -> None:
    """检查候选 module 的逆序末项与惰性消费，失败抛出断言错误。"""
    left = [(3, "left-first"), (3, "left-last"), (2, "left-2")]
    right = [(3, "right-3"), (1, "right-first"), (1, "right-last")]

    def key(value: tuple[int, str]) -> int:
        """使用记录第一项分组，标签仅用于识别被选中的原值。"""
        return value[0]

    _same(list(module.merge(left, key=key, keep_last=True)), left)
    actual = list(module.merge(left, right, key=key, reverse=True, unique=True, keep_last=True))
    _same(actual, [(3, "right-3"), (2, "left-2"), (1, "right-last")])
    _same(list(module.merge([], unique=True, keep_last=True)), [])

    def bounded_input():
        """产生足以输出三组的输入，提前读完全部内容时带原因报错。"""
        yield from (1, 1, 2, 2, 3, 3, 4, 4)
        raise AssertionError("取前三组时不应消费整条输入")

    first_three = list(islice(module.merge(bounded_input(), unique=True, keep_last=True), 3))
    _same(first_three, [1, 2, 3])


def _parser(module: ModuleType, *, use_raw_parser: bool = False):
    """从候选 module 返回装入同一短配置的解析器，默认支持插值。

    use_raw_parser 为真时返回未启用插值的 RawConfigParser 实例。
    """
    parser_class = module.RawConfigParser if use_raw_parser else module.ConfigParser
    parser = parser_class()
    parser.read_string("[DEFAULT]\nbase=/shared\ninherited=on\nsecond=default\n"
                       "[active]\nfirst=%(BASE)s/%(EXTRA)s\nsecond=local\n")
    return parser


def _items_legacy(module: ModuleType) -> None:
    """验证候选 module 的旧字典顺序、vars 和映射视图，失败抛出断言错误。"""
    config = _parser(module)
    actual = config.items("active", False, {"EXTRA": "once"})
    _same(actual, [("base", "/shared"), ("inherited", "on"), ("second", "local"), ("first", "/shared/once")])
    _same([section for section, _ in config.items()], [config.default_section, "active"])


def _items_direct(module: ModuleType) -> None:
    """验证候选 module 仅枚举本节键并保留插值，失败抛出断言错误。"""
    config = _parser(module)
    actual = config.items("active", vars={"EXTRA": "once"}, include_defaults=False)
    _same(actual, [("first", "/shared/once"), ("second", "local")])
    _same(config.items(config.default_section, include_defaults=False),
          [("base", "/shared"), ("inherited", "on"), ("second", "default")])


def _items_with_vars(module: ModuleType) -> None:
    """检查候选 module 的变量顺序、规范化冲突与原值，失败抛出断言错误。"""
    config = _parser(module)
    default_section = config.items(config.default_section, vars={"NEW": "value"}, include_vars=True)
    _same(default_section[-1], ("new", "value"))
    variables = {"BASE": "/override", "EXTRA": "first", "Extra": "last", "Second": "override"}
    actual = config.items("active", vars=variables, include_defaults=False, include_vars=True)
    _same(actual, [("first", "/override/last"), ("second", "override"),
                   ("base", "/override"), ("extra", "last")])
    raw = config.items("active", raw=True, vars=variables, include_defaults=False, include_vars=True)
    _same(raw, [("first", "%(BASE)s/%(EXTRA)s"), ("second", "override"),
                ("base", "/override"), ("extra", "last")])
    _same([section for section, _ in config.items(vars=variables, include_vars=True)],
          [config.default_section, "active"])
    raw_config = _parser(module, use_raw_parser=True)
    _same(raw_config.items("active", vars={"NEW": "value"}, include_defaults=False, include_vars=True),
          [("first", "%(BASE)s/%(EXTRA)s"), ("second", "local"), ("new", "value")])
    with unittest.TestCase().assertRaises(module.NoSectionError):
        config.items("missing", include_defaults=False, include_vars=True)


# 每题按旧行为、首轮要求、末轮变更分开核验，避免仅检验接口签名。
_PROBES = {
    "multiturn-textwrap-indent": (_indent_legacy, _indent_first, _indent_restart),
    "multiturn-fnmatch-filter": (_filter_legacy, _filter_exclude, _filter_keep),
    "multiturn-heapq-merge": (_merge_legacy, _merge_first, _merge_last),
    "multiturn-configparser-items": (_items_legacy, _items_direct, _items_with_vars),
}

# 使用原签名拒绝的新参数，保证基线因缺功能失败，而不是导入或环境异常。
_MISSING_OPTIONS = {
    "multiturn-textwrap-indent": ("first_prefix", "restart_on_blank"),
    "multiturn-fnmatch-filter": ("exclude", "keep"),
    "multiturn-heapq-merge": ("unique", "keep_last"),
    "multiturn-configparser-items": ("include_defaults", "include_vars"),
}


def _load(source: str | bytes, path: Path, variant: str) -> ModuleType:
    """在独立模块作用域运行可信源码，返回可供公开行为检查的模块。

    source 为原件、参考代码或候选文件内容，path 用于错误定位。
    variant 区分三种来源。函数不覆盖系统模块、不写入标准库原件。
    """
    module = ModuleType(f"_checkpoint_{variant}_{path.stem}")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _check(probe, module: ModuleType) -> dict:
    """用 probe 检查候选 module，返回断言名称、通过状态和实际异常。"""
    record = {"probe": probe.__name__, "passed": True}
    try:
        probe(module)
    except Exception as exc:
        record.update(passed=False, error_type=type(exc).__name__, error=str(exc))
    return record


def _precheck_case(case: dict) -> dict:
    """按 case 的题号和原件路径执行行为检查，返回单题可审计记录。"""
    from multiturn.reference_solutions import apply_solution

    source_path = case["workspace"] / case["evidence_file"]
    source_bytes = source_path.read_bytes()
    source = source_path.read_text(encoding="utf-8")
    amended = apply_solution(case["id"], source)
    baseline = _load(source, source_path, "baseline")
    reference = _load(amended, source_path, "reference")
    probes = _PROBES[case["id"]]
    before = [_check(probe, baseline) for probe in probes]
    after = [_check(probe, reference) for probe in probes]
    baseline_rejected_options = all(
        record.get("error_type") == "TypeError" and repr(option) in record.get("error", "")
        for record, option in zip(before[1:], _MISSING_OPTIONS[case["id"]], strict=True)
    )

    history = {relative: (case["workspace"] / relative).stat().st_size
               for relative in case["history_files"]}
    anchor_counts = {
        relative: (case["workspace"] / relative).read_text(encoding="utf-8").count(case["anchor"])
        for relative in [case["evidence_file"], *history]
    }
    evidence_unique = anchor_counts[case["evidence_file"]] == 1 and sum(anchor_counts.values()) == 1
    enough_history = sum(history.values()) >= 310 * 1024
    return {
        "id": case["id"],
        "source": case["source"],
        "evidence_file": case["evidence_file"],
        "source_sha256": sha256(source_bytes).hexdigest(),
        "reference_normalized_utf8_sha256": sha256(amended.encode("utf-8")).hexdigest(),
        "anchor_locations": {relative: count for relative, count in anchor_counts.items() if count},
        "history_bytes": sum(history.values()),
        "history_files": len(history),
        "baseline": before,
        "reference": after,
        "passed": evidence_unique and enough_history and before[0]["passed"] and baseline_rejected_options
        and all(record["passed"] for record in after),
    }


def _grade_error(report: dict, status: str, exc: BaseException) -> dict:
    """为 report 补充执行异常并返回，status 指明候选错误或评分环境错误。"""
    return {**report, "status": status, "error_type": type(exc).__name__, "error": str(exc)}


def grade_candidate(case_id: str, candidate: Path) -> dict:
    """用已冻结的行为断言给实际候选目录评分。

    case_id 为冻结的多轮题号。candidate 为候选的标准库根目录，必须存在；
    只从该目录读取题目对应的源码文件。返回通过状态、源码哈希和各组检查结果，
    并区分行为失败、候选无法加载和评分环境异常。代码只在当前进程的独立模块中加载。
    """
    report = {
        "mode": "candidate",
        "case_id": case_id,
        "passed": False,
        "checks": [],
        "passed_checks": 0,
        "total_checks": 0,
    }
    case = _CASE_BY_ID.get(case_id)
    if case is None:
        return _grade_error(report, "scorer_error", ValueError(f"未登记的题号：{case_id}"))

    try:
        root = candidate.resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(candidate))
    except (OSError, RuntimeError) as exc:
        return _grade_error(report, "scorer_error", exc)

    report["candidate_file"] = case["evidence_file"]
    try:
        path = (root / case["evidence_file"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"候选目录内缺少普通源码文件：{case['evidence_file']}")
        source = path.read_bytes()
    except (FileNotFoundError, ValueError) as exc:
        return _grade_error(report, "candidate_error", exc)
    except (OSError, RuntimeError) as exc:
        return _grade_error(report, "scorer_error", exc)

    report["candidate_sha256"] = sha256(source).hexdigest()
    # 候选代码的普通输出不进入批次接收的评分 JSON。
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        try:
            module = _load(source, path, "candidate")
        except (Exception, SystemExit) as exc:
            return _grade_error(report, "candidate_error", exc)
        try:
            checks = [_check(probe, module) for probe in _PROBES[case_id]]
        except SystemExit as exc:
            return _grade_error(report, "candidate_error", exc)
    passed_checks = sum(check["passed"] for check in checks)
    passed = passed_checks == len(checks)
    return {
        **report,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "checks": checks,
        "passed_checks": passed_checks,
        "total_checks": len(checks),
    }


def main() -> int:
    """运行四题预检或一个候选评分，返回 0 通过、1 失败或 2 评分环境异常。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=_CASE_BY_ID, help="只评分这个多轮题号")
    parser.add_argument("--candidate", type=Path, help="待评分候选的标准库根目录")
    parser.add_argument("--output", type=Path, help="同时保存报告的 UTF-8 JSON 路径")
    options = parser.parse_args()
    if (options.case is None) != (options.candidate is None):
        parser.error("--case 与 --candidate 须同时提供")
    if options.case is None:
        reports = [_precheck_case(case) for case in CASES]
        report = {
            "mode": "no_model",
            "model_requests": 0,
            "python": sys.version.split()[0],
            "passed": all(item["passed"] for item in reports),
            "cases": reports,
        }
    else:
        report = grade_candidate(options.case, options.candidate)
    if options.output:
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 候选评分的标准输出供批次解析，避免 Windows 本地代码页影响 JSON 字符。
    print(json.dumps(report, ensure_ascii=options.case is not None, indent=2))
    if report["passed"]:
        return 0
    return 2 if report.get("status") == "scorer_error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
