"""独立检查固定日志样例的 report.json；0通过、1产物不符、2验收器异常。"""

import json
import os
from pathlib import Path
import stat
import sys


EXPECTED_COUNTS = {"timeout": 3, "database": 2, "auth": 1}  # 宿主固定参考，不使用模型输出作为标准。
MAX_REPORT_BYTES = 1024 * 1024  # 最多检查1 MiB报告，避免为异常产物无界读取。


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """将 JSON 键值对转为字典；重复键抛 ValueError，避免覆盖后误判通过。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def check_report(workspace: Path) -> list[str]:
    """检查 workspace 内的报告，返回失败说明列表；空列表表示固定样例通过。

    workspace 是宿主传入的绝对任务目录。只读 report.json，不修改产物；文件缺失、
    非普通文件、越界链接、超过字节限制、JSON/字段/计数错误均视为产物不符。
    """
    try:
        base = workspace.resolve(strict=True)
        path = (base / "report.json").resolve(strict=True)
        if not path.is_relative_to(base) or not stat.S_ISREG(path.stat().st_mode):
            return ["报告必须是工作区内的普通文件"]
        with path.open("rb") as source:
            raw = source.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            return ["报告超过允许大小"]
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return ["报告缺失、不可读取或不是有效的 UTF-8 JSON"]
    if not isinstance(report, dict) or set(report) != {"counts", "total_errors"}:
        return ["报告必须只包含 counts 与 total_errors"]
    counts, total = report["counts"], report["total_errors"]
    if not isinstance(counts, dict) or set(counts) != set(EXPECTED_COUNTS):
        return ["错误分类与固定日志参考不一致"]
    if any(type(value) is not int or value < 0 for value in counts.values()) or type(total) is not int:
        return ["计数和总数必须是整数，不能使用布尔值或浮点数"]
    errors = []
    if counts != EXPECTED_COUNTS:
        errors.append("错误分类计数不符合固定日志样例")
    if total != sum(EXPECTED_COUNTS.values()) or total != sum(counts.values()):
        errors.append("ERROR 总数与参考或分类合计不一致")
    return errors


def main() -> int:
    """读取两项固定宿主环境变量并打印结构化结论，返回0/1/2，不回显环境或异常正文。"""
    workspace = os.environ.get("HARNESS_WORKSPACE")
    run_id = os.environ.get("HARNESS_RUN_ID")
    if not workspace or not run_id:
        print("缺少宿主验收上下文", file=sys.stderr)
        return 2
    try:
        errors = check_report(Path(workspace))
    except Exception:
        # 产物问题由 check_report 转为断言失败；其余实现异常使用协议专用错误码。
        print("验收器内部异常", file=sys.stderr)
        return 2
    result = {"status": "failed" if errors else "passed", "checks": "fixed_logs_report", "errors": errors}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
