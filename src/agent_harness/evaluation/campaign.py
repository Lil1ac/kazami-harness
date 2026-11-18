"""获批三组实验的阶段调度：先预检和校准，再冻结正式题单，全部共用四十美元账本。"""

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4
from filelock import FileLock

from agent_harness.config import load_config
from agent_harness.evaluation.campaign_budget import CampaignBudget, atomic_json, PHASES
from agent_harness.evaluation.engine import evaluate, fingerprint, read_report, save_report, trial_schedule
from agent_harness.evaluation.mini import native_components
from agent_harness.evaluation.models import Experiment, preset_groups


class Campaign:
    """持有整轮检查点的单写入者；意外中断不会自动重放已开始的付费试次。"""

    def __init__(self, root: Path, suite_root: Path, work_root: Path, swe_python: Path):
        """绑定 root 记录目录、suite_root 固定材料、work_root 隔离临时区及 swe_python 评分解释器。

        构造不启动模型或容器。全部参数必须是显式本地路径，工作区与记录区不得相互包含。
        """
        self.root, self.suite_root = root.resolve(), suite_root.resolve()  # 专属整轮记录和官方材料。
        self.work_root, self.swe_python = work_root.resolve(), swe_python.resolve()  # 可回收临时区和独立评分器。
        self.runs_root = self.root / "runs"  # 阶段报告与逐题原始证据共存。
        self.config = load_config(self.suite_root / "deepseek.toml")  # 审批通过的运行条件。
        self.selection = json.loads((self.suite_root / "selection.json").read_text(encoding="utf-8-sig"))  # 元数据抽样，不根据解题结果挑题。
        self.path = self.root / "campaign.json"  # 阶段检查点，不代替权威费用账本。
        self.state: dict = {}  # 初始化后载入已完成阶段、原始报告和未完成任务。
        self.budget: CampaignBudget | None = None  # 初始化后共享的唯一费用守卫。
        self.protocol: dict = {}  # 代码、模型、价格和选择方法的冻结联合指纹。

    def validate(self) -> None:
        """核对批准的资源与模型配置，变化时拒绝执行；不以费用预估替代硬门禁。"""
        expected = {"max_model_turns": 60, "max_tool_calls": 160, "run_timeout_seconds": 1200,
                    "command_timeout_seconds": 120, "model_turn_timeout_seconds": 120,
                    "max_http_attempts_per_turn": 1, "model_max_output_tokens": 8192}
        if any(getattr(self.config.limits, key) != value for key, value in expected.items()):
            raise ValueError("运行限额与批准协议不符")
        if (self.config.context.max_input_bytes != 262144 or self.config.context.inline_tool_bytes != 8192
                or self.config.context.max_summary_calls != 4
                or self.config.generation.model_dump() != {"thinking": "enabled", "reasoning_effort": "high", "top_p": .95}):
            raise ValueError("上下文或生成参数与批准协议不符")
        if self.config.requested_model != "deepseek-flash" or self.config.custom_providers[self.config.provider].base_url.rstrip("/") != "https://api.deepseek.com/v1":
            raise ValueError("模型或端点与批准协议不符")
        if self.work_root.is_relative_to(self.root) or self.root.is_relative_to(self.work_root):
            raise ValueError("工作区和权威记录必须互不包含")
        if not self.swe_python.is_file():
            raise ValueError("固定官方评分解释器缺失")

    def build_protocol(self) -> dict:
        """返回当前源码、配置和三类身份的审计协议；不改文件，不读取密钥或执行评测。"""
        native = native_components()
        source_root = Path(__file__).resolve().parents[1]
        protocol = {"config": self.config.model_dump(mode="json"), "selection": self.selection,
            "source": json.loads((self.suite_root / "source.json").read_text(encoding="utf-8")),
            "records": {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted((self.suite_root / "records").glob("*.json"))},
            "runtime": {path.relative_to(source_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(source_root.rglob("*.py"))},
            "mini": {path.relative_to(native.package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(native.package.rglob("*")) if path.suffix in {".py", ".yaml"}},
            "phase_limits": PHASES, "groups": [group.model_dump() for group in preset_groups(["A", "B", "C"])],
            "seed": 20261115, "total_trials": 204, "max_usd": 40}
        from agent_harness.evaluation.protocols import identities
        protocol["identities"] = identities(protocol)
        return protocol

    def initialize(self) -> None:
        """持锁核对已应用协议并载入账本；协议修订统一交 recovery 的先审查后应用流程。

        活动操作或未知费用均拒绝，不能绕过恢复审查直接启动另一执行器。
        """
        self.validate()
        self.protocol = self.build_protocol()
        digest = fingerprint(self.protocol)
        self.root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state["protocol_sha256"] != digest:
                raise ValueError("代码或配置已经变化；须核对整轮协议，不能重新开始清零费用")
            if self.state.get("active"):
                raise ValueError("上一阶段有未完成操作；保留现场和费用，禁止自动重放")
            if self.state.get("stop_reason"):
                raise ValueError("整轮尚未完成恢复审查：" + self.state["stop_reason"])
        else:
            images = self.docker(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
            self.state = {"id": str(uuid4()), "protocol_sha256": digest, "created_at": datetime.now(UTC).isoformat(),
                "original_image_tags": images.splitlines(), "phases": {}, "preflight": {}, "replacements": [],
                "active": None, "cleanup": [], "stop_reason": None}
            atomic_json(self.root / "protocol.json", self.protocol)
            self.save()
        self.budget = CampaignBudget(self.root / "ledger.json", digest)
        if self.budget.stop_reason:
            raise ValueError("费用账本尚有停止原因：" + self.budget.stop_reason)

    def check_pause(self) -> None:
        """在新操作前读取持久暂停标记；已有模型响应由费用守卫先结算，不杀进程。"""
        if (self.root / "pause.json").exists():
            self.state["stop_reason"] = "campaign_paused"
            self.save()
            raise ValueError("campaign_paused")

    def save(self) -> None:
        """持久保存当前阶段检查点；无参数，保存失败禁止继续调度。"""
        atomic_json(self.path, self.state)

    def docker(self, args: list[str]) -> str:
        """执行只由宿主构造的 Docker args，返回标准输出；非零或超时抛环境错误。"""
        result = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if result.returncode:
            raise ValueError("Docker 宿主操作失败：" + result.stderr[-1000:])
        return result.stdout.strip()

    def experiment(self, phase: str, repeats: int = 1) -> Experiment:
        """返回 phase 的冻结组与全局请求上限；repeats 仅用于预先规定的稳定性重复。"""
        return Experiment(groups=preset_groups(["A", "B", "C"]), split="dev" if phase == "calibration" else "test",
            repeats=repeats, seed=20261115, max_model_calls=12240, max_cost_cents=4000,
            max_known_tokens=10**12, grader_timeout_seconds=600)

    def record_bytes(self, identifier: str) -> bytes:
        """读取 identifier 的已冻结官方记录字节；当前文件哈希改变或未知题号时拒绝，不返回部分内容。"""
        expected = self.protocol["records"].get(identifier)
        if expected is None:
            raise ValueError("题号不在冻结的官方快照内")
        raw = (self.suite_root / "records" / (identifier + ".json")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("官方任务记录在冻结后发生变化")
        return raw

    def case(self, identifier: str, phase: str) -> dict:
        """从固定官方记录构造 identifier 的任务清单；只有原始需求进入Agent输入。"""
        record = json.loads(self.record_bytes(identifier))
        return {"id": identifier, "project": record["repo"], "category": record["difficulty"],
            "split": "dev" if phase == "calibration" else "test", "prompt": record["problem_statement"],
            "backend": "swebench", "source": "record.json"}

    def suite(self, identifier: str, phase: str) -> Path:
        """物化单题 suite 与私有官方评分记录并返回路径；不从前次工作区复制产物。"""
        folder = self.root / "suites" / phase / identifier
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "record.json").write_bytes(self.record_bytes(identifier))
        value = {"name": "三组对照 · " + phase, "version": "20261115", "provenance": self.protocol["source"]["url"],
                 "kind": "context", "cases": [self.case(identifier, phase)]}
        atomic_json(folder / "suite.json", value)
        return folder / "suite.json"

    def remove_workspace(self, target: Path) -> None:
        """删除本轮 work_root 直接子目录 target；先核对绝对归属，只解除本轮只读Git文件属性。"""
        if target.resolve() != target or target.parent != self.work_root or target.is_symlink() or target.is_junction():
            raise ValueError("工作副本回收路径越界")

        def writable_retry(function, name, error) -> None:
            """仅对 name 所指的本轮普通路径解除只读并重试 function；其余错误原样传播。"""
            path = Path(name)
            if (not isinstance(error, PermissionError) or path.resolve() != path
                    or not path.is_relative_to(target) or path.is_symlink() or path.is_junction()):
                raise error
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
            function(name)

        shutil.rmtree(target, onexc=writable_retry)

    def cleanup(self, report: dict) -> None:
        """只回收 report 登记的本轮工作副本、可重建归档和新拉取镜像；保留补丁与评分原件。

        Windows 删除前逐项核对绝对路径归属；镜像必须不在启动前清单且无容器引用。
        """
        for trial in report["trials"]:
            target = self.work_root / trial["run_id"]
            if target.resolve() != target or target.parent != self.work_root or target.is_symlink() or target.is_junction():
                raise ValueError("工作区回收目标越界")
            if target.is_dir() and not trial.get("interrupted"):
                self.remove_workspace(target)
        for preflight in report.get("preflight", []):
            if not preflight.get("workspace_probe"):
                continue
            target = Path(preflight["workspace_probe"])
            if target.resolve() != target or target.parent != self.work_root or not target.name.startswith("check-") or target.is_symlink() or target.is_junction():
                raise ValueError("预检副本回收目标越界")
            if target.is_dir():
                self.remove_workspace(target)
        directory = self.runs_root / ".evaluations" / report["id"]
        for identifier, environment in report.get("environments", {}).items():
            for name in ("source.tar", "baseline.git.tar"):
                target = directory / "environments" / identifier / name
                if target.resolve() != target or not target.is_relative_to(directory.resolve()) or target.is_symlink():
                    raise ValueError("归档回收目标越界")
                target.unlink(missing_ok=True)
            tag = environment["image"]
            if tag in self.state["original_image_tags"]:
                continue
            actual = self.docker(["image", "inspect", "--format", "{{.Id}}", tag])
            if actual != environment["image_id"] or self.docker(["ps", "-aq", "--filter", "ancestor=" + actual]):
                self.state["cleanup"].append({"image": tag, "status": "retained_in_use_or_changed"})
                continue
            self.docker(["image", "rm", tag])
            self.state["cleanup"].append({"image": tag, "image_id": actual, "status": "removed_owned_tag"})
        self.save()

    def _preflight_passed(self, identifier: str, receipt: dict) -> bool:
        """解释identifier的预检回执receipt，返回是否通过；False只表示题目门槛不满足。

        receipt包含passed布尔值、reason停止原因和原报告编号。下载、评分进程及未知失败
        均抛ValueError，防止把基础设施异常用于候补选择；本方法不修改已保存的报告或缓存。
        """
        if receipt["passed"]:
            from agent_harness.evaluation.preflight import verify_certificate
            folder = self.runs_root / ".evaluations" / receipt["report_id"]
            certificate = json.loads((folder / "preflight-certificate.json").read_text(encoding="utf-8"))
            if fingerprint(certificate) != receipt.get("certificate_sha256"):
                raise ValueError("预检证书未登记或被修改")
            verify_certificate(self.runs_root, certificate,
                [{"id": identifier, "record": json.loads(self.record_bytes(identifier))}],
                self.config.model_dump(mode="json"))
            return True
        if receipt.get("reason") != "invalid_case":
            raise ValueError("预检基础设施异常，停止候补选择并保留原题：" + identifier)
        return False

    async def preflight(self, identifier: str, phase: str) -> bool:
        """验证identifier的原始缺陷和参考补丁；phase指定calibration或formal阶段。

        返回True表示通过，False仅表示官方题目门槛不满足；基础设施失败保存原件后抛ValueError，
        恢复时同样拒绝把这些失败缓存当作换题依据。本方法会创建容器并保存预检检查点。
        """
        self.check_pause()
        key = phase + ":" + identifier
        if key in self.state["preflight"]:
            return self._preflight_passed(identifier, self.state["preflight"][key])
        if shutil.disk_usage(self.work_root).free < 6 * 1024**3:
            raise ValueError("D盘剩余空间不足6GiB，停止环境准备")
        self.state["active"] = {"kind": "preflight", "phase": phase, "case_id": identifier}
        self.save()
        print(json.dumps({"event": "preflight_started", "phase": phase, "case_id": identifier}), flush=True)
        report = await evaluate(self.suite(identifier, phase), self.config, self.experiment(phase), self.runs_root,
            self.work_root, "", preflight_only=True, swe_python=self.swe_python)
        report["internal"] = True
        save_report(self.runs_root / ".evaluations" / report["id"], report)
        passed = report["state"] == "preflight_passed"
        self.state["preflight"][key] = {"report_id": report["id"], "passed": passed, "reason": report.get("stop_reason")}
        if passed:
            from agent_harness.evaluation.preflight import make_certificate
            folder = self.runs_root / ".evaluations" / report["id"]
            certificate = make_certificate(folder)
            atomic_json(folder / "preflight-certificate.json", certificate)
            self.state["preflight"][key].update(certificate_sha256=fingerprint(certificate), status="fresh")
        self.state["active"] = None
        self.save()
        self.cleanup(report)
        print(json.dumps({"event": "preflight_finished", "case_id": identifier, "passed": passed,
                          "report_id": report["id"]}), flush=True)
        return self._preflight_passed(identifier, self.state["preflight"][key])

    async def freeze_formal(self) -> list[str]:
        """按冻结顺序预检五十题；仅按同仓库同难度备用序列替换，付费结果不参与选择。"""
        if self.state.get("formal_ids"):
            return self.state["formal_ids"]
        selected, used = [], set()
        for identifier, repo, difficulty in self.selection["selected"]:
            alternatives = [identifier] + [row[0] for row in self.selection["reserves"] if row[1:] == [repo, difficulty]]
            accepted = None
            for candidate in alternatives:
                if candidate in used:
                    continue
                if await self.preflight(candidate, "formal"):
                    accepted = candidate
                    break
            if accepted is None:
                raise ValueError("同分层备用题耗尽，需要确认调整题单：" + identifier)
            selected.append(accepted)
            used.add(accepted)
            if accepted != identifier:
                self.state["replacements"].append({"original": identifier, "replacement": accepted, "repo": repo, "difficulty": difficulty})
                self.save()
        self.state["formal_ids"] = selected
        replacements = {row["original"]: row["replacement"] for row in self.state["replacements"]}
        self.state["stability_ids"] = [replacements.get(row[0], row[0]) for row in self.selection["sentinels"]]
        atomic_json(self.root / "formal-manifest.json", {"ids": selected, "stability_ids": self.state["stability_ids"],
            "preflight": self.state["preflight"], "replacements": self.state["replacements"], "protocol_sha256": self.state["protocol_sha256"]})
        self.state["formal_manifest_sha256"] = hashlib.sha256((self.root / "formal-manifest.json").read_bytes()).hexdigest()
        self.save()
        return selected

    def aggregate(self, phase: str, ids: list[str], experiment: Experiment) -> dict:
        """创建或读取 phase 的阶段报告；稳定性报告包含预先指定六题在正式阶段的首次结果。"""
        entry = self.state["phases"].get(phase)
        if entry:
            return read_report(self.runs_root, entry["report_id"])
        identifier = str(uuid4())
        cases = [self.case(item, phase) for item in ids]
        schedule = trial_schedule(ids, experiment, True)
        report = {"id": identifier, "name": "SWE-bench Verified · " + phase, "created_at": datetime.now(UTC).isoformat(),
            "suite_version": "20261115", "mode": "real_model", "state": "incomplete", "stop_reason": None,
            "model": self.config.requested_model, "provenance": "固定分层50题子集；稳定性仅六题，非全量500题成绩。",
            "protocol_sha256": self.state["protocol_sha256"], "experiment": experiment.model_dump(),
            "cases": cases, "schedule": schedule, "trials": [], "preflight": [], "environments": {},
            "active_trial": None, "campaign_phase": phase, "budget": self.budget.state}
        folder = self.runs_root / ".evaluations" / identifier
        folder.mkdir(parents=True)
        atomic_json(folder / "protocol.json", self.protocol)
        self.state["phases"][phase] = {"report_id": identifier, "completed_cases": []}
        if phase == "stability":
            formal = read_report(self.runs_root, self.state["phases"]["formal"]["report_id"])
            first = [deepcopy(row) for row in formal["trials"] if row["case_id"] in ids]
            for row in schedule:
                if row["repeat"] == 0:
                    row["run_id"] = next(item["run_id"] for item in first if item["case_id"] == row["case_id"] and item["strategy"] == row["strategy"])
            self.merge(report, {**formal, "trials": first})
        save_report(folder, report)
        self.save()
        return report

    def merge(self, target: dict, source: dict) -> None:
        """合入 source 试次和原件到 target 阶段报告；同一 Run ID 禁止重复，保留来源报告编号。"""
        destination = self.runs_root / ".evaluations" / target["id"]
        source_dir = self.runs_root / ".evaluations" / source["id"]
        existing = {row["run_id"] for row in target["trials"]}
        for trial in source["trials"]:
            if trial["run_id"] in existing:
                raise ValueError("阶段报告含重复试次")
            target["trials"].append({**trial, "source_report_id": source["id"]})
            for suffix in (".patch", ".trajectory.json"):
                artifact = source_dir / (trial["run_id"] + suffix)
                if artifact.is_file():
                    shutil.copyfile(artifact, destination / artifact.name)
            grade = source_dir / "grades" / trial["run_id"]
            if grade.is_dir():
                shutil.copytree(grade, destination / "grades" / trial["run_id"])
        case_ids = {case["id"] for case in target["cases"]}
        target["environments"].update({key: value for key, value in source.get("environments", {}).items() if key in case_ids})
        present = {row["case_id"] for row in target["preflight"]}
        target["preflight"].extend(row for row in source.get("preflight", []) if row["case_id"] in case_ids and row["case_id"] not in present)

    async def run_phase(self, phase: str, ids: list[str], api_key: str) -> dict:
        """串行执行 phase 每题三组；稳定性各加两次，全部失败开销保留，阶段异常立即停止。"""
        experiment = self.experiment(phase, 3 if phase == "stability" else 1)
        report = self.aggregate(phase, ids, experiment)
        entry = self.state["phases"][phase]
        for identifier in ids:
            self.check_pause()
            if identifier in entry["completed_cases"]:
                continue
            preflight_phase = "formal" if phase == "stability" else phase
            if not self.state["preflight"][preflight_phase + ":" + identifier]["passed"]:
                raise ValueError("题目尚未通过零模型预检")
            all_slots = [row for row in report["schedule"] if row["case_id"] == identifier and (phase != "stability" or row["repeat"] > 0)]
            valid_ids = {row["run_id"] for row in report["trials"] if row.get("comparison_valid")}
            completed_slots = [row for row in all_slots if row["run_id"] in valid_ids]
            slots = [row for row in all_slots if row["run_id"] not in valid_ids]
            if not slots:
                entry["completed_cases"].append(identifier)
                self.save()
                continue
            local_experiment = self.experiment(phase, 2 if phase == "stability" else 1)
            if phase == "stability":
                local_experiment.repeat_offset = 1
            self.state["active"] = {"kind": "paid", "phase": phase, "case_id": identifier, "slots": slots}
            self.save()
            print(json.dumps({"event": "case_started", "phase": phase, "case_id": identifier,
                              "known_usd": self.budget.state["known_usd_picos"] / 1e12}), flush=True)
            def progress(current: dict) -> None:
                """实时投影当前题的预算、已结束试次和活跃槽位；只更新父报告，不重复合入原件。"""
                snapshot = {**report, "budget": self.budget.state, "trials": report["trials"] + current["trials"],
                            "active_trial": current["active_trial"]}
                save_report(self.runs_root / ".evaluations" / report["id"], snapshot)

            child = await evaluate(self.suite(identifier, preflight_phase), self.config, local_experiment,
                self.runs_root, self.work_root, api_key, swe_python=self.swe_python,
                required_preflight=self.state["preflight"][preflight_phase + ":" + identifier]["report_id"],
                campaign_budget=self.budget, campaign_phase=phase, frozen_schedule=slots, progress=progress,
                completed_schedule=completed_slots,
                preflight_certificate_sha256=self.state["preflight"][preflight_phase + ":" + identifier]["certificate_sha256"])
            child["internal"] = True
            save_report(self.runs_root / ".evaluations" / child["id"], child)
            self.merge(report, child)
            report["budget"] = self.budget.state
            report["stop_reason"] = child["stop_reason"]
            save_report(self.runs_root / ".evaluations" / report["id"], report)
            self.state["active"] = None
            if child["state"] != "completed":
                self.state["stop_reason"] = child["stop_reason"]
                self.save()
                raise ValueError("阶段停止：" + str(child["stop_reason"]))
            entry["completed_cases"].append(identifier)
            self.save()
            self.cleanup(child)
        report["state"] = "completed"
        report["stop_reason"] = None
        save_report(self.runs_root / ".evaluations" / report["id"], report)
        return report

    def calibration_passed(self, report: dict) -> bool:
        """核对开发校准的工程有效性；不以通过率高低挑选可发布结果或改变模型。"""
        return (report["state"] == "completed" and len(report["trials"]) == 18
            and all(row.get("comparison_valid") and row["usage_complete"] and row.get("cost", {}).get("complete")
                    and row.get("response_model", "").lower() in {"deepseek-flash", "deepseek-v4.1-flash-1017"}
                    and row.get("stop_reason") != "mini_adapter_error" for row in report["trials"])
            and all(row.get("summary_calls") == 0 for row in report["trials"] if row["strategy"] in {"A", "B"})
            and all((row.get("last_admitted_input_bytes") or 0) <= 262144 for row in report["trials"]))

    async def run(self, api_key: str, *, preflight_only: bool = False) -> None:
        """依次执行批准阶段；preflight_only 仅开发题零模型检查，api_key 从环境注入不落盘。"""
        development = json.loads((self.suite_root / "calibration.json").read_text(encoding="utf-8"))
        ids = [case["id"] for case in development["cases"]]
        # 完成的校准不再进入环境准备；恢复入口已核对原始结果和请求账本。
        if not self.state.get("calibration_passed"):
            for identifier in ids:
                if not await self.preflight(identifier, "calibration"):
                    raise ValueError("开发题预检失败，校准尚未开始：" + identifier)
        if preflight_only:
            return
        if not api_key:
            raise ValueError("尚未提供官方 DeepSeek 凭据")
        calibration = await self.run_phase("calibration", ids, api_key)
        if not self.calibration_passed(calibration):
            raise ValueError("开发校准未通过工程有效性门槛，停止正式阶段")
        self.state["calibration_passed"] = True
        self.save()
        formal_ids = await self.freeze_formal()
        for identifier in formal_ids:
            if not await self.preflight(identifier, "formal"):
                raise ValueError("正式题环境门槛不满足：" + identifier)
        await self.run_phase("formal", formal_ids, api_key)
        await self.run_phase("stability", self.state["stability_ids"], api_key)
        self.state["completed"] = True
        self.save()
        print(json.dumps({"event": "campaign_completed", "calls": self.budget.state["calls_admitted"],
                          "peak_usd": self.budget.state["known_usd_picos"] / 1e12}), flush=True)


def main() -> int:
    """解析显式本地路径并持锁执行；返回0仅表示要求的阶段完成，不隐藏失败。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--suite-root", type=Path, default=Path("evals/swe-three-arm"))
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--swe-python", type=Path, default=Path(".runtime/swe-env/Scripts/python.exe"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--offline-proof", type=Path, required=True, help="当前代码的离线验收回执，不含模型成绩")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    campaign = Campaign(args.root, args.suite_root, args.work_root, args.swe_python)
    with FileLock(str(args.root / "campaign.lock"), timeout=0):
        campaign.initialize()
        proof = json.loads(args.offline_proof.read_text(encoding="utf-8"))
        if (proof.get("runtime_sha256") != fingerprint(campaign.protocol["runtime"])
                or proof.get("external_model_requests") != 0
                or not all(proof.get(key) for key in ("backend_passed", "frontend_passed", "docker_passed", "web_passed", "electron_passed"))):
            raise ValueError("离线验收回执与当前代码不符，不开始付费校准")
        try:
            asyncio.run(campaign.run(os.environ.get("DEEPSEEK_API_KEY", ""), preflight_only=args.preflight_only))
        except (ValueError, OSError) as error:
            campaign.state["stop_reason"] = str(error)
            campaign.save()
            print(json.dumps({"event": "campaign_stopped", "reason": str(error)}, ensure_ascii=False), flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
