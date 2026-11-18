"""用单次试次的原始补丁、评分报告和全局账本生成对照表，保留未评分槽位。"""

import os
import json
from collections import Counter
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from run import DATA, MANIFEST, ROOT, RUNS, digest_bytes, read
from agent_harness.evaluation.engine import fingerprint

OUT = ROOT / "output/repository-comparison-20261118"  # 可审阅的报告、开发题补丁和入口回执。


def link(label, path):
    """返回从结果报告访问 path 的 Markdown 链接，label 为展示文本。"""
    return f"[{label}]({Path(os.path.relpath(path, OUT)).as_posix()})"


def result(case, arm):
    """按 case 与 arm 读取唯一试次，返回原始行、证据目录和状态，缺少证据时保留原因。"""
    slot = DATA / "slots" / case / arm
    for name in ("result.json", "progress.json"):
        if (slot / name).exists():
            folder = RUNS / ".evaluations" / read(slot / name)["id"]
            report = read(folder / "report.json")
            rows = report["trials"]
            if len(rows) > 1:
                raise ValueError("单次槽位出现多条试次")
            row = rows[0] if rows else {}
            if row and (row["case_id"] != case or row["strategy"] != arm):
                raise ValueError("报告指向其他槽位")
            if row.get("patch_sha256"):
                if digest_bytes((folder / (row["run_id"] + ".patch")).read_bytes()) != row["patch_sha256"]:
                    raise ValueError("送评补丁 SHA 不一致")
            return row, folder, report.get("stop_reason") or report["state"]
    check = slot.parent / "preflight.json"
    return {}, None, "环境预检失败" if check.exists() and read(check)["state"] != "preflight_passed" else "未运行"


def resolved(row):
    """读取 row 的官方 resolved；无官方判定时返回 None。"""
    value = row.get("grade", {}).get("official", {}).get("resolved")
    return value if isinstance(value, bool) else None


