"""用冻结评分器核对 mini 原生提交与协议采集的终止工作树；仅读取首次试次，无模型调用。"""

import asyncio
import json
from filelock import FileLock

from agent_harness.config import load_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.grading_http import CachedGradingEnvironment
from agent_harness.evaluation.repository import RepositoryEnvironment
from report import OUT, result
from run import CONFIG, DATA, GRADER, MANIFEST, digest_bytes, http_cache_for, read


async def check(case, cfg):
    """按冻结 case 核对 A 组终态，使用 cfg 的资源配置，返回原生提交检查回执，无终态时返回 None。"""
    identifier = case["instance_id"]
    row, folder, _ = result(identifier, "A")
    if not row:
        return None
    run_id = row["run_id"]
    trajectory = folder / (run_id + ".trajectory.json")
    if not trajectory.exists():
        return {"case": identifier, "status": "缺少轨迹", "run_id": run_id}
    raw = trajectory.read_bytes()
    if digest_bytes(raw) != row["trajectory_sha256"]:
        raise ValueError("原生轨迹摘要不一致：" + run_id)
    info = json.loads(raw)["info"]
    if info.get("exit_status") != "Submitted":
        return {"case": identifier, "status": "未显式提交，采用终止工作树", "run_id": run_id}

    # submission 是上游预测文件采用的字符串，连末尾换行也原样保留。
    content = info["submission"]
    data = content.encode("utf-8")
    target = OUT / "formal" / identifier / "A"
    target.mkdir(parents=True, exist_ok=True)
    (target / "submitted.patch").write_bytes(data)
    standard = folder / "grades" / run_id
    report = read(folder / "report.json")
    image = report["environments"][identifier]["image_id"]
    identity = {"case": identifier, "run_id": run_id, "trajectory_sha256": row["trajectory_sha256"],
                "submitted_sha256": digest_bytes(data), "graded_sha256": row["patch_sha256"], "image_id": image,
                "eval_sha256": digest_bytes((standard / "eval.sh").read_bytes())}
    original = row["grade"].get("official", {}).get("resolved")
    if data == (standard / "candidate.patch").read_bytes():
        return {**identity, "status": "与送评补丁逐字节一致", "frozen_resolved": original, "submitted_resolved": original}
    review = target / "submission-review.json"
    work = DATA / "submission-checks" / run_id
    if review.exists():
        saved = read(review)
        verified = work / saved["grade_directory"]
        if (any(saved.get(k) != v for k, v in identity.items()) or
                digest_bytes((verified / "candidate.patch").read_bytes()) != identity["submitted_sha256"] or
                digest_bytes((verified / "eval.sh").read_bytes()) != identity["eval_sha256"] or
                (saved.get("official_sha256") and digest_bytes((target / "submitted-official.json").read_bytes())
                 != saved["official_sha256"])):
            raise ValueError("原生提交复核原件已变更：" + run_id)
        return saved

    # 全部字节不同的显式首次提交使用同一规则独立计分。保留原工作树评分，原始槽位只读。
    record_raw = (DATA / "cases" / identifier / "record.json").read_bytes()
    if digest_bytes(record_raw) != case["record_sha256"]:
        raise ValueError("题目记录已变更：" + identifier)
    material = work / "environment"
    material.mkdir(parents=True, exist_ok=True)
    (material / "record.json").write_bytes(record_raw)
    args = (json.loads(record_raw), material, cfg, GRADER)
    cache = http_cache_for(identifier)
    env = CachedGradingEnvironment(*args, http_cache=cache) if cache else RepositoryEnvironment(*args)
    env.image = image
    env.spec = {"script": (standard / "eval.sh").read_bytes().decode("utf-8")}
    # 进程中断时保留之前的日志，已有官方评分则从原件完成余下发布步骤。
    seq = 1
    grade_dir = work / "grade-1"
    while grade_dir.exists() and not (grade_dir / "official.json").is_file():
        seq += 1
        grade_dir = work / f"grade-{seq}"
    if grade_dir.exists():
        official_result = read(grade_dir / "official.json")
        graded = {"status": "passed" if official_result["resolved"] else "failed", "official": official_result}
    else:
        graded = await env.grade(content, grade_dir)
    if (digest_bytes((grade_dir / "candidate.patch").read_bytes()) != identity["submitted_sha256"] or
            digest_bytes((grade_dir / "eval.sh").read_bytes()) != identity["eval_sha256"]):
        raise ValueError("复核评分器实际使用的补丁或脚本与原件不符：" + run_id)
    receipt = {**identity, "status": graded["status"], "reason": graded.get("reason"), "frozen_resolved": original,
               "submitted_resolved": graded.get("official", {}).get("resolved"), "model_requests": 0,
               "grade_directory": grade_dir.name}
    official = grade_dir / "official.json"
    if official.exists():
        official_raw = official.read_bytes()
        (target / "submitted-official.json").write_bytes(official_raw)
        receipt["official_sha256"] = digest_bytes(official_raw)
    atomic_json(review, receipt)
    return receipt


async def main():
    """逐一读取冻结题单的已结束槽位，写出可审阅的提交核验汇总；返回 None。"""
    cfg = load_config(CONFIG)
    rows = []
    for case in read(MANIFEST)["cases"]:
        row = await check(case, cfg)
        if row:
            rows.append(row)
            print(row["case"], row["status"], flush=True)
    atomic_json(OUT / "原生提交核验.json", rows)


if __name__ == "__main__":
    with FileLock(str(DATA / "submission-checks.lock"), timeout=0):
        asyncio.run(main())
