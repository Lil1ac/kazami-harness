"""AgentHarness 规则试点的脱敏历史、任务输入和本机独立期望。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PilotTask:
    """用真实项目契约派生的独立交付题；模型只读取题面和 input.json。"""

    id: str  # 冻结后用于配对和回溯的任务编号。
    category: str  # 记忆收益或保护场景的分类。
    prompt: str  # 发给答题模型的输出契约。
    inputs: dict  # 完全合成的现场数据，以场景编号为键。
    expected: dict  # 仅供本机参考解和独立评分使用的期望。
    source: str  # 本机回查路径及提交号，不送入模型。
    required: tuple[str, ...] = ()  # 应实际进入模型输入的历史编号。
    forbidden: tuple[str, ...] = ()  # 不得返回或注入的撤回或跨项目历史。


# 历史按相对顺序重放；这些段落是契约摘要，使用合成时间，不是原始会话或源码。
HISTORY = (
    ("scope-lock", "2025-01-01", "调度资源约定已确认：同一会话、相同仓库标识的不同工作树、目录组件层级重叠、声明共享资源组重叠，任一条件成立都要串行。路径按组件比较，名称前缀相同不构成父子关系。"),
    ("queue-scan", "2025-01-02", "队列按接纳顺序查找符合资源条件的请求。队首被阻塞时，继续检查后续独立项目。每次选中的请求在下一次判断前先占用资源和一个并发槽。"),
    ("offline-timing", "2025-01-03", "定时规则从最早尚未处理的时刻开始。延迟最多 60 秒仍按该时刻触发。超过 60 秒默认跳过；开启补跑时只选择严格晚于窗口起点且不晚于当前时刻的最近一次，不逐次补发。"),
    ("trigger-overlap", "2025-01-04", "同一个定时规则仍有活动执行时，新触发默认跳过。设为合并时，若收到更晚的有效触发，待执行位置只保留这个最近时刻。跳过的触发不覆盖已有待执行记录。"),
    ("legacy-start", "2025-01-05", "早期单槽版本的记录：重启只恢复读取，重启本身不会启动或补发队列请求。这条记录描述引入可选启动恢复之前的行为。"),
    ("safe-start", "2025-01-06", "后续版本已加入可选启动恢复，默认关闭。只有尚未认领的排队请求，且开关开启、来源和固定配置复核通过，才重新进入执行。已认领的请求若有可信终态且清理完成或无需清理，只对账。缺少可信终态或清理未确认的已认领请求进入待核对，保持资源阻塞。"),
    ("run-status", "2025-01-07", "运行摘要分别表达执行、独立验收和清理。清理未知或不完整优先显示需检查清理。执行完成却无独立检查，项目运行显示完成·未测试，普通目录显示完成·未验收。只有独立检查通过才显示测试通过或验收通过，模型自报成功不作为验收回执。"),
    ("grant-binding", "2025-01-08", "自动化持续许可按当前规则版本、授权代次和环境身份核对。来源运行记录的版本和代次必须与当前规则完全一致，环境身份也必须匹配，并且当前许可仍存在，才可继续使用该许可。"),
    ("grant-revocation", "2025-01-09", "撤销持续许可会清除许可并提升授权代次。之后为规则建立新许可时，旧代次的运行也不能借用新许可。缺失当前许可、身份不匹配或授权已过期时，保留待人工核对。"),
    ("withdrawn-sentinel", "2025-01-10", "试验用旧授权建议：旧代次可在重新授权后直接沿用。此项是已撤回的负对照。"),
    ("foreign-sentinel", "2025-01-11", "另一个试验项目使用兼容授权代次，旧运行允许跨代复用。此项只适用于该独立项目。"),
)

PROVENANCE = {
    "scope-lock": "fbb5745，docs/SCHEDULING.md:9，src/agent_harness/execution/scheduling.py:76",
    "queue-scan": "fbb5745，docs/SCHEDULING.md:34",
    "offline-timing": "31a6c9b，docs/AUTOMATIONS.md:20，src/agent_harness/automations/service.py:466",
    "trigger-overlap": "31a6c9b，docs/AUTOMATIONS.md:22，src/agent_harness/automations/service.py:315",
    "legacy-start": "737c730:docs/LONG_RUNNING.md（早期单槽版本的重启约定）",
    "safe-start": "fbb5745，docs/SCHEDULING.md:45，src/agent_harness/execution/scheduling.py:111",
    "run-status": "3bdcef1，frontend/src/components/RunStatus.tsx:55，docs/PRD.md:39",
    "grant-binding": "31a6c9b，docs/AUTOMATIONS.md:34，src/agent_harness/automations/authorization.py:30",
    "grant-revocation": "31a6c9b，docs/AUTOMATIONS.md:40",
    "withdrawn-sentinel": "合成负对照，回放时明确撤回",
    "foreign-sentinel": "合成负对照，仅写入独立项目的索引",
}  # 证据编号到本机出处的对照，仅供人工审阅。


def job(identifier: str, path: str, *, session: str | None = None,
        repository: str | None = None, groups: tuple[str, ...] = ()) -> dict:
    """返回合成作业。path 为无盘符目录组件，None 表示无会话或仓库，groups 为空表示未声明共享资源。"""
    return {"id": identifier, "path": path, "session": session,
            "repository": repository, "groups": list(groups)}


TASKS = (
    PilotTask(
        "resource-admission", "既有约定",
        "根据项目已有的并发调度约定，为 input.json 中每个场景判断本次能从 waiting 接纳哪些作业。"
        "active 包含已占槽的作业，capacity 是总槽数，waiting 已按接纳顺序给出。路径是预先规范化的合成目录，"
        "空会话或仓库标识表示未设置。将每个场景编号映射到本次接纳的作业 ID 列表，写入 result.json。",
        {
            "repo_and_skip": {"capacity": 2, "active": [job("R0", "areas/one", repository="repoA")],
                              "waiting": [job("R1", "trees/one", repository="repoA"), job("R2", "areas/two")]},
            "path_components": {"capacity": 3, "active": [job("P0", "areas/core")],
                                "waiting": [job("P1", "areas/core/child"), job("P2", "areas/coreplus")]},
            "session_and_group": {"capacity": 3, "active": [job("S0", "areas/one", session="threadA", groups=("sharedA",))],
                                  "waiting": [job("S1", "areas/two", session="threadA"),
                                              job("S2", "areas/three", groups=("sharedA",)), job("S3", "areas/four")]},
            "claim_during_scan": {"capacity": 2, "active": [],
                                  "waiting": [job("C1", "areas/a", repository="repoB"),
                                              job("C2", "areas/b", repository="repoB"), job("C3", "areas/c"), job("C4", "areas/d")]},
            "capacity_full": {"capacity": 1, "active": [job("F0", "areas/one")], "waiting": [job("F1", "areas/two")]},
        },
        {"repo_and_skip": ["R2"], "path_components": ["P2"], "session_and_group": ["S3"],
         "claim_during_scan": ["C1", "C3"], "capacity_full": []},
        "fbb5745，调度资源冲突和阻塞队首绕行",
        ("scope-lock", "queue-scan"),
    ),
    PilotTask(
        "catchup-and-overlap", "跨会话综合",
        "按项目原有的离线漏跑和同规则重叠约定，核对 input.json 中每个时钟刻度。now、due 和 pending 为秒，"
        "due 从最早尚未处理的触发时刻起，可能含未来时刻。window 是补跑窗口秒数，null 表示关闭。"
        "busy 表示已有活动执行，merge 表示选择合并。只判断此次新触发，不释放已有待执行项。"
        "把场景编号映射到 {start, pending}，值分别为本次立即执行和处理后待执行的时刻，缺项用 null。写 result.json。",
        {
            "grace_edge": {"now": 300, "due": [240, 300], "window": None, "busy": False, "merge": False, "pending": None},
            "missed_default": {"now": 300, "due": [180, 300], "window": None, "busy": False, "merge": False, "pending": None},
            "latest_only": {"now": 300, "due": [120, 210, 240, 300, 360], "window": 120, "busy": False, "merge": False, "pending": None},
            "window_edge": {"now": 300, "due": [180], "window": 120, "busy": False, "merge": False, "pending": None},
            "merge_busy": {"now": 300, "due": [120, 285], "window": 120, "busy": True, "merge": True, "pending": 270},
            "skip_busy": {"now": 300, "due": [300], "window": None, "busy": True, "merge": False, "pending": None},
        },
        {"grace_edge": {"start": 240, "pending": None}, "missed_default": {"start": None, "pending": None},
         "latest_only": {"start": 300, "pending": None}, "window_edge": {"start": None, "pending": None},
         "merge_busy": {"start": None, "pending": 285}, "skip_busy": {"start": None, "pending": None}},
        "31a6c9b，补跑窗口选择和同规则触发合并",
        ("offline-timing", "trigger-overlap"),
    ),
    PilotTask(
        "restart-disposition", "知识更新",
        "按项目最新的启动恢复约定核对 input.json。claimed 表示已认领，terminal 表示已有可信运行终态，"
        "cleanup 是清理结果，resume 是启动恢复开关，source_valid 是固定来源及配置复核结果。"
        "将场景编号映射为 resume（可重新调度）、hold（本次不调度尚未认领的请求）、reconcile（只对账）或 needs_review（待核对）。"
        "只写 result.json，不执行任何恢复动作。",
        {
            "queued_opt_in": {"claimed": False, "terminal": False, "cleanup": "not_needed", "resume": True, "source_valid": True},
            "queued_default": {"claimed": False, "terminal": False, "cleanup": "not_needed", "resume": False, "source_valid": True},
            "source_changed": {"claimed": False, "terminal": False, "cleanup": "not_needed", "resume": True, "source_valid": False},
            "ended": {"claimed": True, "terminal": True, "cleanup": "complete", "resume": True, "source_valid": True},
            "uncertain_cleanup": {"claimed": True, "terminal": True, "cleanup": "unknown", "resume": True, "source_valid": True},
            "no_receipt": {"claimed": True, "terminal": False, "cleanup": "unknown", "resume": False, "source_valid": True},
        },
        {"queued_opt_in": "resume", "queued_default": "hold", "source_changed": "hold",
         "ended": "reconcile", "uncertain_cleanup": "needs_review", "no_receipt": "needs_review"},
        "737c730 → fbb5745，启动恢复与终态对账演进",
        ("safe-start",),
    ),
    PilotTask(
        "unverified-display", "证据不足",
        "按已有运行摘要约定，根据 input.json 生成 result.json。execution 是执行状态，validation 是独立检查状态，"
        "cleanup 是清理状态，project 表示按项目文案展示，reported_success 是模型自述。"
        "每个场景输出简化的状态码之一：cleanup_review、completed_untested、completed_unverified、tested、verified、failed、cancelled。"
        "用场景编号作为键。",
        {
            "project_claim_only": {"execution": "completed", "validation": "unverified", "cleanup": "complete", "project": True, "reported_success": True},
            "plain_claim_only": {"execution": "completed", "validation": "unverified", "cleanup": "not_needed", "project": False, "reported_success": True},
            "unclean_despite_check": {"execution": "completed", "validation": "passed", "cleanup": "unknown", "project": True, "reported_success": True},
            "project_checked": {"execution": "completed", "validation": "passed", "cleanup": "complete", "project": True, "reported_success": False},
            "plain_checked": {"execution": "completed", "validation": "passed", "cleanup": "not_needed", "project": False, "reported_success": False},
            "cancelled": {"execution": "cancelled", "validation": "unverified", "cleanup": "complete", "project": False, "reported_success": False},
            "failed": {"execution": "failed", "validation": "unverified", "cleanup": "complete", "project": True, "reported_success": True},
        },
        {"project_claim_only": "completed_untested", "plain_claim_only": "completed_unverified",
         "unclean_despite_check": "cleanup_review", "project_checked": "tested", "plain_checked": "verified",
         "cancelled": "cancelled", "failed": "failed"},
        "3bdcef1，运行详情的独立检查及清理状态展示",
        ("run-status",),
    ),
    PilotTask(
        "revoked-grant", "撤回与项目隔离",
        "按当前项目的自动化持续许可约定核对 input.json。origin 和 current 分别是来源运行冻结记录及当前规则。"
        "两者均为 [revision, generation, environment] 三元组，三个值均为合成标识。has_grant 表示当前许可存在。"
        "本题只核对持续许可是否匹配，其他动作校验已通过。将场景编号映射为 allowed 或 needs_review，写 result.json。",
        {
            "new_grant_old_run": {"origin": [7, 2, "EnvA"], "current": [7, 4, "EnvA"], "has_grant": True},
            "revoked_now": {"origin": [7, 2, "EnvA"], "current": [7, 3, "EnvA"], "has_grant": False},
            "same_generation_other_scope": {"origin": [7, 4, "EnvB"], "current": [7, 4, "EnvA"], "has_grant": True},
            "revision_updated": {"origin": [6, 4, "EnvA"], "current": [7, 4, "EnvA"], "has_grant": True},
            "exact_current": {"origin": [7, 4, "EnvA"], "current": [7, 4, "EnvA"], "has_grant": True},
            "missing_despite_match": {"origin": [7, 4, "EnvA"], "current": [7, 4, "EnvA"], "has_grant": False},
        },
        {"new_grant_old_run": "needs_review", "revoked_now": "needs_review", "same_generation_other_scope": "needs_review",
         "revision_updated": "needs_review", "exact_current": "allowed", "missing_despite_match": "needs_review"},
        "31a6c9b，自动化授权代次与来源环境核验",
        ("grant-binding", "grant-revocation"), ("withdrawn-sentinel", "foreign-sentinel"),
    ),
    PilotTask(
        "failure-draft-append", "无关任务控制",
        "为 input.json 的每个场景生成追加失败证据后的草稿文本。block 已格式化且非空。若 existing 已包含完全相同的 block，"
        "原样返回 existing；否则在 existing（空字符串时改用 default）后追加两个换行和 block。保留所有原始空白。"
        "输出以场景编号为键、最终草稿为字符串的 result.json。",
        {
            "empty": {"existing": "", "default": "请继续处理。", "block": "记录甲\n原因一"},
            "same_block": {"existing": "已有\n\n记录甲\n原因一\n备注", "default": "默认", "block": "记录甲\n原因一"},
            "different_block": {"existing": "已有\n\n记录甲\n原因一", "default": "默认", "block": "记录甲\n原因二"},
            "whitespace": {"existing": " 保留空白 \n", "default": "默认", "block": "记录乙"},
        },
        {"empty": "请继续处理。\n\n记录甲\n原因一", "same_block": "已有\n\n记录甲\n原因一\n备注",
         "different_block": "已有\n\n记录甲\n原因一\n\n记录甲\n原因二", "whitespace": " 保留空白 \n\n\n记录乙"},
        "3bdcef1，frontend/src/runOutput.ts:108 的草稿保留与去重",
    ),
)  # 六道独立新任务，场景及参考期望只来自冻结的试点规格。
