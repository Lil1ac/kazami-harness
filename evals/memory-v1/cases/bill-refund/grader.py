"""独立核对 bill-refund 的交付物；评分器在隔离副本运行，按任务契约返回进程状态。"""

import json
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1])
checks = json.loads("[[\"refund_v2\",[125],{\"amount\":\"-1.25\"}],[\"refund_v2\",[-4],{\"amount\":\"-0.04\"}],[\"refund_v2\",[0],{\"amount\":\"0.00\"}],[\"legacy_payload\",[2],{\"raw_cents\":2}]]")
try:
    namespace = runpy.run_path(str(root / "solution.py"))
    for name, args, expected in checks:
        value = namespace[name](*args)
        assert type(value) is type(expected) and json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True), f"{name}{tuple(args)}: 预期 {expected!r}，实际 {value!r}"
except (AssertionError, KeyError, TypeError, ValueError, SyntaxError, FileNotFoundError) as error:
    print(error)
    raise SystemExit(1)
print(f"{len(checks)} 项断言通过")
