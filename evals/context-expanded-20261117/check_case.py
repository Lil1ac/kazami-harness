"""在隔离容器中导入候选源码并执行工作区之外预登记的行为断言。"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

def load_subject(case: dict, root: Path, dependencies: Path) -> ModuleType:
    """加载 case 指向的候选模块；root 与 dependencies 是隔离挂载。"""
    name = case["module"]
    sys.path[:0] = [str(root / "src"), str(root), str(dependencies)]
    if case["project"] != "cpython":
        module = importlib.import_module(name)
    else:
        location = root / "Lib" / (name + ".py")
        spec = importlib.util.spec_from_file_location("checkpoint_" + name, location)
        if spec is None or spec.loader is None:
            raise RuntimeError("未找到待评分的标准库模块")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    if not isinstance(module.__file__, str) or not Path(module.__file__).resolve().is_relative_to(root):
        raise RuntimeError("待评分模块不是当前候选源码")
    return module


def evaluate(identifier: str, root: Path, dependencies: Path) -> int:
    """按 identifier 执行行为验收，返回通过 0 或题目不符 1；日志只描述结果类别。"""
    cases = json.loads(Path(__file__).with_name("cases-private.json").read_bytes())
    case = cases[identifier]
    try:
        module = load_subject(case, root, dependencies)
        compiled = compile(case["check"], "<独立行为评分>", "exec")
        exec(compiled, {"m": module})
    except BaseException as error:
        print(json.dumps({"passed": False, "kind": "assertion" if isinstance(error, AssertionError)
                          else "candidate_exception", "exception_type": type(error).__name__}), flush=True)
        return 1
    print(json.dumps({"passed": True, "kind": "assertion", "exception_type": None}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(evaluate(sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()))

