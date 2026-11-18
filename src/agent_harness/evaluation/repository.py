"""真实仓库评测环境：固定镜像、无网络命令、干净副本评分，绝不挂载Docker socket给Agent。"""

import asyncio
import json
import hashlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
import posixpath
import tarfile
from agent_harness.evaluation.archive import CandidateArchive
from tempfile import TemporaryDirectory
from uuid import uuid4

from agent_harness.evaluation.public_self_test import (
    MAX_PUBLIC_SELF_TEST_SECONDS, PUBLIC_SELF_TEST_DRIVER, PYTEST_SELF_TEST_ARGV, PYTEST_SELF_TEST_CASES,
    PYTEST_SELF_TEST_NODE, VERSION_PROBE, archive_pytest_candidate, freeze_pytest_self_test_metadata, pytest_self_test_result,
)
from agent_harness.execution.processes import ProcessRunner, native_io_path
from agent_harness.schemas import RunConfig
from agent_harness.tools.registry import ToolExecutor, _CLEANUP_RANK, _error
from agent_harness.tools.command import RunCommandArguments
from agent_harness.execution.workspace import Workspace, WorkspaceError


class RepositoryEnvironment:
    """一题的宿主环境管理器；模型只接触源码和命令容器，不接触记录、答案和评分器。"""

    def __init__(self, record: dict, directory: Path, config: RunConfig, python: Path, *, public_self_test: bool = False):
        """固定 record 任务、directory 证据目录、config 进程限额与独立评分 python；不运行命令。

        public_self_test 默认 False，显式启用时仅接纳已批准的两个 pytest 实例，否则抛 ValueError。
        """
        if public_self_test and (record.get("instance_id") not in PYTEST_SELF_TEST_CASES or record.get("repo") != "pytest-dev/pytest"):
            raise ValueError("公开自测仅支持两道冻结的 pytest 评测题")
        self.record = record  # 官方完整任务，仅宿主持有，含参考及测试补丁。
        self.directory = directory.resolve()  # 此题专属证据，永不挂载到Agent命令容器。
        self.config = config  # 两组共享的工具及进程配置。
        self.python = python.resolve()  # 安装swebench 4.1.0的解释器。
        self.image = ""  # prepare后为不可变sha256镜像ID，不用浮动标签执行。
        self.archive = self.directory / "source.tar"  # 仅基线commit文件，无完整Git历史。
        self.baseline = ""  # 评分镜像在本地生成并固定的可比较 Git 提交。
        self.spec = {}  # 上游官方评分脚本和版本，prepare后填充。
        self.baselines = {}  # 工作区绝对路径到初始Git提交，保存在宿主，Agent提交不会重置比较基准。
        self.sessions: dict[Path, str] = {}  # 每个活动工作树独占的容器名，清理确认后移除。
        self.generated: set[str] = set()  # 来自固定镜像的未跟踪运行依赖，不作为候选新文件导出。
        self.public_self_test = public_self_test  # 两种策略冻结相同环境的显式试验开关。
        self.public_self_test_metadata: dict | None = None  # 固定镜像材质的版本、公开基线用例和驱动指纹。
        self.public_version_file = self.directory / "pytest-version.py"  # 只挂到自测临时容器，不进入候选工作树。
        self._baseline_source_sha256: str | None = None  # 保存已验真原始基线，用于核对自测只读挂载。

    def full_stdout(self, result: dict) -> str:
        """读取result关联的完整有界stdout原件；没有产物引用返回空字符串，不用预览冒充完整输出。"""
        reference = result.get("stdout_ref")
        raw = self.directory / "processes" / Path(reference).relative_to("artifacts") if reference else None
        return native_io_path(raw).read_text(encoding="utf-8", errors="replace") if raw else ""

    async def host(self, argv: list[str], seconds: float = 600, *, deadline: float | None = None, before_spawn: Callable[[], None] | None = None) -> dict:
        """在证据目录执行可信 argv，返回真实回执；seconds 为超时秒数，argv 不经 Shell 解释。

        deadline 为含等待进程槽位的绝对单调截止时间，None 不额外限制；before_spawn 非空时在取得槽位后调用。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        limits = self.config.limits.model_copy(update={"command_timeout_seconds": seconds})
        return await ProcessRunner(limits, self.directory / "processes").run(
            argv, self.directory, timeout_seconds=seconds, deadline=deadline, before_spawn=before_spawn,
        )

    async def checked(self, argv: list[str], seconds: float = 600) -> str:
        """执行可信argv并返回stdout；超时、非零或未确认清理抛受控环境错误。"""
        result = await self.host(argv, seconds)
        if result.get("exit_code") != 0 or result.get("timed_out") or result.get("cleanup_status") not in {"complete", "not_needed"}:
            raise ValueError("仓库环境命令失败；详见该题processes原始日志")
        return result.get("stdout", "")

    async def container(self, command: list[str], mounts: list[str], seconds: float = 600, *, deadline: float | None = None) -> dict:
        """以固定镜像运行 command 并返回进程回执；mounts 仅由宿主构造。

        seconds 是执行限时，deadline 非空时还限制进程槽位等待；已接纳的容器最终必须完成清理。
        """
        name = "ah-eval-" + uuid4().hex
        admitted = deadline is None  # 未启用额外截止时间时沿用既有清理契约。

        def mark_admitted() -> None:
            """在真实进程创建前标记容器需清理；返回 None。"""
            nonlocal admitted
            admitted = True

        argv = ["docker", "run", "--name", name, "--pull=never", "--network=none", "--cpus=2", "--memory=4g",
                "--pids-limit=256", "--cap-drop=ALL", "--security-opt=no-new-privileges", *mounts,
                "--entrypoint", "/bin/bash", self.image, *command]
        try:
            if deadline is None:
                result = await self.host(argv, seconds)
            else:
                result = await self.host(argv, seconds, deadline=deadline, before_spawn=mark_admitted)
            result["container_name"] = name  # 对应本次唯一容器，便于独立验证超时后的删除结果。
            return result
        finally:
            if admitted:
                # docker 客户端退出不表示容器内进程终止，必须单独核验删除。
                removed = await asyncio.shield(self.host(["docker", "rm", "-f", name], 30))
                if removed.get("exit_code") != 0:
                    raise RuntimeError("容器清理未确认，禁止继续试次")

    async def prepare(self, expected: dict | None = None) -> dict:
        """生成评分脚本并导出精确基线。expected为已预检指纹，非空时使用并核对固定镜像；返回环境指纹。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        record_path = self.directory / "record.json"
        record_path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        worker = Path(__file__).with_name("swe_worker.py")
        await self.checked([str(self.python), str(worker), str(record_path), str(self.directory / "spec.json")])
        self.spec = json.loads((self.directory / "spec.json").read_text(encoding="utf-8"))
        if expected is None:
            # 本地已有官方镜像时直接冻结实际ID；只有缓存缺失才访问镜像仓库。
            try:
                self.image = (await self.checked(["docker", "image", "inspect", "--format", "{{.Id}}", self.spec["image"]])).strip()
            except ValueError:
                if self.public_self_test:
                    raise ValueError("两题离线自测需要本机已缓存的固定官方镜像") from None
                await self.checked(["docker", "pull", self.spec["image"]], 1200)
                self.image = (await self.checked(["docker", "image", "inspect", "--format", "{{.Id}}", self.spec["image"]])).strip()
        else:
            try:
                self.image = (await self.checked(["docker", "image", "inspect", "--format", "{{.Id}}", expected["image_id"]])).strip()
            except ValueError:
                # 本轮允许回收已登记的镜像；重新拉取后必须匹配预检时的不可变ID。
                await self.checked(["docker", "pull", expected["image"]], 1200)
                self.image = (await self.checked(["docker", "image", "inspect", "--format", "{{.Id}}", expected["image"]])).strip()
                if self.image != expected["image_id"]:
                    raise ValueError("重新取得的镜像与冻结预检ID不同")
        if not self.image.startswith("sha256:"):
            raise ValueError("无法固定镜像ID")
        # 在容器本地目录生成完整 Git 基线。Windows 绑定挂载只交换归档，避免几万次 Git 小文件写入。
        export = """set -eu
cd /testbed
git reset --hard "$1" >/dev/null
git archive --format=tar --output=/export/source.tar "$1"
test "$(git rev-parse HEAD)" = "$1"
git ls-files --others -z > /export/image-files.z
tar --null --no-recursion -cf /export/image-files.tar -T /export/image-files.z
mkdir -p /tmp/harness-baseline
tar -xf /export/source.tar -C /tmp/harness-baseline
cd /tmp/harness-baseline
python - <<'PY'
from pathlib import Path
for entry in Path('.').rglob('*'):
    if entry.is_symlink():
        data = entry.read_bytes()
        entry.unlink()
        entry.write_bytes(data)
PY
git init -qb master
git config core.autocrlf false
git config core.filemode false
git add -f .
GIT_AUTHOR_DATE=2000-01-01T00:00:00Z GIT_COMMITTER_DATE=2000-01-01T00:00:00Z \
    git -c user.name='Harness Eval' -c user.email=eval@localhost commit -qm 'fixed baseline'
git rev-parse HEAD >/export/baseline.txt
git repack -adq
# repack 可能把分支移到 packed-refs，显式保留这一份固定分支引用。
cp /export/baseline.txt .git/refs/heads/master
# 仅归档试次需要的对象、索引和引用，排除 reflog、样例钩子与动态目录元数据。
tar -C .git -cf /tmp/harness-git.tar HEAD config index refs objects
cp /tmp/harness-git.tar /export/baseline.git.tar
"""
        outcome = await self.container(["-c", export, "export", self.record["base_commit"]],
            ["--mount", f"type=bind,source={self.directory},target=/export"])
        if outcome.get("exit_code") != 0:
            raise ValueError("镜像无法导出声明的基线commit")
        baseline = (self.directory / "baseline.txt").read_text(encoding="ascii").strip()
        if len(baseline) != 40 or any(char not in "0123456789abcdef" for char in baseline):
            raise ValueError("镜像无法生成固定差异基准")
        self.baseline = baseline
        self.generated = set((self.directory / "image-files.z").read_bytes().decode("utf-8").split("\0")) - {""}
        evidence = {"kind": "docker", "repository": self.record["repo"], "base_commit": self.record["base_commit"],
                "image": self.spec["image"], "image_id": self.image, "grader": "swebench==4.1.0",
                "grader_timeout_seconds": 600, "network": "none", "cpus": 2, "memory_bytes": 4294967296,
                "grader_sha256": hashlib.sha256(self.spec["script"].encode("utf-8")).hexdigest(),
                "source_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(), "git_baseline": baseline,
                "worktree_mode": "prepared_image_session", "generated_files": len(self.generated)}
        self._baseline_source_sha256 = evidence["source_sha256"]
        if self.public_self_test:
            version_probe = await self.container(["-c", VERSION_PROBE, "pytest-version", self.record["base_commit"]], [],
                                                 MAX_PUBLIC_SELF_TEST_SECONDS)
            if (version_probe.get("exit_code") != 0 or version_probe.get("timed_out")
                    or version_probe.get("output_limit_exceeded")):
                raise ValueError("无法从固定官方镜像核验 pytest 版本；详见离线探测原始输出")
            self.public_self_test_metadata = freeze_pytest_self_test_metadata(
                self.full_stdout(version_probe), self.archive, self.public_version_file,
            )
            evidence["public_self_test"] = self.public_self_test_metadata
        if expected is not None and evidence != expected:
            raise ValueError("固定镜像、源码或评分规格与零模型预检不一致")
        return evidence

    async def workspace(self, path: Path) -> None:
        """将基线解包至新path；内部普通文件链接物化为相同字节，拒绝目录链接/越界，返回None。"""
        path.mkdir(parents=True, exist_ok=False)
        with tarfile.open(self.archive) as archive:
            aliases = {}  # Windows不要求创建符号链接权限；只物化归档内直接指向普通文件的别名。
            for member in archive.getmembers():
                if not (path / member.name).resolve().is_relative_to(path.resolve()):
                    raise ValueError("仓库归档含链接、特殊文件或越界路径")
                if member.issym():
                    target = posixpath.normpath(str(PurePosixPath(member.name).parent / member.linkname))
                    if target.startswith(("../", "/")) or not archive.getmember(target).isfile():
                        raise ValueError("归档链接必须直接指向内部普通文件")
                    aliases[member.name] = archive.extractfile(target).read()
                elif not (member.isfile() or member.isdir()):
                    raise ValueError("仓库归档含不支持的特殊文件")
            archive.extractall(path, members=[member for member in archive.getmembers() if not member.issym()], filter="data")
            for name, content in aliases.items():
                (path / name).write_bytes(content)
        with tarfile.open(self.directory / "baseline.git.tar") as archive:
            if any(PurePosixPath(member.name).parts[:1] not in {("HEAD",), ("config",), ("index",), ("refs",), ("objects",)}
                   or not (member.isfile() or member.isdir())
                   for member in archive.getmembers()):
                raise ValueError("基线索引归档只能含 .git 内的普通文件和目录")
            archive.extractall(path / ".git", filter="data")
        self.baselines[str(path.resolve())] = self.baseline

    async def start_session(self, path: Path) -> None:
        """启动 path 独占的试次容器，保留镜像构建产物及跨命令临时文件；启动失败时回收。"""
        path = path.resolve(strict=True)
        name = "ah-trial-" + uuid4().hex
        if path in self.sessions:
            raise ValueError("工作树已有活动容器")
        self.sessions[path] = name
        try:
            mounts = ["--mount", f"type=bind,source={path},target=/testbed",
                      "--mount", f"type=bind,source={self.directory / 'image-files.tar'},target=/image-files.tar,readonly"]
            await self.checked(["docker", "run", "-d", "--name", name, "--pull=never", "--network=none",
                "--cpus=2", "--memory=4g", "--pids-limit=256", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--env", "PYTHONIOENCODING=utf-8", *mounts, "--entrypoint", "/bin/sleep", self.image, "infinity"])
            await self.checked(["docker", "exec", "-w", "/testbed", name, "tar", "--no-same-owner",
                                "--no-same-permissions", "-xf", "/image-files.tar"])
        except BaseException:
            await asyncio.shield(self.close_session(path))
            raise

    async def close_session(self, path: Path) -> None:
        """销毁 path 的专属容器；重复调用无副作用，删除未确认时抛异常并保留名称。"""
        key = path.resolve()
        name = self.sessions.get(key)
        if name is not None:
            result = await self.host(["docker", "rm", "-f", name], 30)
            if result.get("exit_code") and "No such container" not in result.get("stderr", ""):
                raise RuntimeError("试次容器清理未确认：" + name)
            self.sessions.pop(key)

    async def command(self, path: Path, argv: list[str], seconds: float, *, deadline: float | None = None,
                      env: dict | None = None) -> dict:
        """在 path 的活动容器运行 Bash argv，返回原始进程回执。seconds 和可选绝对 deadline 共同限时。"""
        flags = [part for key, value in (env or {}).items() for part in ("-e", f"{key}={value}")]
        try:
            result = await self.host(["docker", "exec", *flags, "-w", "/testbed", self.sessions[path.resolve()],
                                      "/bin/bash", *argv], seconds, deadline=deadline)
        except (asyncio.CancelledError, TimeoutError):
            await asyncio.shield(self.close_session(path))
            raise
        if result.get("timed_out") or result.get("output_limit_exceeded"):
            # 终止 docker exec 客户端不能证明容器中的进程停止。
            await self.close_session(path)
            raise TimeoutError("仓库命令中断，已销毁其试次容器")
        return result

    async def verify_workspace(self, path: Path) -> None:
        """实测 path 已解包的基线能在实际容器挂载中读取，比较提交身份与空差异；失败抛 ValueError。"""
        result = await self.command(path, ["-c", 'git rev-parse HEAD && git --no-pager diff --cached --exit-code HEAD'], 60)
        if result.get("exit_code") != 0 or self.full_stdout(result).strip() != self.baselines[str(path.resolve())]:
            raise ValueError("工作区基线身份与固定 Git 索引不一致")
        imports = {"django/django": "import django as target",
                   "astropy/astropy": "from astropy.io import fits as target",
                   "matplotlib/matplotlib": "from matplotlib import pyplot as target; target.close(target.figure())"}
        probe = imports.get(self.record["repo"])
        if probe:
            script = probe + "; from pathlib import Path; assert Path('/testbed') in Path(target.__file__).resolve().parents; print(target.__file__)"
            result = await self.command(path, ["-c", 'source /opt/miniconda3/bin/activate testbed && MPLBACKEND=Agg exec python -c "$1"',
                                               "import", script], 60)
            if result.get("exit_code") != 0:
                raise ValueError("工作区入口导入失败：" + (result.get("stderr") or result.get("stdout", ""))[-1500:])

    async def verify_public_self_test(self, workspace: Path) -> dict:
        """真实离线预检 workspace 的公开 pytest 自测，返回事实及环境原始日志位置。"""
        fact, process = await self._run_public_self_test(workspace, MAX_PUBLIC_SELF_TEST_SECONDS)
        return {**fact, "source": "preflight",
                "stdout_ref": process.get("stdout_ref"), "stderr_ref": process.get("stderr_ref")}

    async def _run_public_self_test(self, workspace: Path, timeout_seconds: float) -> tuple[dict, dict]:
        """以固定版本和公开基线测试验证 workspace 的源码快照，返回事实与原始进程回执。

        timeout_seconds 是含准备、等待槽位的总执行秒数；超时、字段错误与环境异常分别返回。
        Docker 清理失败抛 RuntimeError；取消原样传播，由持有 Run 的适配器登记。
        """
        started = asyncio.get_running_loop().time()
        fact = {"selector": PYTEST_SELF_TEST_NODE, "candidate_sha256": None, "tests_run": 0,
                "tests_collected": 0, "tests_skipped": 0, "exit_code": None}
        metadata = self.public_self_test_metadata
        if not self.public_self_test or not metadata:
            return {**fact, "status": "error", "reason": "self_test_environment", "elapsed_ms": 0}, {}

        def elapsed() -> int:
            """返回从自测准备开始经过的整数毫秒；不修改任何计时状态。"""
            return round((asyncio.get_running_loop().time() - started) * 1000)

        try:
            with self.archive.open("rb") as stream:
                actual_source = hashlib.file_digest(stream, "sha256").hexdigest()
            with self.public_version_file.open("rb") as stream:
                actual_version = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_source != self._baseline_source_sha256 or actual_version != metadata["version_sha256"]:
                raise ValueError("冻结的公开自测材料改变")
        except (OSError, ValueError):
            return {**fact, "status": "error", "reason": "self_test_environment", "elapsed_ms": elapsed()}, {}

        deadline = started + min(timeout_seconds, MAX_PUBLIC_SELF_TEST_SECONDS)
        with TemporaryDirectory(dir=self.directory, prefix="test-") as directory:
            snapshot = Path(directory) / "c.tar"
            try:
                fact["candidate_sha256"] = archive_pytest_candidate(workspace, snapshot)
            except (OSError, ValueError, tarfile.TarError):
                return {**fact, "status": "error", "reason": "candidate_archive_invalid", "elapsed_ms": elapsed()}, {}
            seconds = deadline - asyncio.get_running_loop().time()
            try:
                if seconds <= 0:
                    raise TimeoutError
                process = await self.container(
                    ["-c", PUBLIC_SELF_TEST_DRIVER, "public-self-test", metadata["selector"],
                     metadata["version"], metadata["version_sha256"]],
                    ["--mount", f"type=bind,source={snapshot},target=/candidate.tar,readonly",
                     "--mount", f"type=bind,source={self.archive},target=/baseline-source.tar,readonly",
                     "--mount", f"type=bind,source={self.public_version_file},target=/pytest-version.py,readonly"],
                    seconds, deadline=deadline,
                )
            except TimeoutError:
                return {**fact, "status": "timed_out", "reason": "self_test_timeout", "elapsed_ms": elapsed()}, {}
        result = pytest_self_test_result(process, self.full_stdout(process))
        return {**fact, **result, "elapsed_ms": elapsed()}, process

    async def patch(self, path: Path, output: Path, selected: tuple[str, ...] = (), *,
                    names_only: bool = False, deadline: float | None = None) -> str:
        """将试次目录 path 相对固定基线的改动写入 output，返回完整补丁正文。

        selected 为空时包含全部候选。names_only 为真时仅返回和保存 NUL 分隔的差异路径。
        deadline 可限制容器执行与排队的单调时钟截止时间，None 表示常规送评限时。
        文件与 Git 基线经独立的只读归档导入容器。若归档、命令或输出不完整则抛错。
        """
        baseline = self.baselines[str(path.resolve())]

        def include_entry(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            """保留可由 Git 比较的归档成员 member；遇到任意层级 Git 管理入口返回 None 并跳过子树。"""
            parts = PurePosixPath(member.name).parts
            if PurePosixPath(member.name).as_posix() in self.generated:
                return None
            if parts and parts[-1].casefold() == ".git":
                return None
            if member.isdir() and path.joinpath(*parts).is_junction():
                raise ValueError("无法归档工作区内的目录联接")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("工作区补丁包含不支持的特殊文件")
            return member

        # Windows 仅传输归档，避免容器通过绑定目录反复扫描数千个小文件。
        # 只解包可信 Git 基线，Agent 创建的提交、配置与钩子不会参与补丁计算。
        command = """set -eu
cd "$(mktemp -d)"
tar -xf /candidate.tar --no-same-owner
mkdir .git
tar -xf /baseline.git.tar -C .git --no-same-owner
git add -N .
format=$1
base=$2
shift 2
if [ "$format" = names ]; then set -- --name-only -z "$base" -- "$@"; else set -- --binary "$base" -- "$@"; fi
git --literal-pathspecs --no-pager diff --no-ext-diff --no-textconv "$@"
"""
        with TemporaryDirectory(dir=self.directory, prefix="patch-") as temp:
            candidate = Path(temp) / "candidate.tar"
            with CandidateArchive.open(candidate, "w", dereference=False) as archive:
                archive.add(path, arcname=".", filter=include_entry)
            result = await self.container(["-c", command, "patch", "names" if names_only else "patch", baseline, *selected], [
                "--mount", f"type=bind,source={candidate},target=/candidate.tar,readonly",
                "--mount", f"type=bind,source={self.directory / 'baseline.git.tar'},target=/baseline.git.tar,readonly",
            ], **({"deadline": deadline} if deadline is not None else {}))
        if result.get("exit_code") != 0 or result.get("timed_out") or result.get("output_limit_exceeded"):
            raise ValueError("无法完整采集补丁")
        patch = self.full_stdout(result)
        output.write_text(patch, encoding="utf-8", newline="\n")
        return patch

    async def grade(self, patch: str, evidence: Path) -> dict:
        """在全新官方镜像应用patch与固定测试补丁；返回官方FAIL_TO_PASS/PASS_TO_PASS判断和日志路径。"""
        evidence.mkdir(parents=True, exist_ok=False)
        for name, content in {"candidate.patch": patch, "eval.sh": self.spec["script"]}.items():
            (evidence / name).write_text(content, encoding="utf-8", newline="\n")
        command = 'exec 2>&1; cd /testbed && git reset --hard "$1" && if [ -s /evidence/candidate.patch ]; then git apply /evidence/candidate.patch || exit 97; fi; bash /evidence/eval.sh'
        result = await self.container(["-c", command, "grade", self.record["base_commit"]],
            ["--mount", f"type=bind,source={evidence},target=/evidence,readonly"], 600)
        # 完整进程输出用于官方解析，不使用被截断的页面摘要。
        stdout = self.full_stdout(result)
        stderr = result.get("stderr", "")
        log = evidence / "test_output.txt"
        log.write_text(stdout + "\n" + stderr, encoding="utf-8", newline="\n")
        if result.get("exit_code") == 97:
            return {"status": "failed", "reason": "patch_apply_failed", "stdout": stdout[-8000:], "stderr": stderr[-2000:]}
        if result.get("timed_out") or result.get("output_limit_exceeded") or result.get("exit_code") not in {0, 1}:
            return {"status": "error", "reason": "grading_process_failed", "stderr": stderr[-4000:]}
        output = evidence / "official.json"
        await self.checked([str(self.python), str(Path(__file__).with_name("swe_worker.py")),
            str(self.directory / "record.json"), str(output), str(log)])
        official = json.loads(output.read_text(encoding="utf-8"))
        status = "passed" if official["resolved"] else "failed" if official.get("tests_status") else "error"
        return {"status": status, "reason": None if status != "error" else "tests_not_parsed", "official": official,
                "stdout": stdout[-8000:], "stderr": stderr[-2000:], "exit_code": result.get("exit_code")}


class RepositoryExecutor:
    """评测专用工具适配；文件沿用既有边界，项目命令只进入挂载本试次源码的无网络容器。"""

    def __init__(self, environment: RepositoryEnvironment, workspace: Path, runs_root: Path, config: RunConfig):
        """绑定environment与唯一workspace；config仅允许本轮固定工具，不注入子Agent和后台命令。"""
        self.environment = environment  # 不可变镜像及宿主生命周期。
        self.inner = ToolExecutor(workspace, runs_root, config)  # 复用文件校验与历史证据读取。
        self.workspace = workspace  # 仅此目录进入命令容器。
        self.cleanup_status = "not_needed"  # 不确定容器清理会升级为unknown。
        self.artifact_refs = []  # 补丁由宿主单独采集，文件产物沿用Runner。
        self.permissions = None  # Runner绑定后用于逐次核验。
        self.before_execute = None  # Runner记录实际调用开始的回调。
        self.deadline: float | None = None  # Run 单调截止时间，转交文件工具核验。
        self.cancelled_result: dict | None = None  # 当前文件工具取消留下的真实进程回执。
        self.call_id = ""  # 当前调用的追溯编号。
        self.run_dir = None  # Runner绑定后存放可通过现有API回看的原始命令输出。
        self._emit_public_self_test: Callable[[str, dict], None] | None = None  # RunContext 的持久化事件入口。
        self._last_public_self_test: dict | None = None  # 最近自测回执，检查候选指纹前不作为收尾依据。
        self.delivery = None  # 仓库修复运行时由引擎指定证据目录，普通工具夹具不要求交付。
        self.batch_count = 0  # 已执行工具的模型轮数，多工具同批只计一次。
        self.closeout_patch = None  # 已在小额剩余预算时提醒过的已验证补丁 SHA。

    async def progress_feedback(self, remaining_turns: int | None = None) -> str | None:
        """按工作树和剩余请求 remaining_turns 给出下一步；None 仅检查工作树，无需提示时返回 None。"""
        if self.delivery is None:
            return None
        self.batch_count += 1
        fact = self.delivery.fact
        if (remaining_turns is not None and 0 < remaining_turns <= 8 and fact.get("status") == "verified"
                and fact.get("patch_sha256") and fact["patch_sha256"] != self.closeout_patch):
            self.closeout_patch = fact["patch_sha256"]
            return (f"剩余 {remaining_turns} 次模型请求。最近一次已验证补丁 {self.closeout_patch[:12]}，"
                    f"文件 {fact.get('selected', [])}。"
                    + (fact.get("coverage_review") or "若此后未改源码且没有新的失败证据，请现在用最终答复交付。"
                       "若内容已变，重跑所选补丁后交付。"))
        if self.batch_count % 8 or self.deadline and self.deadline - asyncio.get_running_loop().time() < 60:
            return None
        # Windows 绑定挂载逐文件扫描很慢；走实际送评的原生 Git 路径，输出仅含差异文件名。
        self.delivery.folder.mkdir(parents=True, exist_ok=True)
        try:
            scan = await self.environment.patch(self.workspace, self.delivery.folder / f"progress-{self.batch_count}.paths",
                                                names_only=True, deadline=self.deadline)
        except (OSError, ValueError):
            self.delivery.progress.append({"batch": self.batch_count, "state": "unavailable"})
            return None
        names = sorted(set(scan.split("\0")) - {""})
        sources = [name for name in names if not any(part in {"test", "tests", "testing", "build", "dist", "__pycache__"}
                   for part in PurePosixPath(name).parts)
                   and not PurePosixPath(name).name.lower().startswith(("test_", "repro", ".tmp", "tmp_repro"))
                   and PurePosixPath(name).suffix.lower() not in {".log", ".pyc", ".o", ".so"}]
        state = "source" if sources else "auxiliary" if names else "empty"
        receipt = {"batch": self.batch_count, "state": state, "paths": names[:12], "path_count": len(names)}
        self.delivery.progress.append(receipt)
        previous = self.delivery.progress[:-1]
        # 同状态仅提醒首次及两次仍无进展时；保留后续采样，不消耗模型轮次反复催促。
        repeats = sum(row["state"] == state for row in previous)
        if repeats not in (0, 2):
            return None
        if state != "source":
            return (f"第 {self.batch_count} 个工具批次，相对起始提交的改动为 {names[:5]}。"
                    "请据已有源码提出一个能被测试推翻的修复假设，运行最小复现或做最小源码修改。"
                    "若还缺信息，指出哪项观察能排除当前假设，再定向读取。")
        if self.delivery.fact.get("status") != "verified":
            return (f"工作树已有候选修改 {sources[:5]}。请从原始 issue 的用户操作重跑用例，"
                    "核对覆盖路径、关联用法及回归后选择实际交付文件。")
        return None

    async def delivery_feedback(self) -> str | None:
        """重新核对当前选中补丁；未验证时返回给模型的下一步说明，已验证或未启用返回 None。"""
        if self.delivery is None:
            return None
        if self.delivery.fact.get("next_action"):
            return self.delivery.fact["next_action"]
        if self.delivery.selected:
            patch = await self.environment.patch(self.workspace, self.delivery.folder / "selected.patch", self.delivery.selected)
            if self.delivery.final(patch)["status"] == "verified":
                return self.delivery.fact.get("coverage_review")
        return "仓库入口尚未验证。若预算允许，请用 harness-reproduce 冻结复现，用 harness-deliver 选择文件并重跑。如实说明仍受阻的地方。"

    @property
    def public_self_test_available(self) -> bool:
        """返回当前执行器是否启用且完成冻结公开自测；仅读取固定环境。"""
        return bool(getattr(self.environment, "public_self_test_metadata", None))

    def bind_public_self_test_events(self, emit: Callable[[str, dict], None]) -> None:
        """绑定当前 RunContext 的 emit；只记录公开自测事实，不合成模型工具请求。"""
        self._emit_public_self_test = emit

    def public_self_test_facts(self, *, emit_invalidation: bool = True) -> dict | None:
        """返回仍绑定于当前候选的最近自测证据；失效时返回 None。

        emit_invalidation 默认 True，在活跃 Run 记录失效并清除缓存；False 仅重新校验，
        供已关闭的 Run 读取报告，不修改状态或事件。
        """
        fact = self._last_public_self_test
        if not fact or not fact["candidate_sha256"]:
            return None
        try:
            with TemporaryDirectory(dir=self.environment.directory, prefix="match-") as directory:
                current = archive_pytest_candidate(self.workspace, Path(directory) / "c.tar")
            if current == fact["candidate_sha256"]:
                return dict(fact)
        except (OSError, ValueError, tarfile.TarError):
            pass  # 无法复核完整候选时同样不能将旧测试认作当前证据。
        if not emit_invalidation:
            return None
        self._last_public_self_test = None
        if self._emit_public_self_test:
            self._emit_public_self_test("public_self_test_invalidated", {
                "self_test_id": fact["self_test_id"], "candidate_sha256": fact["candidate_sha256"],
                "selector": fact["selector"], "source": fact["source"], "reason": "candidate_changed",
            })
        return None

    def _save_command_output(self, result: dict) -> list[dict]:
        """把 result 关联的原始双流拷入 Run，返回本次引用；无 Run 存储时返回空表。"""
        captures = []
        for stream in ("stdout", "stderr"):
            reference = result.get(stream + "_ref")
            if reference and self.run_dir is not None:
                source = self.environment.directory / "processes" / Path(reference).relative_to("artifacts")
                target = self.run_dir / reference
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                captured = {"location": "run", "path": reference, "kind": stream}
                captures.append(captured)
                self.artifact_refs.append(captured)
        return captures

    async def run_public_self_test(self, *, source: str = "handoff", timeout_seconds: float = MAX_PUBLIC_SELF_TEST_SECONDS) -> dict:
        """对当前候选执行冻结公开用例并返回扁平回执，原始日志随 Run 保存。

        source 是发起方，handoff 由宿主在工具额度接纳后调用，model 已走模型工具批次。
        timeout_seconds 是执行秒数且最多三十秒，同时受当前 Run 的绝对截止时间限制。
        """
        started = asyncio.get_running_loop().time()
        base = {"self_test_id": uuid4().hex, "selector": PYTEST_SELF_TEST_NODE, "source": source,
                "candidate_sha256": None, "exit_code": None, "tests_run": 0, "artifact_refs": []}
        if source == "model":
            base["call_id"] = self.call_id
        if (not self.public_self_test_available or self.permissions is None
                or "run_command" not in self.inner.config.tools or self.permissions.decision("run_command") != "allow"):
            return {**base, "status": "not_run", "reason": "permission_denied", "elapsed_ms": 0}
        seconds = min(timeout_seconds, self.inner.config.limits.command_timeout_seconds, MAX_PUBLIC_SELF_TEST_SECONDS)
        if self.deadline is not None:
            seconds = min(seconds, self.deadline - started)
        if seconds <= 0:
            return {**base, "status": "not_run", "reason": "self_test_timeout", "elapsed_ms": 0}
        if self._emit_public_self_test:
            self._emit_public_self_test("public_self_test_started", {**base, "timeout_seconds": seconds})
        try:
            details, process = await self.environment._run_public_self_test(self.workspace, seconds)
            if process:
                cleanup = process.get("cleanup_status", "complete")
                self.cleanup_status = max(self.cleanup_status, "complete", cleanup, key=_CLEANUP_RANK.__getitem__)
                if cleanup in {"unknown", "incomplete"}:
                    details.update(status="error", reason="cleanup_unconfirmed")
            fact = {**base, **details, "artifact_refs": self._save_command_output(process)}
        except RuntimeError:
            self.cleanup_status = max(self.cleanup_status, "unknown", key=_CLEANUP_RANK.__getitem__)
            fact = {**base, "status": "error", "reason": "cleanup_unconfirmed"}
        except (OSError, ValueError, tarfile.TarError):
            fact = {**base, "status": "error", "reason": "self_test_environment"}
        except asyncio.CancelledError as error:
            process = getattr(error, "harness_process_result", {}) or {}
            if process:
                self.cleanup_status = max(self.cleanup_status, process.get("cleanup_status", "unknown"), key=_CLEANUP_RANK.__getitem__)
            fact = {**base, "status": "cancelled", "reason": "self_test_cancelled",
                    "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000),
                    "artifact_refs": self._save_command_output(process)}
            self._last_public_self_test = fact
            if self._emit_public_self_test:
                self._emit_public_self_test("public_self_test_finished", fact)
            self.cancelled_result = {**_error("tool_cancelled", "公开自测被取消"), "data": {"public_self_test": fact}}
            raise
        fact.setdefault("elapsed_ms", round((asyncio.get_running_loop().time() - started) * 1000))
        self._last_public_self_test = fact
        if self._emit_public_self_test:
            self._emit_public_self_test("public_self_test_finished", fact)
        return fact

    @property
    def definitions(self) -> list[dict]:
        """返回固定工具声明；命令明确只支持前台，避免广告不可用能力。"""
        definitions = self.inner.definitions
        for item in definitions:
            if item["function"]["name"] == "search_files":
                item["function"]["description"] += " 限定单个文件时，directory 填父目录，glob 填文件名。"
            if item["function"]["name"] == "run_command":
                item["function"]["description"] = "在无网络Linux容器/testbed运行argv，cwd为相对目录；python使用已安装项目环境；只支持前台执行。"
                item["function"]["parameters"]["properties"]["background"]["const"] = False
                if self.public_self_test_available:
                    item["function"]["description"] += (
                        f"本题受控公开自测请以 argv={json.dumps(list(PYTEST_SELF_TEST_ARGV))}、cwd='.' 调用。"
                    )
        return definitions

    def bind_context(self, archive) -> None:
        """把宿主archive交既有只读read_context；不挂载到命令容器，返回None。"""
        self.inner.bind_context(archive)

    def bind_run(self, run_dir: Path, emit=None) -> None:
        """绑定run_dir以存放可点击的命令原始输出；emit由Runner提供但不启用宿主命令控制器。"""
        self.run_dir = run_dir  # 仅宿主写入运行证据，绝不挂载到模型容器。
        self.inner.bind_run(run_dir)

    async def execute(self, name: str, arguments_json: str) -> dict:
        """按name校验arguments_json并执行；返回统一回执，失败不退回宿主执行命令。"""
        self.cancelled_result = None
        self.inner.permissions, self.inner.before_execute, self.inner.call_id = self.permissions, self.before_execute, self.call_id
        self.inner.deadline = self.deadline
        if name != "run_command":
            try:
                result = await self.inner.execute(name, arguments_json)
                if name in {"write_file", "edit_file"} and result.get("ok"):
                    self.public_self_test_facts()
                return result
            finally:
                self.cancelled_result = self.inner.cancelled_result
                self.cleanup_status = max(self.cleanup_status, self.inner.cleanup_status, key=_CLEANUP_RANK.__getitem__)
                for reference in self.inner.artifact_refs:
                    if reference not in self.artifact_refs:
                        self.artifact_refs.append(reference)
        try:
            args = RunCommandArguments.model_validate_json(arguments_json)
            cwd = Workspace(self.workspace, self.inner.runs_root).resolve(args.cwd, must_exist=True)
            if name not in self.inner.config.tools or args.background or self.permissions is None or self.permissions.decision(name) != "allow":
                return _error("permission_denied", "本轮容器仅允许已授权的前台命令")
            if self.before_execute:
                self.before_execute()
            relative = cwd.relative_to(self.workspace).as_posix()
            if self.delivery is not None and args.argv[0] in {"harness-reproduce", "harness-deliver"}:
                seconds = self.inner.config.limits.command_timeout_seconds
                if self.deadline is not None:
                    seconds = min(seconds, self.deadline - asyncio.get_running_loop().time())
                if seconds <= 0:
                    raise TimeoutError
                try:
                    async with asyncio.timeout(seconds):
                        if args.argv == ["harness-reproduce"]:
                            fact = await self.delivery.register(seconds)
                        elif args.argv[0] == "harness-deliver":
                            fact = await self.delivery.candidate(args.argv[1:], seconds)
                        else:
                            raise ValueError("harness-reproduce 无需参数")
                except ValueError as error:
                    return _error("reproduction_error", str(error))
                return {"ok": True, "data": fact, "error": None, "truncated": False, "artifact_refs": []}
            if self.public_self_test_available and relative == "." and tuple(args.argv) == PYTEST_SELF_TEST_ARGV:
                fact = await self.run_public_self_test(source="model")
                return {"ok": fact["status"] == "passed", "data": {"public_self_test": fact, "exit_code": fact["exit_code"],
                        "timed_out": fact["status"] == "timed_out", "cleanup_status": self.cleanup_status},
                        "error": None if fact["status"] == "passed" else {
                            "code": fact["reason"], "message": "公开自测未通过；查看本次回执和日志",
                        }, "artifact_refs": fact["artifact_refs"]}
            command = 'source /opt/miniconda3/bin/activate testbed && cd "/testbed/$1" && shift && exec "$@"'
            result = await self.environment.command(self.workspace, ["-c", command, "agent", relative, *args.argv],
                self.inner.config.limits.command_timeout_seconds, deadline=self.deadline)
            self.cleanup_status = max(self.cleanup_status, "complete", key=_CLEANUP_RANK.__getitem__)
            # 非预设命令可能写工作区；只有候选指纹未变时才保留之前的自测事实。
            self.public_self_test_facts()
            captures = self._save_command_output(result)
            code = result.get("error_code")
            if not code and result.get("exit_code") != 0:
                code = "command_nonzero_exit"
            return {"ok": result.get("exit_code") == 0, "data": result,
                    "error": None if code is None else {"code": code, "message": "容器命令未成功"}, "truncated": bool(result.get("truncated")),
                    "artifact_refs": captures}
        except WorkspaceError as error:
            return _error(error.code, error.message)
        except TimeoutError:
            raise
        except (ValueError, OSError):
            return _error("invalid_arguments", "容器命令参数或工作目录无效")
        except BaseException:
            self.cleanup_status = max(self.cleanup_status, "unknown", key=_CLEANUP_RANK.__getitem__)
            raise
