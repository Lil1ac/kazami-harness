"""四道使用已核验稳定事实的复用题，数据为虚构接口与业务枚举。"""

import json
from textwrap import dedent


def _code(text: str) -> str:
    """清除内嵌源码 text 的共同缩进，返回带结尾换行的源码。"""
    return dedent(text).strip() + "\n"


def _fixture(identifier: str, title: str, project: str, path: str, target: str,
             mapping: dict, prompt: str, baseline: str, reference: str, score: str) -> dict:
    """生成一题；path 是学习和答题共用的证据，target 是待改源码。

    identifier、title 和 project 定位题目及作用域。mapping 是已发布完整事实，prompt 为新任务。
    baseline 和 reference 分别是初始与参考源码，score 为独立候选评分脚本。
    """
    fact = json.dumps(mapping, ensure_ascii=False, separators=(", ", ": "))
    return {
        "id": identifier,
        "title": title,
        "kind": "reuse",
        "project": project,
        "files": {
            path: f"# {title}\n\n生效版本 R4，以下为完整的正式登记。\n\n```json\n{fact}\n```\n",
            target: _code(baseline),
        },
        "prompt": prompt,
        "episodes": [{
            "prompt": f"请读取 {title} 的正式资料，核对已生效版本的完整枚举和所有对应值。本轮只核对接口资料。",
            "read": [path],
        }],
        "notes": [{
            "episode": 0,
            "status": "active",
            "content": {
                "type": "fact",
                "title": title,
                "details": {"subject": title, "attribute": "生效版本 R4 完整登记", "value": fact},
                "origin": "tool_observation",
                "evidence": [path],
            },
        }],
        "target": target,
        "reference": _code(reference),
        "score": _code(score),
    }


