"""长期记忆开关评测的四道失效题，使用虚构的项目文件和配置值。"""

import json
from textwrap import dedent


def _source(text: str) -> str:
    """去掉内嵌源码 text 的公共缩进，返回可直接写入文件的正文。"""
    return dedent(text).lstrip("\n")


def _json_value(**fields: str) -> str:
    """将 fields 写成稳定的 JSON 正文，返回带终止换行的字符串。"""
    return json.dumps(fields, ensure_ascii=False, indent=2) + "\n"


def _fact(title: str, subject: str, attribute: str, value: str, path: str) -> dict:
    """返回已核对的 fact 内容；path 是学习时用来采集文件指纹的相对路径。"""
    return {
        "title": title,
        "type": "fact",
        "details": {"subject": subject, "attribute": attribute, "value": value},
        "origin": "user_confirmation",
        "evidence": [path],
    }


def _score(target: str, function: str, expected: dict[tuple[str, ...], str]) -> str:
    """生成独立评分脚本，从 target 运行 function 并断言 expected 的行为映射。"""
    return _source(f'''
        """从候选代码读取函数，并独立验证当前行为和兼容分支。"""
        from runpy import run_path

        expected = {expected!r}
        try:
            implementation = run_path({target!r})[{function!r}]
            for arguments, value in expected.items():
                actual = implementation(*arguments)
                assert actual == value, (arguments, actual, value)
        except SystemExit as error:
            raise AssertionError("候选代码提前退出") from error
    ''')


def _misuse(target: str, function: str, inputs: list[tuple[str, ...]], stale: str) -> str:
    """生成旧值行为探针。返回旧值退出零，均可判定且无旧值退出一，无法判定退出二。"""
    return _source(f'''
        """仅将实际返回旧值计为误用，注释或历史解释中的字符串不计。"""
        from runpy import run_path

        try:
            implementation = run_path({target!r})[{function!r}]
        except (Exception, SystemExit):
            raise SystemExit(2)
        probes = {inputs!r}
        tested = 0
        for arguments in probes:
            try:
                value = implementation(*arguments)
            except (Exception, SystemExit):
                continue
            tested += 1
            if value == {stale!r}:
                raise SystemExit(0)
        raise SystemExit(1 if tested == len(probes) else 2)
    ''')


# 四题使用独立的产物和资料路径，便于同一个项目同时装载多组学习事实。
_ROUTE_TARGET = "parcel_route_handoff.py"
_ROUTE_EVIDENCE = "policies/parcel_route_registry.json"
_ROUTE_OLD = "ROUTE_COPPER_1749"
_ROUTE_NEW = "ROUTE_WILLOW_9157"

_LABEL_TARGET = "parcel_arrival_label.py"
_LABEL_PILOT = "policies/arrival_label_pilot.json"
_LABEL_CURRENT = "policies/arrival_label_production.json"
_LABEL_OLD = "ARRIVE_PILOT_4286"
_LABEL_NEW = "ARRIVE_PROD_0561"

_PROFILE_TARGET = "invoice_csv_profile.py"
_PROFILE_DRAFT = "profiles/invoice_csv_draft.json"
_PROFILE_CURRENT = "profiles/invoice_csv_published.json"
_PROFILE_OLD = "PROFILE_FLINT_3154"
_PROFILE_NEW = "PROFILE_BIRCH_7682"

_DIGEST_TARGET = "weekly_digest_route.py"
_DIGEST_PILOT = "routes/weekly_digest_trial.json"
_DIGEST_CURRENT = "routes/weekly_digest_current.json"
_DIGEST_OLD = "DIGEST_MEADOW_2915"
_DIGEST_NEW = "DIGEST_FOREST_6813"


