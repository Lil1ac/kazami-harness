"""冻结 Agent 自选入口用例，在干净镜像复跑基线及实际交付补丁。"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


GUIDE = (
    "仓库复现与交付：先从原始 issue 确定用户操作和可见结果，在 /tmp/harness-repro.py 写一个独立 Python 用例，"
    "复现必须调用 issue 指出的入口：页面问题用应用测试客户端请求实际路由，命令行问题用实际命令；"
    "记录入口、操作以及返回结果中用户看到的内容。只调用组件或序列化函数限于定位，不能证明页面行为。"
    "检查同一操作中使用不同渲染或处理路径的状态。完成后在 stdout 单独一行打印 HARNESS_OBSERVATIONS 加空格再加 JSON。"
    'JSON 形如 {"用户操作":{"actual":"实际结果","expected":"预期结果"}}；值须取自真实操作，'
    "匹配预期时退出 0，遇到所述缺陷时退出 1。依赖、导入、收集或测试设置故障须保留 traceback。"
    "用 run_command 调用 argv=[\"harness-reproduce\"] 在干净基线上冻结并复现，可修订脚本后重新登记。"
    "修改源码后用 argv=[\"harness-deliver\",\"改动路径\",...] 明确选择交付范围，在干净环境重跑同一脚本。"
    "工具会返回实际补丁摘要、未选文件和入口结果。失败时可继续改动；无法完成时如实报告。"
)  # 所有仓库共享，仅说明协议，不含题目答案。


def digest_bytes(data: bytes) -> str:
    """返回 data 原始字节的 SHA-256，用于绑定用例及候选版本。"""
    return hashlib.sha256(data).hexdigest()


def patch_paths(patch: str) -> list[str]:
    """解析 patch 的真实路径，支持空格或非 ASCII 名称，空补丁返回空列表。"""
    if not patch:
        return []
    output = subprocess.run(["git", "apply", "--numstat", "-z"], input=patch.encode(), capture_output=True, check=True)
    return [line.split(b"\t", 2)[2].decode("utf-8") for line in output.stdout.split(b"\0") if line]


def entry_requirement(issue: str, repo: str) -> tuple[str, str]:
    """按原始 issue 的操作描述和 repo 选择可观测边界；不明确时返回自选用例。"""
    title = issue.partition("\n")[0].lower()
    if repo == "django/django":
        if re.search(r"\badmin\b", title) and re.search(r"display|render|page|form|view|widget", title):
            return "admin", ""
    if re.search(r"\b(?:web page|http request|change page|change list page)\b", title):
        return "http", ""
    command = re.search(r"(?:^|\n|[\x60$])\s*(?:python(?:3)?\s+(?:-m\s+)?)?(manage\.py|django-admin|sphinx-build|pytest|pylint)\s+(?:--?[\w-]+|[\w.-]+)", issue)
    if command and re.search(r"\b(?:cli|command|invocation|command.line)\b", title):
        return "cli", command.group(1)
    symbols = set(re.findall(r"(?<![\w.])([A-Za-z]\w*(?:\.[A-Za-z]\w*)+)\s*\(", issue.partition("\n")[0]))
    if len(symbols) == 1:
        symbol = symbols.pop()
        owner = symbol.split(".")[0]
        if owner[0].isupper() or owner == repo.split("/")[-1].replace("-", "_"):
            return "api", symbol
    return "self", ""


def matching_entries(output: str, kind: str, target: str) -> list[dict]:
    """从 output 读取单条解释器回执，按 kind 和 target 返回真正命中的路由或命令。"""
    traces = [line[len("HARNESS_ENTRY_TRACE "):] for line in output.splitlines()
              if line.startswith("HARNESS_ENTRY_TRACE ")]
    if len(traces) != 1:
        return []
    try:
        rows = json.loads(traces[0])
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    rows = [row for row in rows if isinstance(row, dict)]
    if kind == "cli":
        return [row for row in rows if target in (row.get("command") or [])]
    if kind == "api":
        return [row for row in rows if row.get("symbol") == target]
    return [row for row in rows if row.get("method") and row.get("path") and isinstance(row.get("status"), int)
            and (kind == "http" or row.get("admin") is True)]


def coverage_review(kind: str, candidate: dict) -> str | None:
    """对已经通过的页面断言检查覆盖宽度；仅检验一个页面结果时返回一次收尾建议。"""
    observations = candidate.get("observations", {})
    if kind not in {"http", "admin"} or candidate["status"] != "passed" or len(observations) != 1:
        return None
    only = next(iter(observations))[:180]
    detail = ("可编辑控件与只读展示可能走不同代码。请从页面模板或组件调用处确认两条路径，"
              "用同一业务值核对各自实际显示的文本。" if kind == "admin" else
              "请从同一路由的页面代码确认不同状态是否走不同处理路径，核对相关响应。")
    return (f"当前通过的页面断言只有一项：{only}。{detail}"
            "有另一条相关路径就扩展用例并重跑；只有一种适用状态时，交付中说明源码依据。")


def observation(result: dict, output: str) -> dict:
    """从真实进程 result 与完整 output 提取业务观察；缺失、超时或退出码不一致标为执行错误。"""
    code = result.get("exit_code")
    fact = {"status": "error", "exit_code": code, "output": output[-4000:]}
    rows = [line.removeprefix("HARNESS_OBSERVATIONS ") for line in output.splitlines()
            if line.startswith("HARNESS_OBSERVATIONS ")]
    if len(rows) != 1 or code not in (0, 1) or result.get("timed_out") or result.get("output_limit_exceeded"):
        return fact
    try:
        values = json.loads(rows[0])
    except ValueError:
        return fact
    if not isinstance(values, dict) or not values or any(
        not name or not isinstance(pair, dict) or set(pair) != {"actual", "expected"} for name, pair in values.items()
    ):
        return fact
    passed = all(pair["actual"] == pair["expected"] for pair in values.values())
    if code == int(not passed):
        fact.update(status="passed" if passed else "failed", observations=values)
    return fact


async def replay(environment, script: bytes, patch: str, folder: Path, seconds: float,
                 requirement: tuple[str, str] = ("self", "")) -> dict:
    """把 script 与 patch 冻结到私有 folder，在同镜像干净基线上用 seconds 秒复跑，返回真实观察与摘要。"""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "repro.py").write_bytes(script)
    (folder / "candidate.patch").write_bytes(patch.encode())
    shell = """set -e
