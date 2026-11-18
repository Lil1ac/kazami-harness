"""离线验收、单次付费试次及独立后台入口复验。三个阶段独立启动，共用一份证据目录。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from agent_harness.config import load_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.engine import evaluate
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.evaluation.recovery import connection_probe
from agent_harness.evaluation.repository import RepositoryEnvironment, RepositoryExecutor
from agent_harness.evaluation.reproduction import RepositoryDelivery, digest_bytes, patch_paths, replay


ROOT = Path(__file__).resolve().parents[2]  # 仓库根目录；临时材料统一保存在 D 盘项目下。
RECORDS = ROOT / "evals/swe-three-arm/records"  # 已冻结的上游题目记录，仅宿主使用。
DATA = ROOT / ".runtime/entry13512"  # 此轮唯一试次、预检和离线证据。
PYTHON = ROOT / ".runtime/swe-env/Scripts/python.exe"  # 安装官方评分器的解释器。
DJANGO = "django__django-13512"  # 唯一允许发起模型请求的题号。
HISTORY = ROOT / ".runtime/ev3/paid/runs/.evaluations/5d289804-b21d-4a0a-9904-d34910030ca0"  # 上轮真实失败补丁。
PROBE = Path(__file__).with_name("admin_entry.py")  # 隔离于 Agent 的宿主验收探针。


def read(path):
    """读取 path 的 UTF-8 JSON，返回其原始结构。"""
    return json.loads(path.read_bytes())


def save(path, value):
    """将 value 以可读的 UTF-8 JSON 保存至 path，创建所需父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def experiment():
    """返回单组单题一次的硬预算协议，执行、摘要和收尾请求合计至多六十次。"""
    return Experiment(split="test", groups=preset_groups(["C"]), max_model_calls=60, max_cost_cents=100,
                      max_known_tokens=10**9, grader_timeout_seconds=600)


def settings():
    """加载原冻结实验的同模型同工具配置；验证硬限制后返回配置。"""
    cfg = load_config(ROOT / "evals/swe-three-arm/deepseek.toml")
    assert cfg.limits.max_model_turns == 60 and cfg.limits.max_http_attempts_per_turn == 1
    return cfg


def suite(data: Path = DATA):
    """把单题清单和私有评分记录写入本试次的 data，返回只向模型提供原始 issue 的清单路径。"""
    record = read(RECORDS / (DJANGO + ".json"))
    save(data / "case.json", record)
    path = data / "suite.json"
    save(path, {"name": "Django 后台入口验收", "version": "1", "provenance": "SWE-bench Verified",
                "cases": [{"id": DJANGO, "category": "fix", "project": record["repo"], "split": "test",
                           "prompt": record["problem_statement"], "source": "case.json", "backend": "swebench"}]})
    return path


class Allow:
    """离线文件与命令互通验收使用的授权替身。"""

    def decision(self, name):
        """离线验收仅授权预设 name，返回对应权限。"""
        return "allow"


