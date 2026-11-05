"""示例前置门禁：拒绝 Agent 直接修改生成目录，改动应通过项目生成流程产生。"""

import json
import sys


def main() -> int:
    """从 stdin 读取版本 1 工具事件，向 stdout 输出拒绝决定并返回 0；非法事件抛 ValueError。"""
    event = json.load(sys.stdin)
    if event.get("version") != 1 or event.get("event") != "PreToolUse":
        raise ValueError("仅接受版本1的工具前置事件")
    print(json.dumps({"decision": "deny", "reason": "生成目录请通过项目生成流程更新。"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
