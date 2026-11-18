"""独立核对 alert-envelope 的交付物；评分器在隔离副本运行，按任务契约返回进程状态。"""

import json
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1])
checks = json.loads("[[\"alert_envelope\",[\"high\",5],{\"severity\":\"P1\",\"ttl_s\":300}],[\"alert_envelope\",[\"low\",2],{\"severity\":\"P3\",\"ttl_s\":120}],[\"alert_envelope\",[\"normal\",0],{\"severity\":\"P3\",\"ttl_s\":0}]]")
try:
    namespace = runpy.run_path(str(root / "solution.py"))
    for name, args, expected in checks:
        value = namespace[name](*args)
        assert type(value) is type(expected) and json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True), f"{name}{tuple(args)}: 预期 {expected!r}，实际 {value!r}"
except (AssertionError, KeyError, TypeError, ValueError, SyntaxError, FileNotFoundError) as error:
    print(error)
    raise SystemExit(1)
print(f"{len(checks)} 项断言通过")