exec 2>&1
source /opt/miniconda3/bin/activate testbed
export PYTHONIOENCODING=utf-8
cd /testbed
git reset --hard "$1" >/dev/null
rm -rf -- /testbed/.git
mkdir /testbed/.git
tar -xf /harness-baseline.tar -C /testbed/.git
if [ -s /probe/candidate.patch ]; then git apply /probe/candidate.patch; fi
export PYTHONPATH="/testbed:/testbed/src${PYTHONPATH:+:$PYTHONPATH}"
exec python /probe/repro.py"""
    # 同一只读观察器在基线、候选上使用相同参数；业务断言仍由用例执行。
    kind, target = requirement
    mounts = []
    if kind != "self":
        observer = Path(__file__).with_name("entry_observer.py")
        mounts.extend(["--mount", f"type=bind,source={observer.resolve()},target=/probe/observe.py,readonly"])
        shell = shell.replace("exec python /probe/repro.py", 'exec python /probe/observe.py /probe/repro.py "$2" "$3"')
    for source, target in [(folder / "repro.py", "/probe/repro.py"), (folder / "candidate.patch", "/probe/candidate.patch"),
                           (environment.directory / "baseline.git.tar", "/harness-baseline.tar")]:
        mounts.extend(["--mount", f"type=bind,source={source.resolve()},target={target},readonly"])
    result = await environment.container(["-c", shell, "repro", environment.record["base_commit"], kind, target], mounts, seconds)
    output = environment.full_stdout(result) + result.get("stderr", "")
    (folder / "output.txt").write_text(output, encoding="utf-8")
    fact = {**observation(result, output), "script_sha256": digest_bytes(script),
            "patch_sha256": digest_bytes(patch.encode()), "command": "python /probe/repro.py"}
    if kind != "self":
        fact["observed_entries"] = matching_entries(output, kind, target)
    (folder / "result.json").write_text(json.dumps(fact, ensure_ascii=False, indent=2), encoding="utf-8")
    return fact


class RepositoryDelivery:
    """持有一次试次的冻结脚本、选择的补丁及最近一次入口证明。"""

    def __init__(self, environment, workspace: Path, folder: Path, issue: str = ""):
        """绑定独占 workspace 和宿主证据 folder；不启动容器，也不接触评分答案。"""
        self.environment = environment  # 运行与独立复验共用不可变镜像。
        self.workspace = workspace  # 当前唯一工作树。
        self.folder = folder  # 真实报告旁的私有证据目录。
        self.script = b""  # 最近成功读取的用例字节。
        self.selected: tuple[str, ...] = ()  # 空表示尚未确认交付清单。
        self.fact: dict = {}  # 前端及收尾共享的最近入口回执。
        self.attempt = 0  # 防止后续复验覆盖旧的失败输出。
        self.requirement = entry_requirement(issue, environment.record["repo"]) if issue else ("self", "")  # 入口类型与目标名。
        self.progress: list[dict] = []  # 来源于当前工作树的阶段检查，保留在送评报告中。

    async def register(self, seconds: float) -> dict:
        """读取当前容器中限长的临时脚本，在干净基线上复现；seconds 为本次复验上限。"""
        import base64
        self.attempt += 1
        self.fact, self.selected, self.script = {}, (), b""
        source = await self.environment.command(self.workspace, ["-c",
            'test -f /tmp/harness-repro.py && test "$(wc -c </tmp/harness-repro.py)" -le 65536 && base64 -w0 /tmp/harness-repro.py'], seconds)
        if source.get("exit_code") != 0:
            raise ValueError("请先在 /tmp/harness-repro.py 写入完整用例（最多 64 KiB）")
        self.script = base64.b64decode(self.environment.full_stdout(source), validate=True)
        baseline = await replay(self.environment, self.script, "", self.folder / f"{self.attempt}-baseline", seconds,
                                self.requirement)
        self.fact = {"baseline": baseline, "status": "unverified", "entry_kind": self.requirement[0]}
        if self.requirement[0] != "self" and not baseline["observed_entries"]:
            self.fact["next_action"] = self.entry_guidance()
        return self.fact

    def entry_guidance(self) -> str:
        """依据原 issue 需要的边界返回可执行的下一步，保留组件测试作定位证据。"""
        if self.requirement[0] == "cli":
            return f"请从子进程执行 {self.requirement[1]}，从退出码和真实输出取实际值，重新登记用例。"
        if self.requirement[0] == "api":
            return f"问题标题指向 {self.requirement[1]}。请经该公开调用重现，并记录实际返回值或异常。"
        if self.requirement[0] == "http":
            return "请通过应用测试客户端请求实际路由，从响应中取得用户可见结果，再登记用例。"
        return ("请用测试客户端请求用户操作对应的实际页面，检查路由、状态和响应中的字段。"
                "核对可编辑和只读等会走不同渲染路径的状态，然后重新登记用例。")

    async def candidate(self, names: list[str], seconds: float) -> dict:
        """仅收集 names 明确选择的改动，在同一冻结脚本中复验；报告所有未选改动和实际补丁摘要。"""
        if not self.script or self.fact.get("baseline", {}).get("status") != "failed":
            raise ValueError("缺少到达目标行为并观察到缺陷的基线复现，请先运行 harness-reproduce")
        if self.fact.get("next_action"):
            raise ValueError(self.fact["next_action"])
        if not names or any(not n or PurePosixPath(n).is_absolute() or ".." in PurePosixPath(n).parts for n in names):
            raise ValueError("提交时须列出相对工作区的具体文件路径")
        self.attempt += 1
        self.folder.mkdir(parents=True, exist_ok=True)
        full = await self.environment.patch(self.workspace, self.folder / "worktree.patch")
        paths = patch_paths(full)
        if not set(names).issubset(paths):
            raise ValueError("所选文件不全在当前差异中；实际改动：" + json.dumps(paths, ensure_ascii=False))
        self.selected = tuple(dict.fromkeys(names))
        patch = await self.environment.patch(self.workspace, self.folder / "selected.patch", self.selected)
        self.fact.update(status="unverified", selected=list(self.selected), omitted=sorted(set(paths) - set(names)),
                         patch_sha256=digest_bytes(patch.encode()), patch_bytes=len(patch.encode()), preview=patch[:2400])
        candidate = await replay(self.environment, self.script, patch, self.folder / f"{self.attempt}-candidate", seconds,
                                 self.requirement)
        base = self.fact["baseline"].get("observations", {})
        current = candidate.get("observations", {})
        same = base.keys() == current.keys() and all(base[k]["expected"] == current[k]["expected"] for k in base)
        if self.requirement[0] != "self":
            # 同一脚本可能随补丁切换分支；至少应再次经过原来观察到的路由或可执行文件。
            def identity(row):
                """返回 row 的入口身份，HTTP 状态与输出由业务断言另行判断。"""
                return row.get("method"), row.get("path"), row.get("route"), tuple(row.get("command", [])), row.get("symbol")

            seen = {identity(row) for row in self.fact["baseline"]["observed_entries"]}
            same = same and bool(seen) and seen.issubset({identity(row) for row in candidate["observed_entries"]})
        self.fact.update(candidate=candidate, status="verified" if same and candidate["status"] == "passed" else "unverified")
        self.fact["coverage_review"] = coverage_review(self.requirement[0], candidate) if self.fact["status"] == "verified" else None
        return self.fact

    def final(self, patch: str) -> dict:
        """用实际送评 patch 复核回执，内容变更后使旧证明失效，返回可保存的摘要。"""
        fingerprint = digest_bytes(patch.encode())
        result = {**self.fact, "entry_kind": self.requirement[0], "progress": self.progress,
                  "submitted_sha256": fingerprint, "files": patch_paths(patch)}
        if not self.fact.get("patch_sha256") or fingerprint != self.fact["patch_sha256"]:
            result["status"] = "candidate_changed" if "patch_sha256" in self.fact else "unverified"
        return result
