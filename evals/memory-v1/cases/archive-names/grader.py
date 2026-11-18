"""独立核对 archive-names 的交付物；评分器在隔离副本运行，按任务契约返回进程状态。"""

import json
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1])
checks = json.loads("[[\"normalize_names\",[[\" Z \",\"a\",\"A\",\" \"]],[\"a\",\"z\"]],[\"normalize_names\",[[\"É\",\"é\",\"Beta\"]],[\"beta\",\"é\"]],[\"normalize_names\",[[]],[]]]")
try:
    namespace = runpy.run_path(str(root / "solution.py"))
    for name, args, expected in checks:
        before = json.dumps(args)  # 检查契约要求的输入列表不变。
        value = namespace[name](*args)
        assert json.dumps(args) == before, "原输入列表发生变化"
        assert type(value) is type(expected) and json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True), f"{name}{tuple(args)}: 预期 {expected!r}，实际 {value!r}"
except (AssertionError, KeyError, TypeError, ValueError, SyntaxError, FileNotFoundError) as error:
    print(error)
    raise SystemExit(1)
print(f"{len(checks)} 项断言通过")
