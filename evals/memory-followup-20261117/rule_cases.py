"""四道全新的稳定约定复用题；学习记录与新任务使用不同业务实例。"""

from textwrap import dedent


def _code(text: str) -> str:
    """将内嵌源码 text 去除统一缩进，返回保留末尾换行的文本。"""
    return dedent(text).strip() + "\n"


def _case(
    *,
    identifier: str,
    title: str,
    project: str,
    target: str,
    rule_path: str,
    rule_title: str,
    rule_body: str,
    learning_prompt: str,
    task_prompt: str,
    initial: str,
    reference: str,
    score: str,
) -> dict:
    """返回包含同一份仓库资料和已确认规则的一道题。

    identifier、title、project 为唯一题号、题名和项目。target 是这次可编辑的
    模块，rule_path、rule_title、rule_body 是独立的稳定资料路径、主题及完整
    正文。learning_prompt 对应较早的业务实例，task_prompt 对应当期需求。
    initial、reference 和 score 分别是初始代码、参考修改和独立评分源码。
    """
    return {
        "id": identifier,
        "title": title,
        "kind": "reuse",
        "project": project,
        "target": target,
        "files": {
            "README.md": f"# {project}\n稳定的业务口径保存在 handbook/。\n",
            rule_path: f"# {rule_title}\n{rule_body}\n",
            target: _code(initial),
        },
        "prompt": task_prompt + " 已确认的项目约定足以确定行为时可直接实现，必要时查阅仓库资料。",
        "episodes": [{"prompt": learning_prompt, "read": [rule_path]}],
        "notes": [{
            "episode": 0,
            "status": "active",
            "content": {
                "type": "rule",
                "title": rule_title,
                "details": {"topic": rule_title, "behavior": rule_body},
                "tags": [rule_title],
                "evidence": [rule_path],
            },
        }],
        "reference": _code(reference),
        "score": _code(score),
    }