async def offline_case(case, data: Path = DATA):
    """在 case 固定镜像上验证工作树、清理和用户入口，将零模型运行证据写入 data。"""
    folder = data / "offline" / case
    env = RepositoryEnvironment(read(RECORDS / (case + ".json")), folder / "env", settings(), PYTHON)
    identity = await env.prepare()
    assert identity["image_id"] == read(HISTORY / "report.json")["environments"][case]["image_id"]
    tree = data / "w" / ("o-" + uuid4().hex[:8])
    await env.workspace(tree)
    await env.start_session(tree)
    name = env.sessions[tree.resolve()]
    facts = {"image": identity["image_id"], "model_calls": 0}
    try:
        await env.verify_workspace(tree)
        executor = RepositoryExecutor(env, tree, data / "runs", settings())
        # 通过文件工具自身的工作区边界执行，容器命令使用与正式试次相同的适配器。
        result = await executor.inner.execute("write_file", json.dumps({"path": "entry-check.txt", "content": "through-file"}))
        assert result["ok"], result
        executor.permissions = Allow()
        for cmd in (["bash", "-c", "test \"$(cat entry-check.txt)\" = through-file && echo keep >/tmp/session-marker && echo through-command >entry-check.txt"],
                    ["bash", "-c", "test \"$(cat /tmp/session-marker)\" = keep"]):
            result = await executor.execute("run_command", json.dumps({"argv": cmd}))
            assert result["ok"], result
        executor.permissions = None  # 文件夹具沿用工具内建的本地工作区边界。
        result = await executor.execute("read_file", json.dumps({"path": "entry-check.txt"}))
        assert result["data"]["content"].strip() == "through-command"
        (tree / "entry-check.txt").unlink()
        facts.update(import_ok=True, shared_files=True, persistent_tmp=True)
        if case == DJANGO:
            script = PROBE.read_bytes()
            old = (HISTORY / "a1913230-c157-4b01-9a2a-e446b17b2461.patch").read_text(encoding="utf-8")
            for label, patch in (("baseline", ""), ("partial", old), ("reference", env.record["patch"])):
                value = await replay(env, script, patch, folder / label, 120)
                facts[label] = value
                assert value["status"] == ("passed" if label == "reference" else "failed"), value
                for field, pair in value["observations"].items():
                    assert (pair["actual"] == pair["expected"]) == (label == "reference" or label == "partial" and field == "editable")
            await env.checked(["docker", "cp", str(PROBE), name + ":/tmp/harness-repro.py"])
            delivery = RepositoryDelivery(env, tree, folder / "delivery")
            assert (await delivery.register(120))["baseline"]["status"] == "failed"
            source = folder / "reference" / "candidate.patch"
            await env.checked(["docker", "cp", str(source), name + ":/tmp/fix.patch"])
            applied = await env.command(tree, ["-c", "git apply /tmp/fix.patch && touch unrelated.txt && echo x > legitimate-new.txt"], 60)
            assert applied["exit_code"] == 0, applied
            chosen = patch_paths(env.record["patch"]) + ["legitimate-new.txt"]
            fact = await delivery.candidate(chosen, 120)
            assert fact["status"] == "verified" and "unrelated.txt" in fact["omitted"], fact
            actual = await env.patch(tree, folder / "delivery.patch", delivery.selected)
            assert delivery.final(actual)["status"] == "verified" and "unrelated.txt" not in actual
            (tree / "legitimate-new.txt").write_text("second version", encoding="utf-8")
            changed = await env.patch(tree, folder / "changed.patch", delivery.selected)
            assert delivery.final(changed)["status"] == "candidate_changed"
            official = await env.grade(actual, folder / "official")
            assert official["status"] == "passed", official
            facts.update(scope_ok=True, stale_receipt_invalidated=True, official=official["status"])
        # 取消 docker exec 的调用方后，必须连同容器内 sleep 一起收回。
        task = asyncio.create_task(env.command(tree, ["-c", "sleep 40"], 60))
        await asyncio.sleep(1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        await env.close_session(tree)
    assert not (await env.checked(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"])).strip()
    facts["cleanup_confirmed"] = True
    save(folder / "acceptance.json", facts)
    print(case, "offline passed", flush=True)


async def main(mode, data: Path = DATA):
    """按 mode 执行离线、付费或只读复核；data 独立存放本次账本和一次性标记，默认用首轮目录。"""
    if mode == "offline":
        for case in (DJANGO, "astropy__astropy-14539", "matplotlib__matplotlib-24627"):
            await offline_case(case, data)
        result = await evaluate(suite(data), settings(), experiment(), data / "runs", data / "w", "",
                                swe_python=PYTHON, preflight_only=True)
        assert result["state"] == "preflight_passed", result.get("preflight_error")
        save(data / "offline.json", {"preflight": result["id"], "probe_sha256": digest_bytes(PROBE.read_bytes()), "model_calls": 0})
    elif mode == "live":
        lock = data / "single-trial.lock"  # 已占用的账本即使重复启动，也保留原始连接回执。
        if lock.exists():
            raise FileExistsError("本目录的真实试次已占用")
        offline = read(data / "offline.json")
        assert offline["probe_sha256"] == digest_bytes(PROBE.read_bytes())
        key = os.environ["DEEPSEEK_API_KEY"]
        cfg = settings()
        # GET 无生成费用，连接或认证失败时仍可修复环境；真正执行之前排他占用唯一试次。
        receipt = await connection_probe(cfg, key)
        with lock.open("x") as stream:
            stream.write(offline["preflight"])
        save(data / "connectivity.json", receipt)
        result = await evaluate(data / "suite.json", cfg, experiment(), data / "runs", data / "w", key,
                                swe_python=PYTHON, required_preflight=offline["preflight"])
        save(data / "live.json", {"report": result["id"], "state": result["state"], "reason": result["stop_reason"]})
        print(result["id"], result["state"], result["stop_reason"], flush=True)
    elif mode == "review":
        ref = read(data / "live.json")
        folder = data / "runs/.evaluations" / ref["report"]
        report = read(folder / "report.json")
        assert len(report["trials"]) == 1
        row = report["trials"][0]
        patch = (folder / (row["run_id"] + ".patch")).read_bytes()
        assert digest_bytes(patch) == row["patch_sha256"]
        env = RepositoryEnvironment(read(data / "case.json"), folder / "environments" / DJANGO, settings(), PYTHON)
        env.image = report["environments"][DJANGO]["image_id"]
        script = PROBE.read_bytes()
        if digest_bytes(script) != read(data / "offline.json")["probe_sha256"]:
            raise ValueError("独立入口用例与离线验收时冻结的版本不同")
        observed = await replay(env, script, patch.decode(), data / "review", 120)
        # 事后的宿主复核单独挂到试次上，保持原始模型轨迹、评分和费用账本可复查。
        atomic_json(folder / "entry-reviews" / (row["run_id"] + ".json"),
                    {"run_id": row["run_id"], "protocol_sha256": report["protocol_sha256"], **observed})
        print(json.dumps(observed, ensure_ascii=False), flush=True)
    else:
        await offline_case(mode, data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["offline", "live", "review", DJANGO, "astropy__astropy-14539", "matplotlib__matplotlib-24627"])
    parser.add_argument("--data-dir", type=Path, default=DATA, help="每个获批试次独占的账本与证据目录")
    args = parser.parse_args()
    asyncio.run(main(args.mode, args.data_dir.resolve()))