def publish(row, folder, target):
    """从 folder 复制 row 首次提交的补丁、评分及实际入口脚本到 target；校验所有已登记摘要。"""
    target.mkdir(parents=True, exist_ok=True)
    run_id = row["run_id"]
    originals = [(folder / (run_id + ".patch"), "candidate.patch", row.get("patch_sha256")),
                 (folder / "grades" / run_id / "official.json", "official.json", (row.get("grade_artifacts") or {}).get("score"))]
    entry = row.get("entry", {})
    if entry:
        (target / "entry.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        signature = entry.get("baseline", {}).get("script_sha256")
        if signature:
            matches = [p for p in (folder / "entries" / run_id).glob("*/repro.py") if digest_bytes(p.read_bytes()) == signature]
            if not matches:
                raise ValueError("缺少已登记入口脚本：" + run_id)
            originals.append((matches[0], "repro.py", signature))
    for path, name, expected in originals:
        if not path.exists() and not expected:
            continue
        raw = path.read_bytes()
        if expected and digest_bytes(raw) != expected:
            raise ValueError("待交付原件摘要不符：" + run_id)
        (target / name).write_bytes(raw)


def cell(row, folder, reason):
    """按 row 生成评分与证据链接；缺少试次时只显示 reason。"""
    if not row:
        return reason
    label = {True: "通过", False: "失败", None: "未评分"}[resolved(row)]
    patch = folder / (row["run_id"] + ".patch")
    execution = row.get("stop_reason") or row.get("execution_status", reason)
    amount = (row.get("cost") or {}).get("known_usd_picos", 0) / 10**12
    delivery = "完整交付" if row.get("success") else "未完整交付"
    return " · ".join([label, delivery, execution, f'{row.get("model_calls", 0)} 请求', f'{amount:.4f} 美元',
                       f'{row.get("agent_seconds", 0):.0f} 秒', link("补丁", patch) if patch.exists() else "无补丁",
                       link("报告", folder / "report.json")])


def audit_pair(case, entries, ledger, frozen_files):
    """用 case 与两组原件核对首次补丁、模型、镜像、评分和费用；返回逐项证据与不可比原因。"""
    receipts, issues = {}, []
    for arm, (row, folder, reason) in entries.items():
        if not row:
            issues.append(f"{arm} {reason}")
            continue
        report, protocol = read(folder / "report.json"), read(folder / "protocol.json")
        env = report["environments"].get(case["instance_id"], {})
        schedule = protocol["schedule"]
        run_id = row["run_id"]
        score_file = folder / "grades" / run_id / "official.json"
        if not row.get("comparison_valid"):
            issues.append(f'{arm} 试次无效（{row.get("stop_reason") or reason}）')
        if not score_file.exists() or resolved(row) is None:
            issues.append(f"{arm} 缺少官方判定")
        elif (digest_bytes(score_file.read_bytes()) != row["grade_artifacts"]["score"]
              or read(score_file) != row["grade"]["official"]):
            raise ValueError("官方评分原件与报告不一致：" + run_id)
        if resolved(row) is not None:
            # 交付件、评分器真正应用的字节及实际执行的官方脚本都必须能追溯至开跑时的证书。
            check_id = read(DATA / "slots" / case["instance_id"] / "preflight.json")["id"]
            certified = RUNS / ".evaluations" / check_id / "preflight" / case["instance_id"] / "reference"
            if (digest_bytes(score_file.with_name("candidate.patch").read_bytes()) != row["patch_sha256"]
                    or score_file.with_name("eval.sh").read_bytes() != (certified / "eval.sh").read_bytes()
                    or digest_bytes(score_file.with_name("test_output.txt").read_bytes()) != row["grade_artifacts"]["log"]):
                raise ValueError("送评补丁、官方脚本或测试日志与原件不一致：" + run_id)
        if (report["protocol_sha256"] != fingerprint(protocol) or len(schedule) != 1
                or schedule[0]["run_id"] != run_id or schedule[0]["repeat"] != 0
                or schedule[0]["strategy"] != arm or env.get("base_commit") != case["base_commit"]
                or run_id != str(uuid5(NAMESPACE_URL, f'{ledger["protocol_sha256"]}/formal/{case["instance_id"]}/{arm}'))):
            issues.append(f"{arm} 协议、槽位或基线不匹配")
        if frozen_files and protocol["runtime_hashes"] != frozen_files:
            issues.append(f"{arm} 运行时指纹偏离正式冻结版本")
        requests = [r for r in ledger["requests"] if r["run_id"] == run_id]
        known = sum(r.get("known_usd_picos", 0) for r in requests)
        held = sum(r["reserved_usd_picos"] for r in requests if r["status"] in {"reserved", "unknown"})
        cost = row.get("cost") or {}
        response_models = sorted({r.get("model") or "" for r in requests if r["status"] == "settled"})
        if (len(requests) > 60 or known + held > 10**12 or cost.get("known_usd_picos") != known
                or cost.get("reserved_usd_picos") != held or any(r["phase"] != "formal" for r in requests)
                or [r["turn"] for r in requests] != list(range(1, len(requests) + 1))):
            issues.append(f"{arm} 逐请求账本核对失败")
        if response_models != [protocol["config"]["requested_model"]]:
            issues.append(f"{arm} 逐请求响应模型与冻结标识不匹配")
        tests = row.get("grade", {}).get("official", {}).get("tests_status", {})
        counts = {key: {status: len(values) for status, values in tests[key].items()}
                  for key in ("FAIL_TO_PASS", "PASS_TO_PASS") if key in tests}
        receipts[arm] = {"run_id": run_id, "resolved": resolved(row), "tests": counts,
                         "complete_delivery": row.get("success"), "execution_status": row.get("execution_status"),
                         "stop_reason": row.get("stop_reason"), "agent_seconds": row.get("agent_seconds"),
                         "entry_status": row.get("entry", {}).get("status"), "calls": len(requests), "known_usd_picos": known,
                         "reserved_usd_picos": held, "patch_sha256": row.get("patch_sha256"),
                         "settled_response_models": response_models,
                         "calls_by_purpose": dict(Counter(r["purpose"] for r in requests)),
                         "response_model": row.get("response_model"), "protocol_sha256": report["protocol_sha256"],
                         "common": {"model": protocol["config"]["requested_model"], "generation": protocol["config"]["generation"],
                                    "limits": protocol["config"]["limits"], "environment": env}}
        if env.get("grading_http_cache") and score_file.with_name("test_output.txt").exists():
            # 从评分进程的实际日志提取命中，单有缓存挂载不足以证明测试使用了对应响应。
            prefix = "HARNESS_HTTP_REPLAY "
            hits = [json.loads(line[len(prefix):]) for line in score_file.with_name("test_output.txt")
                    .read_text(encoding="utf-8").splitlines() if line.startswith(prefix)]
            reference = read(MANIFEST.with_name("grader-http") / "responses.json")["responses"]
            fields = ("method", "url", "status", "body_sha256")
            recorded = {tuple(item[k] for k in fields) for item in reference}
            observed = {tuple(hit[k] for k in fields) for hit in hits}
            if not observed.issubset(recorded):
                raise ValueError("独立评分器使用了未冻结的响应：" + run_id)
            receipts[arm]["grading_http_replay"] = {"hits": hits, "all_recorded_seen": observed == recorded}
    if len(receipts) == 2:
        if receipts["A"]["common"] != receipts["C"]["common"]:
            issues.append("两组环境或生成参数不一致")
        if not receipts["A"]["response_model"] or receipts["A"]["response_model"] != receipts["C"]["response_model"]:
            issues.append("两组实际响应模型无法配对")
    return {"case": case["instance_id"], "groups": receipts, "issues": issues, "valid": not issues}


def render():
    """生成冻结题单的逐题结果、开发题入口证据和费用汇总，返回已有效配对的题数。"""
    OUT.mkdir(parents=True, exist_ok=True)
    protocol, ledger = read(MANIFEST), read(DATA / "ledger.json")
    formal = ["| 题号 | 先手 | AgentHarness | mini |", "|---|---|---|---|"]
    totals = {arm: Counter() for arm in ("C", "A")}
    exclusive = {"C": [], "A": []}
    ids = {"C": set(), "A": set()}
    frozen = DATA / "runtime-freeze.json"
    frozen_files = {k.removeprefix("src/agent_harness/"): v for k, v in read(frozen)["files"].items()
                    if k.startswith("src/agent_harness/")} if frozen.exists() else {}
    audits = []
    paired = 0
    paired_passes = Counter()  # 只汇总双方均有效的题，与全部终止补丁的原始通过数单列。
    closed_pairs = 0  # 已保存两个首次执行终态的题数，含基础设施异常。
    terminal_issues = []
    for case in protocol["cases"]:
        identifier = case["instance_id"]
        entries = {arm: result(identifier, arm) for arm in ("C", "A")}
        published = {}
        for arm, (row, folder, reason) in entries.items():
            sums = totals[arm]
            sums["scored"] += resolved(row) is not None
            sums["resolved"] += resolved(row) is True
            sums["complete"] += bool(row.get("success"))
            # 两组的原生交付条件有区别，另列完全相同的“正常结束且官方通过”口径。
            sums["finished_resolved"] += row.get("execution_status") == "completed" and resolved(row) is True
            sums["agent_seconds"] += row.get("agent_seconds", 0)
            ids[arm].add(str(uuid5(NAMESPACE_URL, f'{ledger["protocol_sha256"]}/formal/{identifier}/{arm}')))
            if row:
                target = OUT / "formal" / identifier / arm
                publish(row, folder, target)
                published[arm] = " · " + link("原件", target)
        audit = audit_pair(case, entries, ledger, frozen_files)
        audits.append(audit)
        if all((DATA / "slots" / identifier / arm / "result.json").is_file() for arm in ("A", "C")):
            closed_pairs += 1
            if audit["issues"]:
                terminal_issues.append(identifier + " · " + "、".join(audit["issues"]))
        if audit["valid"]:
            paired += 1
            for arm, other in (("A", "C"), ("C", "A")):
                paired_passes[arm] += resolved(entries[arm][0]) is True
                if resolved(entries[arm][0]) and not resolved(entries[other][0]):
                    exclusive[arm].append(identifier)
        formal.append(f'| {identifier} | {case["groups"][0]} | {cell(*entries["C"])}{published.get("C", "")} | '
                      f'{cell(*entries["A"])}{published.get("A", "")} |')
    difference = totals["C"]["resolved"] - totals["A"]["resolved"]
    verdict = "达到本题单目标。" if difference >= 1 else "未达到本题单目标。"
    if paired < 20:
        verdict = (f"全部 20 题各执行一次。有效配对为 {paired}/20，未满足预设的全题单验收条件。"
                   if closed_pairs == 20 else "有效配对尚未覆盖全题单，无法判定是否超越基线。")
    lines = ["# AgentHarness 与 mini-SWE-agent 的冻结对照", "",
        "C 代表 AgentHarness 混合压缩，A 代表 mini-SWE-agent 2.4.6。两组使用同一官方模型标识 deepseek-flash。"
        "逐请求保留服务端返回的模型名，模型列表仅公开 Flash 别名。", "",
        f'正式题有效配对 {paired}/20。AgentHarness 已评分 {totals["C"]["scored"]} 题，通过 {totals["C"]["resolved"]} 题。'
        f'mini 已评分 {totals["A"]["scored"]} 题，通过 {totals["A"]["resolved"]} 题。', "",
        f'在双方均有效的 {paired} 个配对中，AgentHarness 通过 {paired_passes["C"]} 题，'
        f'mini 通过 {paired_passes["A"]} 题。', "",
        verdict, "",
        f'完整交付 C {totals["C"]["complete"]} 题，A {totals["A"]["complete"]} 题。'
        f'两组 Agent 执行时长分别为 {totals["C"]["agent_seconds"]:.1f} 秒和 {totals["A"]["agent_seconds"]:.1f} 秒。', "",
        f'按共同口径“正常结束且官方通过”，C {totals["C"]["finished_resolved"]} 题，A {totals["A"]["finished_resolved"]} 题。'
        "C 的完整交付另要求对最终补丁用同一入口复验成功。A 以原生提交信号标记正常结束。"
        "主指标只使用官方 resolved。", "",
        "C 独赢：" + ("、".join(exclusive["C"]) or "暂无") + "。A 独赢：" + ("、".join(exclusive["A"]) or "暂无") + "。", "",
        *(["已终结的无效配对", "", *["- " + item for item in terminal_issues], ""] if terminal_issues else []),
        "## 开发题", ""]
    for case in protocol["development"]:
        row, folder, why = result(case, "C")
        lines.append(f"- {case} · {cell(row, folder, why)}")
        if row and folder:
            publish(row, folder, OUT / case)
            lines.append(f'  自选入口 {row.get("entry", {}).get("status", "未登记")}，补丁 {row.get("patch_bytes", 0)} 字节。')
            tests = row.get("grade", {}).get("official", {}).get("tests_status", {})
            for key, label in (("FAIL_TO_PASS", "目标"), ("PASS_TO_PASS", "回归")):
                if key in tests:
                    passes, failures = len(tests[key]["success"]), len(tests[key]["failure"])
                    lines.append(f"  {label}测试通过 {passes}/{passes + failures}。")
            review = folder / "entry-reviews" / (row["run_id"] + ".json")
            if review.exists():
                fact = read(review)
                for field, pair in fact.get("observations", {}).items():
                    lines.append(f'  字段 {field} 的实际显示为 \x60{pair["actual"]}\x60，预期为 \x60{pair["expected"]}\x60。')
            for title, source in (("实际补丁", folder / (row["run_id"] + ".patch")),
                ("独立后台请求", folder / "entry-reviews" / (row["run_id"] + ".json")),
                ("官方评分", folder / "grades" / row["run_id"] / "official.json")):
                if source.exists():
                    target = OUT / case / source.name
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    lines.append("  " + link(title, target))
    known, reserved = ledger["known_usd_picos"] / 10**12, ledger["reserved_usd_picos"] / 10**12
    lines += ["", "## 正式逐题结果", "", *formal, "", "## 费用与核验", "",
              f'整轮已接纳 {ledger["calls_admitted"]} 次请求，账本估算 {known:.6f} 美元，'
              f'未结算预留 {reserved:.2f} 美元，总上限 10 美元。', "",
              "| 阶段或组 | 请求 | 已知美元 | 未知预留美元 |", "|---|---:|---:|---:|"]
    for label, arm in (("开发 C", None), ("正式 C", "C"), ("正式 A", "A")):
        rows = [r for r in ledger["requests"] if r["run_id"] in ids[arm]] if arm else [
            r for r in ledger["requests"] if r["phase"] == "calibration"]
        lines.append(f'| {label} | {len(rows)} | {sum(r.get("known_usd_picos", 0) for r in rows)/10**12:.6f} | '
                     f'{sum(r["reserved_usd_picos"] for r in rows if r["status"] in {"unknown", "reserved"})/10**12:.2f} |')
    lines += ["", link("冻结题单", MANIFEST) + " · " + link("逐请求账本", DATA / "ledger.json") +
              " · " + link("离线回归", DATA / "offline/passed.json"), ""]
    if (DATA / "runtime-freeze.json").exists():
        lines += [link("正式版本与外部基线源码指纹", DATA / "runtime-freeze.json") + " · " +
                  link("冻结版本源文件", OUT / "正式版本.zip"), ""]
    launch = OUT / "正式开跑核验.json"
    if launch.exists():
        lines += [link("20 题预检及开跑时账本", launch) + " · " +
                  link("Sphinx-10435 评分环境补充协议", MANIFEST.with_name("grading-addendum.json")), ""]
    audit_file = OUT / "逐题核验.json"
    audit_file.write_text(json.dumps(audits, ensure_ascii=False, indent=2), encoding="utf-8")
    lines += [link("逐题镜像、模型和费用核验", audit_file), ""]
    native = OUT / "原生提交核验.json"
    if native.exists():
        lines += [link("mini 首次显式提交与终止工作树的核验", native), ""]
    browser = ROOT / "output/playwright/hv2/receipt.json"
    if browser.exists():
        lines += [link("浏览器核对与下载哈希", browser) + " · " + link("实际试次页面", browser.with_name("django-entry.png")), ""]
    diagnosis = OUT / "实验复盘.md"
    if diagnosis.exists():
        lines += [link("运行差距与具体题目", diagnosis) + " · " + link("开发前的七道失败题", OUT / "开发前的七道失败题.md"), ""]
    (OUT / "对照结果.md").write_text("\n".join(lines), encoding="utf-8")
    return paired


if __name__ == "__main__":
    print("valid pairs", render())
