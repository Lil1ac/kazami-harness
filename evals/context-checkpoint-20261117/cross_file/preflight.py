"""仅运行本地 Click 预检，并生成带源码指纹的 JSON 回执。"""

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from cases import CASES, SOURCE, SOURCE_FILES, WORKSPACE
from reference import apply_reference


# 脚本、临时工作区、最终回执均放在本轮授权的跨文件评测目录内。
_ROOT = Path(__file__).resolve().parent


def _digests(directory: Path) -> dict[str, str]:
    """对 directory 中每份 Python 源码取 SHA-256，返回文件名到指纹的映射。"""
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _check(workspace: Path, identifier: str) -> dict:
    """用独立进程检查 workspace 内的 Click 副本，返回逐断言结果。"""
    process = subprocess.run(
        [sys.executable, "-I", "-B", str(_ROOT / "checks.py"), str(workspace), identifier],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return json.loads(process.stdout)


def _verified_case(case: dict, root: Path, source_hashes: dict[str, str]) -> dict:
    """在 root 下分别构建基线和参考副本，判定题目可用，并返回审计记录。"""
    evidence_path = case["evidence_file"]
    anchor = case["anchor"].encode("utf-8")
    matches = [name for name in SOURCE_FILES if anchor in (WORKSPACE / name).read_bytes()]
    if matches != [evidence_path] or (WORKSPACE / evidence_path).read_bytes().count(anchor) != 1:
        raise ValueError(f"{case['id']} 的证据锚点必须唯一")

    copies = {}
    for label in ("baseline", "reference"):
        work = root / case["id"] / label
        package = work / "click"
        package.mkdir(parents=True)
        for name in SOURCE_FILES:
            shutil.copyfile(WORKSPACE / name, package / name)
        if _digests(package) != source_hashes:
            raise ValueError(f"{label} 副本与冻结来源不一致")
        copies[label] = (work, package)

    baseline = _check(copies["baseline"][0], case["id"])
    modified = apply_reference(copies["reference"][1], case["id"])
    reference_hashes = _digests(copies["reference"][1])
    changed = [name for name in SOURCE_FILES if reference_hashes[name] != source_hashes[name]]
    if sorted(modified) != sorted(changed) or len(changed) < 2:
        raise ValueError(f"{case['id']} 参考修改并非预期的跨文件变更")
    reference = _check(copies["reference"][0], case["id"])
    if not all(row["passed"] for row in baseline["regression"].values()):
        raise ValueError(f"{case['id']} 基线回归失败：{baseline}")
    if any(row["passed"] for row in baseline["feature"].values()):
        raise ValueError(f"{case['id']} 基线已具备部分新功能：{baseline}")
    if not all(row["passed"] for group in reference.values() for row in group.values()):
        raise ValueError(f"{case['id']} 参考解未通过：{reference}")

    return {
        "id": case["id"],
        "evidence_file": evidence_path,
        "evidence_sha256": source_hashes[evidence_path],
        "unique_anchor": True,
        "history_bytes": sum((WORKSPACE / name).stat().st_size for name in case["history_files"]),
        "reference_files": {name: reference_hashes[name] for name in changed},
        "baseline": baseline,
        "reference": reference,
        "passed": True,
    }


def main() -> None:
    """核对源码版本，执行每题的基线和参考验收，原子替换正式回执。"""
    if importlib.metadata.version("click") != "8.5.0" or not SOURCE_FILES:
        raise RuntimeError("请用本项目 .venv 的 Python 执行预检，需要 Click 8.5.0")
    source_hashes = _digests(WORKSPACE)
    with TemporaryDirectory(prefix=".preflight-", dir=_ROOT) as scratch:
        rows = [_verified_case(case, Path(scratch), source_hashes) for case in CASES]
    if _digests(WORKSPACE) != source_hashes:
        raise ValueError("Click 安装目录源码在预检期间发生变化")

    report = {
        "source": SOURCE,
        "source_files": source_hashes,
        "python": sys.version.split()[0],
        "external_model_requests": 0,
        "cases": rows,
    }
    stage = _ROOT / ".preflight-stage.json"
    stage.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stage.replace(_ROOT / "preflight.json")
    print(f"4 道 Click 跨文件题通过零模型预检，回执为 {_ROOT / 'preflight.json'}")


if __name__ == "__main__":
    main()
