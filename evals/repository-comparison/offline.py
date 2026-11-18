"""重放历史真实失败和成功入口，检查工作树提示是否能区分空补丁与源码修改。"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from agent_harness.evaluation.repository import RepositoryEnvironment, RepositoryExecutor
from agent_harness.evaluation.reproduction import RepositoryDelivery, coverage_review, replay

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository-entry"))
from verify import DJANGO, PROBE, PYTHON, RECORDS, ROOT, read, save, settings  # noqa: E402


async def run():
    """以已有 Docker 镜像复验两个开发题，保存真实观察和独立评分，失败时抛异常。"""
    base = ROOT / ".runtime/hv2/offline"
    history = ROOT / "output/repository-entry-20261118"
    for case in (DJANGO, "matplotlib__matplotlib-24627"):
        folder = base / case
        record = read(RECORDS / (case + ".json"))
        env = RepositoryEnvironment(record, folder / "env", settings(), PYTHON)
        identity = await env.prepare()
        facts = {"image_id": identity["image_id"], "model_calls": 0}
        if case == DJANGO:
            for label, script, patch in (
                ("helper", history / "agent-repro.py", ""), ("base", PROBE, ""),
                ("partial", PROBE, (history / "django-13512.patch").read_bytes().decode()),
                ("correct", PROBE, record["patch"]),
            ):
                fact = await replay(env, script.read_bytes(), patch, folder / label, 120, ("admin", ""))
                facts[label] = fact
                if label == "helper":
                    assert fact["status"] == "failed" and not fact["observed_entries"], fact
                else:
                    assert any(r["status"] == 200 and r["admin"] for r in fact["observed_entries"]), fact
                    assert fact["status"] == ("passed" if label == "correct" else "failed"), fact
            partial = facts["partial"]["observations"]
            assert partial["editable"]["actual"] == partial["editable"]["expected"]
            assert partial["readonly"]["actual"] != partial["readonly"]["expected"]
            paid = base.parent / "slots" / case / "C/result.json"
            if paid.exists():
                report = read(base.parent / "r/.evaluations" / read(paid)["id"] / "report.json")
                proof = report["trials"][0]["entry"]["candidate"]
                # 读取封存的自主用例观察，测试新收尾规则；不修改历史官方结论。
                facts["coverage_review"] = coverage_review("admin", proof)
                assert facts["coverage_review"] and coverage_review("admin", facts["correct"]) is None
        else:
            tree = base / ("w-" + uuid4().hex[:8])
            await env.workspace(tree)
            await env.start_session(tree)
            try:
                await env.verify_workspace(tree)
                exe = RepositoryExecutor(env, tree, base / "runs", settings())
                exe.delivery = RepositoryDelivery(env, tree, folder / "entries", record["problem_statement"])
                # 八批真实 Git 采样一次。源码名取自历史轨迹，不向正式解题传入操作。
                for n in range(24):
                    if n == 8:
                        (tree / "build.log").write_text("build output")
                    if n == 16:
                        with (tree / "lib/matplotlib/artist.py").open("ab") as stream:
                            stream.write(b"\n# offline diff probe\n")
                    await exe.progress_feedback()
                facts["progress"] = exe.delivery.progress
                assert [r["state"] for r in exe.delivery.progress] == ["empty", "auxiliary", "source"], facts
                paid = base.parent / "slots" / case / "C/result.json"
                if paid.exists():
                    report = read(base.parent / "r/.evaluations" / read(paid)["id"] / "report.json")
                    exe.delivery.fact = report["trials"][0]["entry"]
                    facts["closeout"] = await exe.progress_feedback(4)
                    assert "剩余 4" in facts["closeout"] and await exe.progress_feedback(3) is None
            finally:
                await env.close_session(tree)
        save(folder / "receipt.json", facts)
        print(case, "passed", flush=True)
    save(base / "passed.json", {"model_calls": 0, "cases": [DJANGO, "matplotlib__matplotlib-24627"]})


if __name__ == "__main__":
    asyncio.run(run())
