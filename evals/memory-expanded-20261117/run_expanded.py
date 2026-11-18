"""冻结及验证 36 道仓库维护题，之后在原 Runner 上执行 144 个记忆 A/B 槽位。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.memory.types import MemoryContent
from agent_harness.model_selection import valid_model_key

from fixtures import build_case, validate_case
from sources import HERE, SOURCES, read_source
from tasks_native import TASKS as ARCHIVE_TASKS
from tasks_release import TASKS as RELEASE_TASKS


sys.path.insert(0, str(HERE.parent / "memory-repository-20261117"))
import run_repository as original
sys.path.pop(0)

from evidence import TraceTransport, measured_trial, prepare_seed


OUTPUT = HERE.parent.parent / "output" / HERE.name  # 冻结题单、零模型回执和正式运行归档。
CASE_COUNT = 36  # 每个项目六题，各场景类型固定两题。
ROUNDS = (1, 2)  # 两个完全独立的 A/B 重复轮次。
TRIALS = CASE_COUNT * len(ROUNDS) * 2  # 预注册的真实模型试次总数。
MAX_CALLS = TRIALS * 12  # 保持原专项每槽最多十二个模型轮次。


def frozen_suite() -> tuple[list[dict], dict]:
    """构建固定题单与协议，返回 36 题和含源码指纹、预算及顺序的可审计清单。"""
    groups = ARCHIVE_TASKS | RELEASE_TASKS
    distributions = {}
    rows = {}
    for source in SOURCES:
        tasks = groups[source.slug]
        if Counter(task.category for task in tasks) != {"locate": 2, "knowledge": 2, "safety": 2}:
            raise ValueError(f"任务分层不符合固定配额 {source.slug}")
        text, info = read_source(source)
        info["sha256"] = original.memory_run.fingerprint(text)
        distributions[source.slug] = info
        rows[source.slug] = [build_case(source, task, tasks, text) for task in tasks]
    ordered = [rows[source.slug][ordinal] for ordinal in range(6) for source in SOURCES]
    if len({row["id"] for row in ordered}) != CASE_COUNT:
        raise ValueError("题单必须是互不重复的 36 道维护需求")
    for row in ordered:
        validate_case(row)
        for note in row["notes"]:
            MemoryContent.model_validate(note["content"])
    index = {case["id"]: ordinal for ordinal, case in enumerate(ordered, start=1)}
    order = [{"round": round_no, "case_id": case["id"], "arm": arm,
              "slot_root": f"r{round_no}/{index[case['id']]:02d}{arm}"}
             for round_no in ROUNDS for case, arm in original.schedule_for(ordered, round_no)]
    sources = tuple(HERE.glob("*.py")) + (Path(original.__file__), Path(original.memory_run.__file__))
    code = {source.name: sha256(source.read_bytes()).hexdigest() for source in sorted(sources)}
    manifest = {
        "case_count": CASE_COUNT, "trial_count": TRIALS, "repositories": distributions,
        "schedule": order, "code_sha256": code, "scorer_image": original.memory_run.SCORER_IMAGE,
        "configurations": {arm: original.config(arm).model_dump(mode="json") for arm in "AB"},
        "cases": [{"id": row["id"], "project": row["project"], "scenario": row["scenario"],
                   "title": row["title"], "target": row["target"], "evidence": row["evidence_paths"],
                   "sha256": original.memory_run.fingerprint(row)} for row in ordered],
    }
    manifest["protocol"] = original.memory_run.fingerprint(manifest)
    return ordered, manifest


def write_manifest(batch: Path, cases: list[dict], manifest: dict) -> None:
    """在班次目录 batch 持久化 manifest 与完整 cases 夹具，供离线复查。"""
    atomic_json(batch / "manifest.json", manifest)
    atomic_json(batch / "frozen-suite.json", {"manifest": manifest, "cases": cases})


async def _prepare_sample(cases: list[dict], batch: Path, report: dict, save) -> None:
    """执行 36 道实际只读学习及六条基线负控制，更新 report 与持久化 save 回执。"""
    seeds = report["seeds"]
    controls = {"locate": "click", "knowledge": "packaging", "safety": "pluggy"}
    sample = {next(case["id"] for case in cases if case["project"] == project and case["scenario"] == category)
              for category, project in controls.items()}
    workers = asyncio.Semaphore(2)

    async def learn(ordinal: int, case: dict) -> None:
        """在短编号 ordinal 独立目录准备一题 case，保留真实学习与可选两臂负控制。"""
        async with workers:
            directory = batch / "l" / f"{ordinal:02d}"
            seeds[case["id"]] = await prepare_seed(case, "A", directory)
            if case["id"] in sample:
                other = await prepare_seed(case, "B", directory)
                if other["fixture_sha256"] != seeds[case["id"]]["fixture_sha256"]:
                    raise ValueError(f"零模型负控制两组素材不一致 {case['id']}")
                for arm in "AB":
                    row = await measured_trial(case, arm, directory, "offline", None)
                    if row["http_attempts"] or row["patch_passed"] is not False:
                        raise ValueError(f"实际 Runner 的离线负控制失败 {case['id']} {arm}")
                    report["controls"].append(row)
            save()
            print(f"经过学习 {len(seeds)}/{CASE_COUNT} {case['id']}", flush=True)

    await asyncio.gather(*(learn(i, case) for i, case in enumerate(cases, start=1)))


async def preflight(cases: list[dict], manifest: dict) -> bool:
    """先冻结 cases 和 manifest，再检查独立评分、真实学习与失效探针；通过时返回 True。"""
    batch = OUTPUT / "preflight" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    write_manifest(batch, cases, manifest)
    report = {"state": "preparing", "protocol": manifest["protocol"], "cases": CASE_COUNT,
              "scores": {}, "seeds": {}, "controls": [], "external_http": 0, "errors": {}}

    def save() -> None:
        """保存本批最新零模型进度与学习和评分回执。"""
        atomic_json(batch / "batch.json", report)

    print(f"零模型预检目录 {batch}", flush=True)
    save()
    workers = asyncio.Semaphore(2)

    async def score(case: dict) -> None:
        """离线核验 case 的负基线、正参考解和可选失效探针，持续写预检原件。"""
        async with workers:
            try:
                receipt = await asyncio.to_thread(original.memory_run.scoring_preflight, case, batch)
            except (OSError, ValueError) as error:
                report["errors"][case["id"]] = str(error)
                print(f"评分预检失败 {case['id']}: {error}", flush=True)
            else:
                report["scores"][case["id"]] = receipt
                print(f"完成评分 {len(report['scores'])}/{CASE_COUNT} {case['id']}", flush=True)
            save()

    with (
        patch.object(original.memory_run, "config", original.config),
        patch.object(original.memory_run, "EvidenceTransport", TraceTransport),
    ):
        # 边学习边评参考稿，评分器的 Docker 调用只在两条线程内读挂载。
        result = await asyncio.gather(_prepare_sample(cases, batch, report, save),
                                      *(score(case) for case in cases), return_exceptions=True)
    for error in result:
        if isinstance(error, BaseException):
            report["errors"]["preflight_infrastructure"] = str(error)
    passed = not report["errors"] and len(report["scores"]) == len(report["seeds"]) == CASE_COUNT
    report["state"] = "passed" if passed else "failed"
    save()
    if passed:
        atomic_json(OUTPUT / "preflight-latest.json", {"state": "passed", "protocol": manifest["protocol"],
                    "batch": str(batch), "external_http": 0, "cases": CASE_COUNT})
    print(f"零模型预检 {report['state']}；评分 {len(report['scores'])}/{CASE_COUNT}；"
          f"学习 {len(report['seeds'])}/{CASE_COUNT}", flush=True)
    return passed


async def main() -> int:
    """根据 CLI 执行零模型预检或通过同协议预检的正式试次，以退出码报告完成情况。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="在已有全局预检后启动已批准批次。")
    parser.add_argument("--key-from-stdin", action="store_true", help="经进程匿名管道读取官方连接凭据。")
    parser.add_argument("--resume", type=Path, help="继续同一正式批次中尚无可用结果的试次。")
    parser.add_argument("--recover-transport", action="store_true", help="保留原账本，继续已核对的传输异常。")
    parser.add_argument("--freeze-only", action="store_true", help="仅冻结题单和协议，无任何模型或容器请求。")
    args = parser.parse_args()
    cases, manifest = frozen_suite()
    if args.freeze_only and not args.real:
        batch = OUTPUT / "frozen" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        write_manifest(batch, cases, manifest)
        print(batch)
        return 0
    if not args.real:
        if args.key_from_stdin or args.resume or args.recover_transport:
            parser.error("恢复及匿名凭据参数仅供 --real 使用")
        return 0 if await preflight(cases, manifest) else 1
    if not args.key_from_stdin:
        parser.error("正式密钥须经匿名管道传入")
    key = sys.stdin.readline(2048).strip()
    if not valid_model_key(key):
        raise SystemExit("官方连接没有有效凭据")
    from campaign import run_campaign
    return 0 if await run_campaign(cases, manifest, key, args.resume, args.recover_transport) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