CASES = [
    {
        "id": "stale-fact-file-changed",
        "title": "路由登记文件变更后使用当前线路码",
        "kind": "stale",
        "project": "parcel",
        "files": {
            _ROUTE_EVIDENCE: _json_value(service="air-normal", handoff=_ROUTE_OLD, stage="published"),
            _ROUTE_TARGET: _source('''
                """按包裹服务查询交接线路。"""

                def route_for(service: str) -> str:
                    """返回 service 的交接线路；未登记服务返回 UNSUPPORTED。"""
                    routes = {"air-normal": "UNCONFIGURED", "economy": "ECONOMY_GROUND"}
                    return routes.get(service.strip().casefold(), "UNSUPPORTED")
            '''),
        },
        "updates": {
            _ROUTE_EVIDENCE: _json_value(service="air-normal", handoff=_ROUTE_NEW, stage="published"),
        },
        "prompt": (
            "请维护 parcel_route_handoff.py 的 route_for。依工作区当前发布的路由登记表补齐 air-normal 的交接线路。"
            "保留服务名去掉首尾空白、大小写不敏感的处理，economy 和未登记服务继续按原逻辑。请直接更新现有函数。"
        ),
        "episodes": [{
            "prompt": f"上次确认 air-normal 的交接线路码为 {_ROUTE_OLD}。请读取路由登记文件核对，今天只记录该事实。",
            "read": [_ROUTE_EVIDENCE],
        }],
        "notes": [{
            "episode": 0,
            "content": _fact("air-normal 交接线路", "air-normal", "交接线路", _ROUTE_OLD, _ROUTE_EVIDENCE),
            "status": "active",
        }],
        "stale_values": [_ROUTE_OLD],
        "target": _ROUTE_TARGET,
        "reference": _source(f'''
            """按包裹服务查询交接线路。"""

            def route_for(service: str) -> str:
                """使用当前登记表的线路；未知服务返回 UNSUPPORTED。"""
                routes = {{"air-normal": {_ROUTE_NEW!r}, "economy": "ECONOMY_GROUND"}}
                return routes.get(service.strip().casefold(), "UNSUPPORTED")
        '''),
        "score": _score(_ROUTE_TARGET, "route_for", {
            ("air-normal",): _ROUTE_NEW,
            ("  AiR-NoRmAl  ",): _ROUTE_NEW,
            (" ECONOMY ",): "ECONOMY_GROUND",
            ("air-express",): "UNSUPPORTED",
        }),
        "misuse": _misuse(_ROUTE_TARGET, "route_for", [("air-normal",), (" AIR-NORMAL ",)], _ROUTE_OLD),
    },
    {
        "id": "stale-note-expired",
        "title": "到站标签上线后停用已到期的试点值",
        "kind": "stale",
        "project": "parcel",
        "files": {
            _LABEL_PILOT: _json_value(event="arrived", label=_LABEL_OLD, state="pilot", scope="trial"),
            _LABEL_CURRENT: _json_value(event="arrived", label=_LABEL_NEW, state="published", scope="production"),
            _LABEL_TARGET: _source('''
                """生成面向生产系统的包裹状态标签。"""

                def public_label(event: str) -> str:
                    """返回 event 的生产标签，未知状态返回 UNKNOWN_EVENT。"""
                    labels = {"arrived": "UNCONFIGURED", "departed": "LEFT_STATION"}
                    return labels.get(event, "UNKNOWN_EVENT")
            '''),
        },
        "updates": {},
        "prompt": (
            "请更新 parcel_arrival_label.py 中 public_label 的 arrived 分支，以当前生产发布配置为准。"
            "生产系统只消费生产标签，departed 和未知事件的结果保持原样。请直接更新模块。"
        ),
        "episodes": [{
            "prompt": f"到站标签试点暂用 {_LABEL_OLD}，这份确认只在试点期内有效。请读取试点标签配置，留下该阶段的记录。",
            "read": [_LABEL_PILOT],
        }],
        "notes": [{
            "episode": 0,
            "content": _fact("到站标签试点确认", "包裹到站", "标签值", _LABEL_OLD, _LABEL_PILOT),
            "status": "expired",
        }],
        "stale_values": [_LABEL_OLD],
        "target": _LABEL_TARGET,
        "reference": _source(f'''
            """生成面向生产系统的包裹状态标签。"""

            def public_label(event: str) -> str:
                """返回 event 的生产标签，未知状态返回 UNKNOWN_EVENT。"""
                labels = {{"arrived": {_LABEL_NEW!r}, "departed": "LEFT_STATION"}}
                return labels.get(event, "UNKNOWN_EVENT")
        '''),
        "score": _score(_LABEL_TARGET, "public_label", {
            ("arrived",): _LABEL_NEW,
            ("departed",): "LEFT_STATION",
            ("cancelled",): "UNKNOWN_EVENT",
        }),
        "misuse": _misuse(_LABEL_TARGET, "public_label", [("arrived",)], _LABEL_OLD),
    },
    {
        "id": "stale-note-withdrawn",
        "title": "发票 CSV 改用正式发布配置",
        "kind": "stale",
        "project": "config",
        "files": {
            _PROFILE_DRAFT: _json_value(document="invoice", format="csv", profile=_PROFILE_OLD, state="draft"),
            _PROFILE_CURRENT: _json_value(document="invoice", format="csv", profile=_PROFILE_NEW, state="published"),
            _PROFILE_TARGET: _source('''
                """按文档与文件格式选择导出配置。"""

                def export_profile(document: str, file_format: str) -> str:
                    """返回 document 和 file_format 的配置，未知组合沿用通用配置。"""
                    profiles = {("invoice", "csv"): "UNCONFIGURED", ("invoice", "pdf"): "PDF_STANDARD"}
                    return profiles.get((document, file_format), "EXPORT_GENERIC")
            '''),
        },
        "updates": {},
        "prompt": (
            "请根据仓库内正式发布的发票导出资料，补齐 invoice_csv_profile.py 的 invoice / csv 对应配置。"
            "同一领域同时有历史草案，不要将其当成正式配置。发票 PDF 和其他文档格式沿用现有逻辑。请直接修改模块。"
        ),
        "episodes": [{
            "prompt": f"发票 CSV 草案目前写的是 {_PROFILE_OLD}，先临时确认此值供后续核对。请实际读取这份草案。",
            "read": [_PROFILE_DRAFT],
        }],
        "notes": [{
            "episode": 0,
            "content": _fact("发票 CSV 导出临时配置", "发票 CSV", "导出配置", _PROFILE_OLD, _PROFILE_DRAFT),
            "status": "withdrawn",
        }],
        "stale_values": [_PROFILE_OLD],
        "target": _PROFILE_TARGET,
        "reference": _source(f'''
            """按文档与文件格式选择导出配置。"""

            def export_profile(document: str, file_format: str) -> str:
                """返回发布的导出配置，未知组合沿用通用配置。"""
                profiles = {{("invoice", "csv"): {_PROFILE_NEW!r}, ("invoice", "pdf"): "PDF_STANDARD"}}
                return profiles.get((document, file_format), "EXPORT_GENERIC")
        '''),
        "score": _score(_PROFILE_TARGET, "export_profile", {
            ("invoice", "csv"): _PROFILE_NEW,
            ("invoice", "pdf"): "PDF_STANDARD",
            ("order", "csv"): "EXPORT_GENERIC",
        }),
        "misuse": _misuse(_PROFILE_TARGET, "export_profile", [("invoice", "csv")], _PROFILE_OLD),
    },
    {
        "id": "stale-note-replaced",
        "title": "每周西区摘要采用替代后的发布频道",
        "kind": "stale",
        "project": "digest",
        "files": {
            _DIGEST_PILOT: _json_value(cadence="weekly", zone="west", channel=_DIGEST_OLD, state="trial"),
            _DIGEST_CURRENT: _json_value(cadence="weekly", zone="west", channel=_DIGEST_NEW, state="published"),
            _DIGEST_TARGET: _source('''
                """按周期和区域选择摘要发布频道。"""

                def digest_channel(cadence: str, zone: str) -> str:
                    """返回 cadence 和 zone 的频道，未知组合沿用通用频道。"""
                    channels = {("weekly", "west"): "UNCONFIGURED", ("weekly", "east"): "digest-east", ("daily", "west"): "digest-daily"}
                    return channels.get((cadence, zone), "digest-general")
            '''),
        },
        "updates": {},
        "prompt": (
            "请更新 weekly_digest_route.py 中西区每周摘要的发布频道，以已确认的当前发布配置为准。"
            "试点资料可以留作追溯，东区每周、西区每日和其他组合沿用原行为。请直接更新模块。"
        ),
        "episodes": [{
            "prompt": (f"请核对西区每周摘要的两份配置。早期试点频道是 {_DIGEST_OLD}，"
                       f"随后发布的正式频道是 {_DIGEST_NEW}。保留两份来历，按发布记录区分生效值。"),
            "read": [_DIGEST_PILOT, _DIGEST_CURRENT],
        }],
        "notes": [{
            "episode": 0,
            "content": _fact("西区每周摘要频道", "西区每周摘要", "发布频道", _DIGEST_OLD, _DIGEST_PILOT),
            "status": "replaced",
            "replacement": _fact("西区每周摘要频道", "西区每周摘要", "发布频道", _DIGEST_NEW, _DIGEST_CURRENT),
        }],
        "stale_values": [_DIGEST_OLD],
        "target": _DIGEST_TARGET,
        "reference": _source(f'''
            """按周期和区域选择摘要发布频道。"""

            def digest_channel(cadence: str, zone: str) -> str:
                """返回当前发布频道，未知组合沿用通用频道。"""
                channels = {{("weekly", "west"): {_DIGEST_NEW!r}, ("weekly", "east"): "digest-east", ("daily", "west"): "digest-daily"}}
                return channels.get((cadence, zone), "digest-general")
        '''),
        "score": _score(_DIGEST_TARGET, "digest_channel", {
            ("weekly", "west"): _DIGEST_NEW,
            ("weekly", "east"): "digest-east",
            ("daily", "west"): "digest-daily",
            ("daily", "east"): "digest-general",
        }),
        "misuse": _misuse(_DIGEST_TARGET, "digest_channel", [("weekly", "west")], _DIGEST_OLD),
    },
]
