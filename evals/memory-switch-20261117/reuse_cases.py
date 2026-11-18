"""六道新拟的跨会话复用题。学习资料独立于候选代码和当期字段细节。"""

from textwrap import dedent


def _code(source: str) -> str:
    """去掉内嵌 Python 源码的统一缩进，返回以单个换行结束的源码。"""
    return dedent(source).strip() + "\n"


def _rule(episode: int, title: str, behavior: str, evidence: str, tags: list[str]) -> dict:
    """返回一条会话约定，episode 从零起算，evidence 为当轮实际读取的资料路径。

    title 和 behavior 分别给出约定名称与做法，tags 提供相关检索关键词。
    """
    return {
        "episode": episode,
        "status": "active",
        "content": {
            "type": "rule",
            "title": title,
            "details": {"topic": title, "behavior": behavior},
            "evidence": [evidence],
            "tags": tags,
        },
    }


def _reference(episode: int, title: str, target: str, evidence: str, tags: list[str]) -> dict:
    """返回一条资料入口，episode 从零起算，title 是名称，tags 提供检索词。

    target 是应查阅的资料路径，evidence 是当轮已读的索引路径。
    """
    return {
        "episode": episode,
        "status": "active",
        "content": {
            "type": "reference",
            "title": title,
            "details": {"target": target, "purpose": title},
            "evidence": [evidence],
            "tags": tags,
        },
    }