# 学习文档描述通用行为，不出现任务里的新模块名、字段名或输入样例。
CASES = [
    _case(
        identifier="followup-rule-half-open-window",
        title="上架流水应用统一的半开时间窗",
        project="陈列盘点",
        target="shelf.py",
        rule_path="handbook/period-membership.md",
        rule_title="报告时间窗",
        rule_body=(
            "所有报告按半开范围判定整数时间点，包含开始点、排除结束点。"
            "开始为 None 表示无下界，结束为 None 表示无上界。"
            "两端相同表示空范围；两端均非 None 且开始晚于结束时抛 ValueError。"
        ),
        learning_prompt="上次巡检归档报告在整点切换，请核对并记录报告时间窗约定。只回顾归档口径。",
        task_prompt=(
            "修改 shelf.py 的 summarize_move。保留 rack 字段，新增布尔字段 recorded_in_window，"
            "按报告时间窗约定判断 row 的 stocked_minute 是否落在 begin 和 end 范围内。"
            "这三个时间量使用同一整数分钟尺度，begin 和 end 可缺省。"
        ),
        initial='''
            def summarize_move(row, begin=None, end=None):
                """返回行 row 的货架标识，begin 和 end 为可选的分钟边界。"""
                return {"rack": row["rack"]}
        ''',
        reference='''
            def summarize_move(row, begin=None, end=None):
                """返回货架和上架分钟是否落入时间窗，倒置边界抛 ValueError。"""
                if begin is not None and end is not None and begin > end:
                    raise ValueError("报告时间窗的起点晚于终点")
                minute = row["stocked_minute"]
                included = (begin is None or begin <= minute) and (end is None or minute < end)
                return {"rack": row["rack"], "recorded_in_window": included}
        ''',
        score='''
            from shelf import summarize_move

            row = {"rack": "E7", "stocked_minute": 323}
            assert summarize_move(row, 323, 324) == {"rack": "E7", "recorded_in_window": True}
            assert summarize_move(row, 322, 323)["recorded_in_window"] is False
            assert summarize_move(row, 323, 323)["recorded_in_window"] is False
            assert summarize_move(row, None, 324)["recorded_in_window"] is True
            assert summarize_move(row, 324, None)["recorded_in_window"] is False
            assert summarize_move(row)["recorded_in_window"] is True
            try:
                summarize_move(row, 324, 323)
            except ValueError:
                pass
            else:
                raise AssertionError("倒置边界应抛出 ValueError")
        ''',
    ),
    _case(
        identifier="followup-rule-signed-minor-units",
        title="报价摘要用既定金额口径输出差旅费",
        project="现场报价",
        target="quote.py",
        rule_path="handbook/money-encoding.md",
        rule_title="元转整数分",
        rule_body=(
            "金额从有效的有限十进制字符串读入，单位为元。使用精确十进制乘 100 后舍入到整数分；"
            "恰好处于两个整数分的中间时向远离零的一边舍入，贷记金额保留负号。"
            "可选金额键缺失或值为 None 时返回 None；真实零金额返回整数 0。"
        ),
        learning_prompt="回顾上一张材料结算单的舍入口径，确认正负半分和缺省金额怎么表示。只阅读通用金额约定。",
        task_prompt=(
            "修改 quote.py 的 format_quote。新增 travel_cents，由可选差旅费 travel_yuan 按项目统一的"
            "元转整数分口径转换。金额绝对值小于一万亿且最多六位小数。保留 quote_id。"
        ),
        initial='''
            def format_quote(line):
                """返回报价记录 line 的展示字段。"""
                return {"quote_id": line["quote_id"]}
        ''',
        reference='''
            from decimal import Decimal, ROUND_HALF_UP


            def format_quote(line):
                """返回报价编号和差旅费整数分，缺省金额保持 None。"""
                raw = line.get("travel_yuan")
                cents = None if raw is None else int((Decimal(raw) * 100).to_integral_value(rounding=ROUND_HALF_UP))
                return {"quote_id": line["quote_id"], "travel_cents": cents}
        ''',
        score='''
            from quote import format_quote

            assert format_quote({"quote_id": "D6", "travel_yuan": "1.005"}) == {"quote_id": "D6", "travel_cents": 101}
            for raw, expected in (("-1.005", -101), ("2.0049", 200), ("-0.004", 0),
                                  ("0.005", 1), ("0", 0), ("99999999999.995", 10000000000000)):
                result = format_quote({"quote_id": "D6", "travel_yuan": raw})
                assert type(result["travel_cents"]) is int
                assert result["travel_cents"] == expected
            assert format_quote({"quote_id": "D7"})["travel_cents"] is None
            assert format_quote({"quote_id": "D7", "travel_yuan": None})["travel_cents"] is None
        ''',
    ),
    _case(
        identifier="followup-rule-review-state",
        title="集成矩阵采用统一状态合并顺序",
        project="制品验收",
        target="run_matrix.py",
        rule_path="handbook/state-composition.md",
        rule_title="检查状态合并",
        rule_body=(
            "只有 passed、pending、blocked 三种子状态。先核对所有输入状态，出现任意其他值时"
            "抛 ValueError，即使此前已出现 blocked。有效状态中 blocked 优先，其次 pending；"
            "非空且全为 passed 才返回 passed；空集合返回 pending。输入顺序不影响结果。"
        ),
        learning_prompt="回顾旧安装清单的各检查项如何合成状态，记录完整检查状态合并规则。只查本项目既有定义。",
        task_prompt=(
            "修改 run_matrix.py 的 summarize_matrix，保留 batch_id，并给集成矩阵加 review_state。"
            "矩阵 checks 是已有检查记录列表，每条记录有 status，按检查状态合并约定汇总。"
        ),
        initial='''
            def summarize_matrix(matrix):
                """从矩阵记录 matrix 返回已有摘要字段。"""
                return {"batch_id": matrix["batch_id"]}
        ''',
        reference='''
            def summarize_matrix(matrix):
                """汇总矩阵编号和检查状态，遇到未知子状态抛 ValueError。"""
                statuses = [check["status"] for check in matrix["checks"]]
                if any(status not in ("passed", "pending", "blocked") for status in statuses):
                    raise ValueError("包含未知检查状态")
                if "blocked" in statuses:
                    state = "blocked"
                elif not statuses or "pending" in statuses:
                    state = "pending"
                else:
                    state = "passed"
                return {"batch_id": matrix["batch_id"], "review_state": state}
        ''',
        score='''
            from run_matrix import summarize_matrix


            def result(*statuses):
                """给定任意个状态字符串，返回独立构造矩阵的摘要。"""
                return summarize_matrix({"batch_id": "matrix-3", "checks": [{"status": item} for item in statuses]})


            assert result() == {"batch_id": "matrix-3", "review_state": "pending"}
            assert result("passed", "passed")["review_state"] == "passed"
            assert result("pending", "passed")["review_state"] == "pending"
            assert result("passed", "blocked", "pending")["review_state"] == "blocked"
            assert result("pending", "blocked", "passed")["review_state"] == "blocked"
            for unknown in ("unknown", None, ["pending"]):
                try:
                    result("blocked", unknown)
                except ValueError:
                    pass
                else:
                    raise AssertionError("已有阻塞项时仍须拒绝未知状态")
        ''',
    ),
    _case(
        identifier="followup-rule-shared-place",
        title="选手排位沿用并列竞赛名次",
        project="校内计时",
        target="entrant.py",
        rule_path="handbook/placing.md",
        rule_title="计时排位",
        rule_body=(
            "计时的好坏按整数耗时从小到大判断，第一名记 1。同一耗时共享名次；"
            "每行的名次等于严格比它快的行数加 1。输出仍按输入报名顺序，输入记录不得修改。"
            "空输入得到空排序结果。"
        ),
        learning_prompt="回顾旧团体计时榜出现并列用时时怎么排名，记录项目的计时排位规则。不做任何实现。",
        task_prompt=(
            "修改 entrant.py 的 qualifier_summary，为单人选拔增加 places 列表。"
            "每个参赛记录的用时字段是 elapsed_ms，按计时排位规则为每条记录给出整数名次。"
            "已有 entrants 字段保留，places 中的每项须与同位置选手对应。"
        ),
        initial='''
            def qualifier_summary(entrants):
                """返回报名顺序中的选手编号，entrants 是参赛记录列表。"""
                return {"entrants": [entry["bib"] for entry in entrants]}
        ''',
        reference='''
            def qualifier_summary(entrants):
                """按报名顺序返回编号和竞赛名次，不修改输入记录。"""
                fastest = sorted(entry["elapsed_ms"] for entry in entrants)
                places_by_time = {}
                for place, elapsed in enumerate(fastest, 1):
                    places_by_time.setdefault(elapsed, place)
                return {"entrants": [entry["bib"] for entry in entrants],
                        "places": [places_by_time[entry["elapsed_ms"]] for entry in entrants]}
        ''',
        score='''
            from entrant import qualifier_summary

            input_rows = [{"bib": "F4", "elapsed_ms": 21}, {"bib": "K3", "elapsed_ms": 12},
                          {"bib": "W7", "elapsed_ms": 21}, {"bib": "M5", "elapsed_ms": 31},
                          {"bib": "A2", "elapsed_ms": 12}]
            original = [dict(row) for row in input_rows]
            assert qualifier_summary(input_rows) == {
                "entrants": ["F4", "K3", "W7", "M5", "A2"], "places": [3, 1, 3, 5, 1]
            }
            assert input_rows == original
            assert qualifier_summary([]) == {"entrants": [], "places": []}
            assert qualifier_summary([{"bib": "A9", "elapsed_ms": 310}])["places"] == [1]
        ''',
    ),
]
