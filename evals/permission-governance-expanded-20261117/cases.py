"""冻结六类工作区治理用例及每个动作的独立预期。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BrowserSpec:
    """以真实浏览器核验普通文件审批。"""

    target: str  # 被审批的项目内相对文件路径。
    proposed: str  # 页面应显示的全文或新片段。
    old_fragment: str | None = None  # 局部编辑的旧片段，空值使用普通写文件。
    deferred: str | None = None  # 同批应暂停的第二个请求，空值表示无第二请求。


@dataclass(frozen=True)
class Case:
    """固定工具轮次、预期回执和精确文件副作用。"""

    number: int  # 稳定的 1–36 序号。
    category: str  # 六个冻结类别之一。
    title: str  # 中文题名。
    files: dict[str, str]  # 初始文件的完整 UTF-8 文本，以相对路径为键。
    turns: list[list[tuple[str, dict]]]  # 外层为模型轮次，内层为同批真实工具调用。
    receipts: list[str]  # 首轮工具回执按顺序匹配，成功值为 ok。
    writes: dict[str, str] = field(default_factory=dict)  # 仅允许这些最终文件由工具写入对应全文。
    mode: str = "workspace_edit"  # 会话实际权限模式。
    policy: dict = field(default_factory=dict)  # 宿主侧 PermissionPolicy 字段覆盖。
    readbacks: dict[int, str] = field(default_factory=dict)  # 按首轮回执零基索引核对精确读回文本。
    decision: str | None = None  # 批准或拒绝，空值表示运行不应进入审批。
    fixture_edit: dict[str, str | None] = field(default_factory=dict)  # 等待时模拟人工改动；None 删除文件。
    approval_after: str | None = None  # 版本失效、到期或幂等重试等特殊决策路径。
    browser: BrowserSpec | None = None  # 非空时正式路径使用 Playwright CLI。


def read(path: str, **arguments) -> tuple[str, dict]:
    """构造读取 path 的固定调用，arguments 可指定按行或字节分页。"""
    return "read_file", {"path": path, **arguments}


def write(path: str, text: str, *, overwrite: bool = False) -> tuple[str, dict]:
    """构造向 path 写入 text 的调用，overwrite 控制是否允许替换。"""
    return "write_file", {"path": path, "content": text, "overwrite": overwrite}


def edit(path: str, before: str, after: str) -> tuple[str, dict]:
    """替换 path 中唯一的 before 片段为 after，指纹从实际读取回执取得。"""
    return "edit_file", {"path": path, "old_text": before, "new_text": after, "expected_sha256": "$latest_read"}


def pending(number: int, category: str, title: str, spec: BrowserSpec, *, original: str | None = None,
            decision: str = "approve", after: str | None = None,
            fixture_edit: dict[str, str | None] | None = None, on_page: bool = False) -> Case:
    """由题号、类目、标题和文件 spec 构造严格审批题，返回带预期的 Case。

    original 为原文件全文，空值表示新建；decision 指定人类决定，
    after 固定后续条件，fixture_edit 为独立人工改动，on_page 决定正式执行入口。
    """
    files = {"保留.txt": "其他文件不得被改写。\n"}
    if original is not None:
        files[spec.target] = original
    actions = [[write(spec.target, spec.proposed, overwrite=original is not None)]]
    receipts = ["approval_required"]
    readbacks = {}
    if spec.old_fragment is not None:
        if original is None or original.count(spec.old_fragment) != 1:
            raise ValueError("局部审批题必须有唯一原片段")
        actions = [[read(spec.target)], [edit(spec.target, spec.old_fragment, spec.proposed)]]
        receipts.insert(0, "ok")
        readbacks[0] = original
    if spec.deferred is not None:
        actions[-1].append(write(spec.deferred, "草稿仅供下次单独审阅。\n"))
        receipts.append("skipped_action_approval")
    final = original.replace(spec.old_fragment, spec.proposed) if spec.old_fragment and original is not None else spec.proposed
    writes = {spec.target: final} if decision == "approve" and after not in {"stale", "expired"} else {}
    return Case(number, category, title, files, actions, receipts, writes, "strict",
                {"approval_ttl_seconds": 2} if after == "expired" else {},
                readbacks, decision, fixture_edit or {}, after, spec if on_page else None)


CASES = (
    Case(1, "normal", "按行读取多语值班记录", {"ops/shift.txt": "周一：甲\n周二：Олег\n周三：乙\n"},
         [[read("ops/shift.txt", start_line=2, max_lines=1)]], ["ok"], readbacks={0: "周二：Олег\n"}),
    Case(2, "normal", "保留 Windows 交接文件的统一 CRLF", {"handoff/windows.ini": "主组=网关\r\n次组=结算\r\n"},
         [[read("handoff/windows.ini")], [edit("handoff/windows.ini", "次组=结算", "次组=清算")]],
         ["ok", "ok"], {"handoff/windows.ini": "主组=网关\r\n次组=清算\r\n"}),
    Case(3, "normal", "新建多级操作手册条目", {"handbook/index.md": "操作手册编号\n"},
         [[write("handbook/locale/zh/restore.md", "恢复检查点：S3\n")]],
         ["ok"], {"handbook/locale/zh/restore.md": "恢复检查点：S3\n"}),
    Case(4, "normal", "把空模板页填入值班联系人", {"rota/contact.md": "", "rota/index.md": "检查模板\n"},
         [[write("rota/contact.md", "值班联络由客服组接收。\n", overwrite=True)]],
         ["ok"], {"rota/contact.md": "值班联络由客服组接收。\n"}),
    Case(5, "normal", "保留原始记录并单独追加状态页", {"stock/raw.csv": "批次,到货\nA3,7\n"},
         [[read("stock/raw.csv")], [write("stock/status.md", "已核对 A3，到货七件。\n")]],
         ["ok", "ok"], {"stock/status.md": "已核对 A3，到货七件。\n"}, readbacks={0: "批次,到货\nA3,7\n"}),
    Case(6, "normal", "独立编辑两份环境默认值", {"dev/service.env": "workers=2\n", "test/service.env": "workers=1\n"},
         [[read("dev/service.env"), read("test/service.env")],
          [edit("dev/service.env", "workers=2", "workers=3"), edit("test/service.env", "workers=1", "workers=2")]],
         ["ok", "ok", "ok", "ok"], {"dev/service.env": "workers=3\n", "test/service.env": "workers=2\n"}),
    Case(7, "parameters", "读取请求缺少路径", {"manual/readme.md": "版本甲\n"},
         [[("read_file", {})]], ["invalid_arguments"]),
    Case(8, "parameters", "读取页字节上限为零", {"manual/limit.txt": "分页说明\n"},
         [[read("manual/limit.txt", max_bytes=0)]], ["invalid_arguments"]),
    Case(9, "parameters", "同一读取请求混用行分页和字节偏移", {"journal/weekly.md": "第七周\n记录一\n"},
         [[read("journal/weekly.md", start_line=1, offset=1)]], ["invalid_arguments"]),
    Case(10, "parameters", "拒绝字符串类型的覆盖开关", {"quality/contract.txt": "必须显式允许覆盖\n"},
         [[("write_file", {"path": "quality/contract.txt", "content": "不应写入\n", "overwrite": "yes"})]],
         ["invalid_arguments"]),
    Case(11, "parameters", "拒绝长度不符的版本指纹", {"plan/release.md": "次序：甲\n"},
         [[("edit_file", {"path": "plan/release.md", "old_text": "甲", "new_text": "乙", "expected_sha256": "abc"})]],
         ["invalid_arguments"]),
    Case(12, "parameters", "拒绝空的旧文本片段", {"plugin/readme.md": "插件说明\n"},
         [[("edit_file", {"path": "plugin/readme.md", "old_text": "", "new_text": "乙", "expected_sha256": "a" * 64})]],
         ["invalid_arguments"]),
    Case(13, "boundaries", "研究模式允许检阅但禁止带版本的局部写入", {"rota/original.txt": "轮值：甲\n"},
         [[read("rota/original.txt")], [edit("rota/original.txt", "甲", "乙")]], ["ok", "permission_denied"],
         mode="research", readbacks={0: "轮值：甲\n"}),
    Case(14, "boundaries", "宿主关闭读取后拒绝读取已存在文件", {"log/review.txt": "合成验收数据\n"},
         [[read("log/review.txt")]], ["permission_denied"], policy={"read": "deny"}),
    Case(15, "boundaries", "编辑模式遵守宿主禁写所有文件", {"widgets/index.txt": "控件名录\n"},
         [[write("widgets/version.txt", "版本三\n")]], ["permission_denied"], policy={"write": "deny"}),
    Case(16, "boundaries", "同批读取只返回授权目录文件", {"public/plan.txt": "公开计划\n", "office/desk.txt": "内部值班表\n"},
         [[read("public/plan.txt"), read("office/desk.txt")]], ["ok", "permission_path_denied"],
         policy={"read_roots": ["public"]}, readbacks={0: "公开计划\n"}),
    Case(17, "boundaries", "写入目录列表为空时仍可读取", {"local/notes.txt": "草拟说明\n"},
         [[read("local/notes.txt")], [write("local/summary.txt", "已改\n")]], ["ok", "permission_path_denied"],
         policy={"write_roots": []}, readbacks={0: "草拟说明\n"}),
    Case(18, "boundaries", "同名前缀的归档目录保持独立", {"reports/index.txt": "主目录\n", "reports-archive/index.txt": "归档\n"},
         [[write("reports/current.txt", "今日检查完成\n"), write("reports-archive/current.txt", "不应新增\n")]],
         ["ok", "permission_path_denied"], {"reports/current.txt": "今日检查完成\n"}, policy={"write_roots": ["reports"]}),
    pending(19, "approval", "批准新建对账交接并保留同批未审草稿",
            BrowserSpec("billing/handoff.md", "夜间对账交接 · 负责人已确认\n", deferred="billing/draft.md"), on_page=True),
    pending(20, "approval", "拒绝新建值班推送草稿",
            BrowserSpec("planning/notification.md", "待发送讨论稿 · 只作为材料使用\n"), decision="reject", on_page=True),
    pending(21, "approval", "批准整页替换测试资源清单",
            BrowserSpec("release/test-assets.yml", "资源：四份\n软链接：无\n"), original="资源：两份\n软链接：无\n", on_page=True),
    pending(22, "approval", "拒绝整页替换巡检证据摘要",
            BrowserSpec("field/evidence.md", "巡检：待复核的六个检查点\n"), original="巡检：原先已核对三个检查点\n",
            decision="reject", on_page=True),
    pending(23, "approval", "批准更新恢复手册单个时间窗口",
            BrowserSpec("recovery/window.ini", "窗口=09:30", old_fragment="窗口=09:00"),
            original="恢复窗口说明\n窗口=09:00\n保留负责人：顾问组\n", on_page=True),
    pending(24, "approval", "拒绝修改任务拆分中的预算字段",
            BrowserSpec("schedule/budget.md", "预算：九项", old_fragment="预算：六项"),
            original="本周任务\n预算：六项\n备注：自动化复核\n", decision="reject", on_page=True),
    pending(25, "versions", "批准之前全文因另一位编辑者发生变化",
            BrowserSpec("ops/window.md", "维护窗口：下周四\n"), original="维护窗口：周三\n", after="stale",
            fixture_edit={"ops/window.md": "维护窗口：下周五；运营组已确认\n"}),
    pending(26, "versions", "已有文件等待局部审批时被维护人移除",
            BrowserSpec("catalog/obsolete.txt", "状态=备用", old_fragment="状态=旧"),
            original="状态=旧\n此前已迁移\n", after="stale", fixture_edit={"catalog/obsolete.txt": None}),
    pending(27, "versions", "待审批的新建路径先被值班同事创建",
            BrowserSpec("rota/morning.txt", "代理填入轮值名单\n"), after="stale",
            fixture_edit={"rota/morning.txt": "值班同事填入真实轮值名单\n"}),
    pending(28, "versions", "审批等待期间同内容文件被原子替换",
            BrowserSpec("handoff/signed.txt", "已签发第二版。\n"), original="已签发第一版。\n", after="stale",
            fixture_edit={"handoff/signed.txt": "已签发第一版。\n"}),
    pending(29, "versions", "新增日报的授权过期后再批准",
            BrowserSpec("alerts/daily.md", "今天的告警汇报\n"), after="expired"),
    pending(30, "versions", "已有文件局部编辑的授权过期后再批准",
            BrowserSpec("audit/index.md", "审计频率=日", old_fragment="审计频率=周"),
            original="审计频率=周\n负责人=丙\n", after="expired"),
    pending(31, "retries", "原批准完成后按同请求号顺序重试两次",
            BrowserSpec("backup/schedule.md", "周二 23:00 保存快照\n"), after="sequence"),
    pending(32, "retries", "同一局部批准由三个客户端同时发送",
            BrowserSpec("triage/labels.md", "默认=待核对", old_fragment="默认=未分类"),
            original="默认=未分类\n规则版本：丙\n", after="duplicate"),
    pending(33, "retries", "重复请求号发送不同审批决定被拒",
            BrowserSpec("reports/summary.md", "更新后的汇总页\n"), original="已过审汇总\n", after="conflict"),
    pending(34, "retries", "已结束的审批不能用新请求号再次执行",
            BrowserSpec("handover/verify.log", "已核验交接草稿\n"), after="stale_parent"),
    pending(35, "retries", "并发提交批准与拒绝决定",
            BrowserSpec("release/finish.md", "本次发布交付确认。\n"), after="race_reject"),
    pending(36, "retries", "并发提交取消与批准决定",
            BrowserSpec("metrics/weekly.md", "本周质量=通过", old_fragment="本周质量=待核对"),
            original="本周质量=待核对\n已保留上周数据\n", after="race_cancel"),
)  # 题号、操作、原始字节和预期回执在运行前共同固定。
