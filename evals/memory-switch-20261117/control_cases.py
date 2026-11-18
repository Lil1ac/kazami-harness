"""两道独立负对照，核查本项目无关记忆及异项目同名决策的干扰。"""

import json


def _json_line(value: dict) -> str:
    """把小型 JSON 对象 value 序列化为项目工作区文本，返回以换行结尾的一行。"""
    return json.dumps(value, ensure_ascii=False) + "\n"


def _check(target: str, field: str, expected: str, *, exact: bool) -> str:
    """生成从候选根读取 target 的独立 Python 评分脚本。

    exact=True 核对输出仅含 field 且等于 expected；False 只判定诱饵值是否用于该字段。命中时脚本退出 0。
    格式无效时，完整评分记失败，误用探针记为不可判定。脚本只读取候选产物。
    """
    comparison = (f"value == {{{field!r}: {expected!r}}}" if exact
                  else f"isinstance(value, dict) and value.get({field!r}) == {expected!r}")
    return (
        "from pathlib import Path\n"
        "import json\n"
        "try:\n"
        f"    value = json.loads(Path({target!r}).read_text(encoding='utf-8'))\n"
        "except (OSError, UnicodeError, ValueError):\n"
        f"    raise SystemExit({1 if exact else 2}) from None\n"
        + ("" if exact else f"if not isinstance(value, dict) or {field!r} not in value:\n    raise SystemExit(2)\n")
        +
        f"raise SystemExit(0 if {comparison} else 1)\n"
    )


# 当前产物在 answer 阶段可查项目文档。两个诱饵只对应独立场景，不作为当前需求答案。
CASES = [
    {
        "id": "control-release-profile",
        "title": "生产发布配置不套用预览约定",
        "kind": "control",
        "project": "config",
        "files": {
            "docs/release-policy.json": _json_line({"production_profile": "release-jade-72"}),
            "docs/preview-policy.json": _json_line({"preview_profile": "preview-amber-913"}),
            "out/release.json": _json_line({"profile": None}),
        },
        "prompt": (
            "生成当前项目的生产发布收据。检查 docs/release-policy.json 的现行 production_profile，"
            "写入 out/release.json。输出是仅含 profile 字段的 JSON 对象。其他文件保持原样。"
        ),
        "episodes": [
            {"prompt": "确认预览环境的部署收据约定。预览收据依据 docs/preview-policy.json 单独生成。",
             "read": ["docs/preview-policy.json"]},
        ],
        "notes": [
            {"episode": 0, "status": "active", "content": {
                "title": "发布收据的预览部署方案",
                "type": "decision",
                "details": {"topic": "发布收据的部署 profile", "choice": "preview-amber-913",
                            "rationale": "仅供预览环境接入共享测试资源。"},
                "tags": ["发布收据", "profile"],
                "applies_to": {"paths": ["docs/preview-policy.json", "out/preview.json"]},
            }},
        ],
        "stale_values": ["preview-amber-913"],
        "target": "out/release.json",
        "reference": _json_line({"profile": "release-jade-72"}),
        "score": _check("out/release.json", "profile", "release-jade-72", exact=True),
        "misuse": _check("out/release.json", "profile", "preview-amber-913", exact=False),
    },
    {
        "id": "control-digest-project-route",
        "title": "日报投递只采用本项目路线",
        "kind": "control",
        "project": "digest",
        "files": {
            "docs/digest-routing.json": _json_line({"digest_route": "summary-moss-35"}),
            "out/digest.json": _json_line({"route": None}),
        },
        "foreign_files": {
            "docs/foreign-digest-routing.json": _json_line({"digest_route": "summary-copper-841"}),
        },
        "prompt": (
            "生成当前项目日报的投递清单。用 docs/digest-routing.json 的现行 digest_route，"
            "写入 out/digest.json。输出是仅含 route 字段的 JSON 对象。其他文件保持原样。"
        ),
        "episodes": [
            {"prompt": "确认这个独立项目的日报投递约定，读取 docs/foreign-digest-routing.json 的现行路线。",
             "read": ["docs/foreign-digest-routing.json"]},
        ],
        "notes": [
            {"episode": 0, "status": "active", "foreign": True, "content": {
                "title": "日报投递路线 digest_route",
                "type": "decision",
                "details": {"topic": "日报投递路线 digest_route", "choice": "summary-copper-841",
                            "rationale": "该独立项目的通知服务使用单独的日报路线。"},
                "tags": ["日报投递", "digest_route"],
                "evidence": ["docs/foreign-digest-routing.json"],
            }},
        ],
        "stale_values": ["summary-copper-841"],
        "target": "out/digest.json",
        "reference": _json_line({"route": "summary-moss-35"}),
        "score": _check("out/digest.json", "route", "summary-moss-35", exact=True),
        "misuse": _check("out/digest.json", "route", "summary-copper-841", exact=False),
    },
]
