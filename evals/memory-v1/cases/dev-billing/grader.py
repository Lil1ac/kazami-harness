"""独立核对 dev-billing 的交付物；评分器在隔离副本运行，按任务契约返回进程状态。"""

import json
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1])
checks = json.loads("[[\"draft_amount\",[120],{\"draft_total\":\"1.2\"}],[\"draft_amount\",[0],{\"draft_total\":\"0.0\"}]]")
try:
    namespace = runpy.run_path(str(root / "solution.py"))
    for name, args, expected in checks:
        value = namespace[name](*args)
        assert type(value) is type(expected) and json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True), f"{name}{tuple(args)}: 预期 {expected!r}，实际 {value!r}"
except (AssertionError, KeyError, TypeError, ValueError, SyntaxError, FileNotFoundError) as error:
    print(error)
    raise SystemExit(1)
print(f"{len(checks)} 项断言通过")