CASES = [
    _fixture(
        "reuse-fact-transfer-scan-codes", "中转扫描事件码", "parcel",
        "contracts/relay-scan-registry-r4.md", "transfer_scans_r4.py",
        {"BridgeAccepted": "Z4A", "BridgeHeld": "F8Q", "BridgeSorted": "P2K", "BridgeClosed": "M7T"},
        "请更新 transfer_scans_r4.py 的 build_transfer_row。用已发布的中转扫描事件码，将输入 scan_status 转成输出 event_code。"
        "保留 transfer_id，缺失或未登记的状态写 None。状态名按接口原样精确匹配。",
        '''
        def build_transfer_row(raw: dict) -> dict:
            """提取 raw 的中转记录编号，返回新的台账字典。"""
            return {"transfer_id": raw["transfer_id"]}
        ''',
        '''
        def build_transfer_row(raw: dict) -> dict:
            """从 raw 返回中转记录编号和登记的事件码，未识别状态写 None。"""
            codes = {"BridgeAccepted": "Z4A", "BridgeHeld": "F8Q", "BridgeSorted": "P2K", "BridgeClosed": "M7T"}
            return {"transfer_id": raw["transfer_id"], "event_code": codes.get(raw.get("scan_status"))}
        ''',
        '''
        from runpy import run_path

        build = run_path("transfer_scans_r4.py")["build_transfer_row"]
        for status, code in (("BridgeAccepted", "Z4A"), ("BridgeHeld", "F8Q"),
                             ("BridgeSorted", "P2K"), ("BridgeClosed", "M7T")):
            assert build({"transfer_id": "X07", "scan_status": status}) == {"transfer_id": "X07", "event_code": code}
        assert build({"transfer_id": 5}) == {"transfer_id": 5, "event_code": None}
        assert build({"transfer_id": 5, "scan_status": "bridgeheld"})["event_code"] is None
        ''',
    ),
    _fixture(
        "reuse-fact-job-window-policy", "作业窗口别名到策略编号", "config",
        "runtime-spec/window-key-registry-r4.md", "job_window_r4.py",
        {"early": "WIN_VEGA_8", "core": "WIN_ORBIT_3", "late": "WIN_MINT_6", "flex": "WIN_BRASS_2"},
        "请更新 job_window_r4.py 的 format_settings，按作业窗口别名到策略编号的现行登记，将可选的 window_slug 转成 policy_id。"
        "别名忽略首尾空白和大小写，缺失、None 或未登记写 None。保留 workers 的默认值和整数转换。",
        '''
        def format_settings(raw: dict) -> dict:
            """读取 raw 中的 workers，默认两个工作进程。"""
            return {"workers": int(raw.get("workers", 2))}
        ''',
        '''
        def format_settings(raw: dict) -> dict:
            """返回工作进程数和 raw 指定的窗口策略，未识别的别名返回 None。"""
            policies = {"early": "WIN_VEGA_8", "core": "WIN_ORBIT_3", "late": "WIN_MINT_6", "flex": "WIN_BRASS_2"}
            slug = raw.get("window_slug")
            policy = None if slug is None else policies.get(slug.strip().casefold())
            return {"workers": int(raw.get("workers", 2)), "policy_id": policy}
        ''',
        '''
        from runpy import run_path

        format_settings = run_path("job_window_r4.py")["format_settings"]
        for slug, policy in ((" EARLY ", "WIN_VEGA_8"), ("core", "WIN_ORBIT_3"),
                             ("LaTe", "WIN_MINT_6"), ("flex", "WIN_BRASS_2")):
            assert format_settings({"window_slug": slug, "workers": "4"}) == {"workers": 4, "policy_id": policy}
        assert format_settings({}) == {"workers": 2, "policy_id": None}
        assert format_settings({"window_slug": None})["policy_id"] is None
        assert format_settings({"window_slug": "graveyard"})["policy_id"] is None
        ''',
    ),
    _fixture(
        "reuse-fact-digest-template-grid", "摘要订阅通知模板矩阵", "digest",
        "delivery-spec/subscription-template-grid-r4.md", "digest_template_r4.py",
        {"brief": {"zh-CN": "DGT_AMBER_45", "en-US": "DGT_IRIS_62"},
         "full": {"zh-CN": "DGT_MAPLE_18", "en-US": "DGT_HAZEL_53"}},
        "请更新 digest_template_r4.py 的 build_notification。根据已发布的摘要订阅通知模板矩阵，读取输入的 layout、locale，"
        "添加 template_id。缺失或未知的组合写 None。两个键使用协议原值精确匹配，保留 subscription_id。",
        '''
        def build_notification(subscription: dict) -> dict:
            """从 subscription 取出订阅编号，返回通知的初始字段。"""
            return {"subscription_id": subscription["subscription_id"]}
        ''',
        '''
        def build_notification(subscription: dict) -> dict:
            """返回订阅编号和模板 ID，未登记的布局与地区组合写 None。"""
            templates = {
                "brief": {"zh-CN": "DGT_AMBER_45", "en-US": "DGT_IRIS_62"},
                "full": {"zh-CN": "DGT_MAPLE_18", "en-US": "DGT_HAZEL_53"},
            }
            template = templates.get(subscription.get("layout"), {}).get(subscription.get("locale"))
            return {"subscription_id": subscription["subscription_id"], "template_id": template}
        ''',
        '''
        from runpy import run_path

        build = run_path("digest_template_r4.py")["build_notification"]
        for layout, locale, value in (("brief", "zh-CN", "DGT_AMBER_45"), ("brief", "en-US", "DGT_IRIS_62"),
                                      ("full", "zh-CN", "DGT_MAPLE_18"), ("full", "en-US", "DGT_HAZEL_53")):
            assert build({"subscription_id": "0009", "layout": layout, "locale": locale}) == {
                "subscription_id": "0009", "template_id": value,
            }
        assert build({"subscription_id": 3, "layout": "full"}) == {"subscription_id": 3, "template_id": None}
        assert build({"subscription_id": 3, "layout": "brief", "locale": "en-us"})["template_id"] is None
        ''',
    ),
    _fixture(
        "reuse-fact-parcel-hold-reasons", "包裹留置原因对外编码", "parcel",
        "relay-interfaces/parcel-hold-dictionary-r4.md", "parcel_hold_notice_r4.py",
        {"RIM": {"code": "HOLD_MOSS_76", "severity": 2},
         "LUX": {"code": "HOLD_GARNET_31", "severity": 4},
         "YET": {"code": "HOLD_FERN_94", "severity": 1}},
        "请更新 parcel_hold_notice_r4.py 的 build_notice，按已发布的包裹留置原因对外编码表，"
        "把输入 hold_reason 转成 reason_code 和整数 severity。原始代号精确匹配，缺失或未登记时两项都写 None，保留 parcel_id。",
        '''
        def build_notice(parcel: dict) -> dict:
            """返回 parcel 给出的包裹编号，作为留置通知的初始字段。"""
            return {"parcel_id": parcel["parcel_id"]}
        ''',
        '''
        def build_notice(parcel: dict) -> dict:
            """返回包裹编号及登记的留置元数据，缺失或未登记时写 None。"""
            reasons = {"RIM": ("HOLD_MOSS_76", 2), "LUX": ("HOLD_GARNET_31", 4), "YET": ("HOLD_FERN_94", 1)}
            code, severity = reasons.get(parcel.get("hold_reason"), (None, None))
            return {"parcel_id": parcel["parcel_id"], "reason_code": code, "severity": severity}
        ''',
        '''
        from runpy import run_path

        build = run_path("parcel_hold_notice_r4.py")["build_notice"]
        for reason, code, severity in (("RIM", "HOLD_MOSS_76", 2), ("LUX", "HOLD_GARNET_31", 4), ("YET", "HOLD_FERN_94", 1)):
            result = build({"parcel_id": 41, "hold_reason": reason})
            assert result == {"parcel_id": 41, "reason_code": code, "severity": severity}
            assert type(result["severity"]) is int
        assert build({"parcel_id": 41}) == {"parcel_id": 41, "reason_code": None, "severity": None}
        assert build({"parcel_id": 41, "hold_reason": "rim"}) == {"parcel_id": 41, "reason_code": None, "severity": None}
        ''',
    ),
]
