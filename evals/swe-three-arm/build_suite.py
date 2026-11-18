"""从固定 SWE-bench Verified 快照物化获批题单；选题只使用元数据。"""

import hashlib
import io
import json
from pathlib import Path
import urllib.request
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent  # 题单和冻结任务文件的宿主目录。
DEV = ["django__django-16082", "django__django-17029", "pytest-dev__pytest-5262",
       "pytest-dev__pytest-7521", "sympy__sympy-22914", "sympy__sympy-23950"]  # 只作开发校准。


def build() -> None:
    """下载固定快照并保存任务与套件；不运行容器或模型，失败时不替换已有冻结题单。"""
    selection = json.loads((ROOT / "selection.json").read_text(encoding="utf-8-sig"))
    url = "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/resolve/" + selection["revision"] + "/data/test-00000-of-00001.parquet"
    with urllib.request.urlopen(url, timeout=120) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("数据快照大小超限")
    rows = pq.read_table(io.BytesIO(raw)).to_pylist()
    if len(rows) != 500:
        raise ValueError("固定快照必须含500条任务")
    by_id = {row["instance_id"]: row for row in rows}
    groups = {}
    for identifier, repo, difficulty in selection["selected"]:
        groups.setdefault((repo, difficulty), []).append(identifier)
    for (repo, difficulty), chosen in groups.items():
        candidates = [row["instance_id"] for row in rows if row["repo"] == repo and row["difficulty"] == difficulty
                      and row["instance_id"] not in selection["excluded"]]
        ranked = sorted(candidates, key=lambda identifier: hashlib.sha256((str(selection["seed"]) + ":" + identifier).encode()).hexdigest())
        if ranked[:len(chosen)] != chosen:
            raise ValueError("题单不符合冻结分层哈希抽样")
    records = ROOT / "records"
    records.mkdir(exist_ok=True)
    identifiers = DEV + [row[0] for row in selection["selected"] + selection["reserves"]]
    for identifier in identifiers:
        record = by_id[identifier]
        encoded = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        target = records / (identifier + ".json")
        if target.exists() and target.read_text(encoding="utf-8") != encoded:
            raise ValueError("已有任务记录与固定快照不一致")
        target.write_text(encoded, encoding="utf-8")
    for name, ids, split in (("calibration", DEV, "dev"), ("formal", [row[0] for row in selection["selected"]], "test")):
        suite = {"name": "SWE-bench Verified 分层三组对照 · " + name, "version": "20261115",
            "provenance": "princeton-nlp/SWE-bench_Verified@" + selection["revision"] + "；分层50题子集，非全量500题成绩。",
            "kind": "context", "cases": [{"id": identifier, "project": by_id[identifier]["repo"],
                "category": by_id[identifier]["difficulty"], "split": split, "prompt": by_id[identifier]["problem_statement"],
                "backend": "swebench", "source": "records/" + identifier + ".json"} for identifier in ids]}
        (ROOT / (name + ".json")).write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {"url": url, "revision": selection["revision"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
               "records": len(identifiers), "selection_sha256": hashlib.sha256((ROOT / "selection.json").read_bytes()).hexdigest()}
    (ROOT / "source.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    build()
