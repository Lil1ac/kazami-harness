"""从已固定的六份官方记录生成开发试点清单；不联网、不选择题目、不调用模型。"""

import json
from pathlib import Path


def main() -> None:
    """读取同目录六份任务并写suite.json；返回None，原始issue保持不变，参考补丁仅留宿主记录。"""
    root = Path(__file__).resolve().parent
    identifiers = ["pytest-dev__pytest-5262", "django__django-16082", "django__django-17029",
                   "pytest-dev__pytest-7521", "sympy__sympy-22914", "sympy__sympy-23950"]
    cases = []
    for identifier in identifiers:
        record = json.loads((root / (identifier + ".json")).read_text(encoding="utf-8"))
        cases.append({"id": identifier, "project": record["repo"], "category": "repository_bugfix", "split": "dev",
                      "backend": "swebench", "prompt": record["problem_statement"], "source": identifier + ".json"})
    suite = {"name": "SWE-bench Verified 六题开发试点", "version": "2026-11-12-pilot1",
             "provenance": "来自princeton-nlp/SWE-bench_Verified，人工选择Django、pytest、SymPy三个仓库的六个较小修复，属于便利开发样本；未删改原始issue（可能含解决提示）。Requests候选因无网络回归失败、django-16429因上游测试名称解析冲突被排除；全部选择在模型运行前完成。此成绩不是官方完整SWE-bench得分，不能据此推广到真实任务总体。",
             "cases": cases}
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
