"""冻结未参与开发的正式题、配对顺序和资源限制，仅按仓库、难度和题号选择。"""

import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]  # 项目目录，用来审计已有评测暴露。
HERE = Path(__file__).resolve().parent  # 固定协议存放在版本管理内。
PRIVATE = ROOT / ".runtime/hv2"  # 含上游评分数据的文件仅供宿主读取。
SEED = "agentharness-holdout-20261118-v1"  # 选题和正式运行顺序使用不同哈希域。


def digest(raw):
    """返回原始字节 raw 的十六进制 SHA-256。"""
    return hashlib.sha256(raw).hexdigest()


def put(path, value):
    """在 path 首次写入稳定 JSON，重放时校验已有字节，差异触发异常。"""
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"冻结文件发生变化：{path}")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
    return digest(raw)


def history():
    """返回历史正式、开发、候补和单题记录中出现过的题号，不以得分过滤。"""
    args = ["rg", "--no-filename", "--no-line-number", "--no-heading", "-o", "--hidden",
            "--no-ignore", "-I", "-g", "!repository-comparison/**", "-g", "!records/**",
            "-g", "!node_modules", "-g", "!.git", "-g", "!workspaces", "-g", "!site-packages",
            "-g", "!backend-dist", "-g", "!pkg", "-g", "!w", "-g", "!work", "-g", "!cache",
            "-g", "report.json", "-g", "suite.json", "-g", "selection.json", "-g", "formal.json",
            "-g", "calibration.json", "-g", "record.json", "-g", "case.json", "-g", "*.md",
            r"[A-Za-z0-9._-]+__[A-Za-z0-9._-]+-[0-9]+", "evals", "output", "docs",
            ".runtime/ev3/paid/runs/.evaluations", ".runtime/ev3/paid/suites", ".runtime/swe-real",
            ".runtime/swe-pilot", ".runtime/swe-v2/r"]
    output = subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, check=True).stdout.decode()
    return sorted(set(output.splitlines()) | {p.stem for p in (ROOT / "evals/swe-three-arm/records").glob("*.json")})


def allocate(counts, seats):
    """按各组样本数 counts 分配 seats 个名额；最大余数相同时按组名升序。"""
    total = sum(counts.values())
    assigned = {k: n * seats // total for k, n in counts.items()}
    for k in sorted(counts, key=lambda k: (-(counts[k] * seats % total), k))[:seats - sum(assigned.values())]:
        assigned[k] += 1
    return assigned


def freeze(source):
    """校验 source 快照，写出未见题协议与隔离的单题清单；已有协议固定原排除表。"""
    meta = json.loads((ROOT / "evals/swe-three-arm/source.json").read_bytes())
    if digest(source.read_bytes()) != meta["dataset_sha256"]:
        raise ValueError("上游快照 SHA 不匹配")
    table = pq.read_table(source, columns=["instance_id", "repo", "difficulty", "base_commit"]).to_pylist()
    ids = {r["instance_id"] for r in table}
    if len(table) != 500 or len(ids) != 500:
        raise ValueError("上游题单不完整")
    target = HERE / "protocol.json"
    excluded = json.loads(target.read_bytes())["excluded"] if target.exists() else sorted(set(history()) & ids)
    eligible = [r for r in table if r["instance_id"] not in excluded]
    chosen = []
    for repo, seats in sorted(allocate(Counter(r["repo"] for r in eligible), 20).items()):
        rows = [r for r in eligible if r["repo"] == repo]
        for level, n in allocate(Counter(r["difficulty"] for r in rows), seats).items():
            group = [r for r in rows if r["difficulty"] == level]
            chosen.extend(sorted(group, key=lambda r: digest((SEED + ":pick:" + r["instance_id"]).encode()))[:n])
    chosen.sort(key=lambda r: digest((SEED + ":order:" + r["instance_id"]).encode()))
    assert len(chosen) == 20 and len({r["instance_id"] for r in chosen}) == 20
    # 选题已完成后，才读取每题评分字段并物化到宿主私有目录。
    records = {r["instance_id"]: r for r in pq.read_table(source).to_pylist()}
    for index, row in enumerate(chosen):
        case = row["instance_id"]
        record = records[case]
        folder = PRIVATE / "cases" / case
        row["record_sha256"] = put(folder / "record.json", record)
        row["groups"] = ["C", "A"] if index % 2 == 0 else ["A", "C"]
        put(folder / "suite.json", {"name": case, "version": "20261118", "provenance": meta["revision"],
            "cases": [{"id": case, "project": row["repo"], "category": row["difficulty"], "split": "test",
                       "prompt": record["problem_statement"], "backend": "swebench", "source": "record.json"}]})
    protocol = {"revision": meta["revision"], "dataset_sha256": meta["dataset_sha256"], "seed": SEED,
        "sampling": "历史评测涉及和已物化的正式、候补、开发题全部排除。按仓库、仓库内难度两级最大余数分配，层内按固定 SHA 升序。正式顺序独立哈希，交替先手。",
        "excluded": excluded, "eligible": len(eligible), "cases": chosen, "development": [
            "django__django-13512", "matplotlib__matplotlib-24627", "astropy__astropy-14369", "django__django-11239"],
        "max_usd": 10, "per_trial": {"calls": 60, "usd": 1, "agent_seconds": 1200},
        "groups": {"A": "mini-SWE-agent==2.4.6", "C": "AgentHarness hybrid"},
        "config_sha256": digest((ROOT / "evals/swe-three-arm/deepseek.toml").read_bytes()),
        "grading": "SWE-bench 4.1.0。每组每题一次、首次交付或终止时补丁，独立评分。保留异常、超时、未知用量和未运行题。完整20对上 C resolved 至少多1题才达标。"}
    print("protocol", put(target, protocol), "excluded", len(excluded), "eligible", len(eligible), flush=True)


if __name__ == "__main__":
    freeze(Path(sys.argv[1]))