# 每题都带一个可运行的初始模块、新需求完成后的参考版本和宿主侧断言。
CASES = [
    {
        "id": "reuse-parcel-depot-reference",
        "title": "接入承运商合作方站点标识",
        "kind": "reuse",
        "project": "parcel",
        "target": "carrier.py",
        "files": {
            "README.md": "# 小型包裹台账\n接口版本在 merchant/，常用工程约定在 handbook/。\n",
            "handbook/keys.md": (
                "# 合作方标识\n合作方 ID 若提供字符串，去掉首尾空白，保留内部字符和大小写。"
                "缺失、None 或纯空白统一写 None；保留前导零，勿转成数字。\n"
            ),
            "merchant/gateway-2026-h2.txt": (
                "本期承运商网关接口：新传 vstRef 字段，对应台账的 depot_reference。"
                "这是可选合作方标识。未传时写 None。既有 parcel_id 字段不变。\n"
            ),
            "carrier.py": _code('''
                def normalize_packet(row):
                    """从网关记录 row 提取包裹台账字段，返回新的字典。"""
                    return {"parcel_id": row["parcel_id"]}
            '''),
        },
        "episodes": [
            {"prompt": "核对一般合作方标识的处理约定，例如包含前导零的商户编号。只核对资料。", "read": ["handbook/keys.md"]},
        ],
        "notes": [
            _rule(0, "合作方标识", "去掉首尾空白，保留内部字符、大小写和前导零；缺失、None 或纯空白使用 None。",
                  "handbook/keys.md", ["合作方标识", "承运商", "网关"]),
        ],
        "prompt": "修改 carrier.py 的 normalize_packet，按当前承运商网关协议接入可选站点标识。保持包裹编号的既有行为，按仓库的合作方标识规则处理。",
        "reference": _code('''
            def normalize_packet(row):
                """从网关记录 row 返回包裹编号及可选合作方站点标识。"""
                reference = row.get("vstRef")
                if reference is not None:
                    reference = reference.strip() or None
                return {"parcel_id": row["parcel_id"], "depot_reference": reference}
        '''),
        "score": _code('''
            from carrier import normalize_packet

            assert normalize_packet({"parcel_id": 19, "vstRef": "  001Ab  "}) == {
                "parcel_id": 19, "depot_reference": "001Ab"
            }
            assert normalize_packet({"parcel_id": 5, "vstRef": "   "})["depot_reference"] is None
            assert normalize_packet({"parcel_id": 7}) == {"parcel_id": 7, "depot_reference": None}
            assert normalize_packet({"parcel_id": "P8", "vstRef": None})["depot_reference"] is None
            assert normalize_packet({"parcel_id": "P9", "vstRef": " Ab C "})["depot_reference"] == "Ab C"
        '''),
    },
    {
        "id": "reuse-parcel-terminal-wait",
        "title": "组合协议入口和时长约定处理码头排队",
        "kind": "reuse",
        "project": "parcel",
        "target": "manifest.py",
        "files": {
            "README.md": "# 小型包裹台账\n导入文档和时间约定在 help/；批次载荷版本可参考入口目录。\n",
            "help/waterway-index.txt": (
                "海道批次载荷当前合同放在 wires/estuary-v2.txt。"
                "找新接入的海道批次字段时从这里进入；wires/harbor-v1.txt 是历史版本。\n"
            ),
            "help/duration.md": (
                "# 时长字段\n展示层只接收完整分钟。来自外部记录的非负整数秒向上取到整分钟。"
                "没有时长保留 None；显式零秒等于 0 分钟；负秒数为无效值，抛 ValueError。\n"
            ),
            "wires/estuary-v2.txt": (
                "海道 v2 当前合同。码头排队时长从 q7s 读取，单位秒，写到台账 terminal_wait_minutes。"
                "允许未传和 None。泊位 berth 不改。\n"
            ),
            "wires/harbor-v1.txt": "旧海道 v1 合同，曾计划用 queue_seconds，未随 v2 发布。\n",
            "manifest.py": _code('''
                def normalize_manifest(record):
                    """将海道批次 record 转换为台账字典。"""
                    return {"berth": record["berth"]}
            '''),
        },
        "episodes": [
            {"prompt": "确定团队接入海道批次字段时，如何找到当前载荷合同。只阅读入口说明。", "read": ["help/waterway-index.txt"]},
            {"prompt": "整理上一次展示提货时长时采用的分钟换算约定。只阅读规则正文。", "read": ["help/duration.md"]},
        ],
        "notes": [
            _reference(0, "海道批次当前载荷合同", "wires/estuary-v2.txt", "help/waterway-index.txt", ["海道", "码头", "排队"]),
            _rule(1, "外部时长换算", "非负整数秒向上取整为分钟，缺失保留 None，显式零秒为 0，负值抛出 ValueError。",
                  "help/duration.md", ["时长", "分钟", "海道", "码头"]),
        ],
        "prompt": "修改 manifest.py 的 normalize_manifest，接入海道运输批次的码头排队时长。使用当前合同和已有时长约定，保留泊位字段。",
        "reference": _code('''
            def normalize_manifest(record):
                """返回批次泊位及排队分钟数，缺失时长用 None，负值抛 ValueError。"""
                seconds = record.get("q7s")
                if seconds is not None and seconds < 0:
                    raise ValueError("码头排队时长不可为负")
                minutes = None if seconds is None else (seconds + 59) // 60
                return {"berth": record["berth"], "terminal_wait_minutes": minutes}
        '''),
        "score": _code('''
            from manifest import normalize_manifest

            assert normalize_manifest({"berth": "E2", "q7s": 61}) == {
                "berth": "E2", "terminal_wait_minutes": 2
            }
            assert normalize_manifest({"berth": "E1", "q7s": 60})["terminal_wait_minutes"] == 1
            assert normalize_manifest({"berth": "E1", "q7s": 0})["terminal_wait_minutes"] == 0
            assert normalize_manifest({"berth": "E1", "q7s": None})["terminal_wait_minutes"] is None
            assert normalize_manifest({"berth": "E1", "queue_seconds": 99})["terminal_wait_minutes"] is None
            try:
                normalize_manifest({"berth": "E1", "q7s": -1})
            except ValueError:
                pass
            else:
                raise AssertionError("负秒数没有拒绝")
        '''),
    },
    {
        "id": "reuse-config-sweep-limit",
        "title": "按当前登记接入扫档批量上限",
        "kind": "reuse",
        "project": "config",
        "target": "settings.py",
        "files": {
            "README.md": "# 配置转换工具\n修改设置接口时先看 ops/ 中的入口约定。catalog/ 有多份历史记录。\n",
            "ops/setting-index.txt": (
                "设置加载器的正式参数名、兼容输入键和默认值登记在 catalog/rolling-03.tsv；"
                "catalog/rolling-02.tsv 仅用于旧号回溯，不是当前配置基准。\n"
            ),
            "catalog/rolling-03.tsv": (
                "业务名称\t输入键\t输出键\t默认值\t约束\n"
                "每轮扫档批量上限\tscan_slice\tsweep_limit\t7\t"
                "允许正整数或十进制整数字符串，拒绝 bool、浮点、None 及非正整数，无效时报 ValueError\n"
            ),
            "catalog/rolling-02.tsv": "业务名称\t输入键\t输出键\t默认值\n每轮扫档批量上限\tsweep_max\tlimit\t12\n",
            "settings.py": _code('''
                def load_settings(values):
                    """读取 values 中的参数，返回设置字典。"""
                    return {"parallelism": int(values.get("parallelism", 2))}
            '''),
        },
        "episodes": [
            {"prompt": "确认如何找到生产设置登记，之前有人把旧登记当成配置来源。只查起点。", "read": ["ops/setting-index.txt"]},
        ],
        "notes": [
            _reference(0, "当前设置加载登记", "catalog/rolling-03.tsv", "ops/setting-index.txt", ["设置", "扫档", "配置"]),
        ],
        "prompt": "在 settings.py 的 load_settings 中接入每轮扫档批量上限，按当前生产设置登记确定字段和输入校验，保持 parallelism 原行为。",
        "reference": _code('''
            def load_settings(values):
                """返回并发度和正整数扫档上限，无效上限抛 ValueError。"""
                raw_limit = values.get("scan_slice", 7)
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, (str, int)):
                    raise ValueError("扫档上限必须是正整数")
                limit = int(raw_limit)
                if limit <= 0:
                    raise ValueError("扫档上限必须是正整数")
                return {"parallelism": int(values.get("parallelism", 2)), "sweep_limit": limit}
        '''),
        "score": _code('''
            from settings import load_settings

            assert load_settings({}) == {"parallelism": 2, "sweep_limit": 7}
            assert load_settings({"scan_slice": "11", "parallelism": "4"}) == {
                "parallelism": 4, "sweep_limit": 11
            }
            assert load_settings({"sweep_max": 17})["sweep_limit"] == 7
            for invalid in ("five", "1.25", "0", -2, 2.5, True, None):
                try:
                    load_settings({"scan_slice": invalid})
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"无效上限被接受：{invalid!r}")
        '''),
    },
    {
        "id": "reuse-config-notify-sources",
        "title": "给通知来源列表复用环境配置约定",
        "kind": "reuse",
        "project": "config",
        "target": "environment.py",
        "files": {
            "README.md": "# 配置转换工具\n环境变量的约定在 policy/，当期键名在 change/。\n",
            "policy/env-list.md": (
                "# 环境列表约定\n列表用分号分隔。去掉单项首尾空白和空项，严格区分大小写；"
                "按首次出现顺序去重。未设置或空列表返回 []。\n"
            ),
            "change/w17.txt": "通知模块接入：读 NOTICE_SOURCE_KEYS，公开字段 notification_sources，输入为环境列表。\n",
            "environment.py": _code('''
                def normalized_environment(values):
                    """从 values 读取环境配置，返回项目配置字典。"""
                    return {"mode": values.get("APP_MODE", "worker")}
            '''),
        },
        "episodes": [
            {"prompt": "记录旧模块引入环境列表时的通用拆分、去重规则，只读政策。", "read": ["policy/env-list.md"]},
        ],
        "notes": [
            _rule(0, "环境列表约定", "使用分号分隔；去首尾空白、排除空项，区分大小写，按首次出现去重；未设置或空列表返回 []。",
                  "policy/env-list.md", ["环境列表", "通知来源", "环境配置"]),
        ],
        "prompt": "在 environment.py 的 normalized_environment 中增加通知来源列表。按当期通知接入说明和既有环境列表规则处理，保留运行模式设置。",
        "reference": _code('''
            def normalized_environment(values):
                """返回运行模式和通知来源，缺少通知环境变量时返回空来源列表。"""
                parts = values.get("NOTICE_SOURCE_KEYS", "").split(";")
                keys = [part.strip() for part in parts if part.strip()]
                return {"mode": values.get("APP_MODE", "worker"), "notification_sources": list(dict.fromkeys(keys))}
        '''),
        "score": _code(r'''
            from environment import normalized_environment

            assert normalized_environment({}) == {"mode": "worker", "notification_sources": []}
            values = {"APP_MODE": "job", "NOTICE_SOURCE_KEYS": " app ; ; Web ; app;web ;  " }
            assert normalized_environment(values) == {
                "mode": "job", "notification_sources": ["app", "Web", "web"]
            }
            assert normalized_environment({"NOTICE_SOURCE_KEYS": " ; \t ;"})["notification_sources"] == []
            assert normalized_environment({"NOTICE_SOURCE_KEYS": "desk,web"})["notification_sources"] == ["desk,web"]
        '''),
    },
    {
        "id": "reuse-digest-catalog-channel",
        "title": "按正式渠道目录补齐摘要显示名",
        "kind": "reuse",
        "project": "digest",
        "target": "digest.py",
        "files": {
            "README.md": "# 摘要生成器\n发布接口只认生效目录，目录记录在 mapping/；正式入口在 maint/。\n",
            "maint/deploy-ledger.md": (
                "部署版本的摘要渠道显示名应以 mapping/production-d42.txt 为准；"
                "同目录的 trial-d43.txt 是未发布演示。\n"
            ),
            "mapping/production-d42.txt": (
                "摘要渠道：WB 对应 Web 发布，QX 对应 已归档，TN 对应 团队简报。"
                "发布映射区分大小写，其他输入显示 其他渠道。\n"
            ),
            "mapping/trial-d43.txt": "演示渠道：WB 对应 内测网页，QX 对应 快讯，TN 对应 内测简报。\n",
            "digest.py": _code('''
                def channel_label(channel):
                    """将渠道 channel 转换成摘要展示名，未知渠道沿用默认显示。"""
                    return "其他渠道"


                def heading(text):
                    """返回固定标题前缀和 text。"""
                    return "摘要 " + text
            '''),
        },
        "episodes": [
            {"prompt": "查明摘要部署时采用哪张渠道显示目录，区别于演示登记。只读部署入口。", "read": ["maint/deploy-ledger.md"]},
        ],
        "notes": [
            _reference(0, "正式摘要渠道显示目录", "mapping/production-d42.txt", "maint/deploy-ledger.md", ["摘要", "渠道", "显示名"]),
        ],
        "prompt": "补齐 digest.py 的 channel_label，按已发布的摘要渠道目录返回显示名，保持未知渠道和 heading 的行为。",
        "reference": _code('''
            def channel_label(channel):
                """按已发布目录返回渠道显示名，未知或缺省渠道返回默认显示。"""
                return {"WB": "Web 发布", "QX": "已归档", "TN": "团队简报"}.get(channel, "其他渠道")


            def heading(text):
                """返回固定标题前缀和 text。"""
                return "摘要 " + text
        '''),
        "score": _code('''
            from digest import channel_label, heading

            assert [channel_label(value) for value in ("WB", "QX", "TN")] == ["Web 发布", "已归档", "团队简报"]
            assert channel_label("wb") == "其他渠道"
            assert channel_label(None) == "其他渠道"
            assert heading("演示") == "摘要 演示"
        '''),
    },
    {
        "id": "reuse-digest-annotation-summary",
        "title": "组合订阅字段入口和单行标签约定",
        "kind": "reuse",
        "project": "digest",
        "target": "render.py",
        "files": {
            "README.md": "# 摘要生成器\n订阅的通知字段和显示约定独立维护，文档入口在 docs/。\n",
            "docs/subscription-index.txt": (
                "摘要订阅实际随 batch/sr4.txt 发布，记录当前输入和可选展示字段。"
                "batch/sr3.txt 仅用于旧版本排查。\n"
            ),
            "docs/inline-label.md": (
                "# 单行标签\n把所有连续空白折成一个 ASCII 空格，并去掉首尾空白。"
                "缺失、None 或折叠后为空时省略整段附注，不留下前缀或多余空格。\n"
            ),
            "batch/sr4.txt": (
                "新的摘要订阅记录字段 supplementary_caption。"
                "作为单行辅助说明，非空时以一个空格和方括号追加到原标题末尾。\n"
            ),
            "batch/sr3.txt": "旧版辅助标签是 label_hint，仅供历史调试。\n",
            "render.py": _code('''
                def render_summary(entry):
                    """从摘要 entry 返回一行标题。"""
                    return f"摘要 {entry['title']}"
            '''),
        },
        "episodes": [
            {"prompt": "定位当前订阅发布使用的摘要字段登记。不要打开登记正文。", "read": ["docs/subscription-index.txt"]},
            {"prompt": "记录维护旧状态标记时用到的单行标签空白处理约定。只看排版约定。", "read": ["docs/inline-label.md"]},
        ],
        "notes": [
            _reference(0, "当前摘要订阅字段登记", "batch/sr4.txt", "docs/subscription-index.txt", ["摘要", "订阅", "辅助说明"]),
            _rule(1, "单行标签约定", "连续空白折成一个 ASCII 空格并去首尾空白；缺失、None 或空白时省略整段附注。",
                  "docs/inline-label.md", ["摘要", "单行标签", "辅助说明"]),
        ],
        "prompt": "修改 render.py 的 render_summary，展示当前订阅记录的可选辅助说明。保留既有标题前缀，显示方式遵循订阅字段约定和单行标签排版规则。",
        "reference": _code('''
            def render_summary(entry):
                """用 entry 生成一行摘要标题，空附注时返回原有标题。"""
                title = f"摘要 {entry['title']}"
                caption = " ".join((entry.get("supplementary_caption") or "").split())
                return f"{title} [{caption}]" if caption else title
        '''),
        "score": _code(r'''
            from render import render_summary

            assert render_summary({"title": "长文", "supplementary_caption": " \t阶段一 \n 阶段二\u3000 "}) == "摘要 长文 [阶段一 阶段二]"
            assert render_summary({"title": "旧版", "label_hint": "忽略"}) == "摘要 旧版"
            assert render_summary({"title": "空白", "supplementary_caption": " \n  "}) == "摘要 空白"
            assert render_summary({"title": "空值", "supplementary_caption": None}) == "摘要 空值"
            assert render_summary({"title": "无附注"}) == "摘要 无附注"
        '''),
    },
]
