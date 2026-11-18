"""限定真实仓库评测中的公开 pytest 自测、生效版本和可核对回执。"""

import base64
import binascii
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


PYTEST_SELF_TEST_CASES = frozenset({"pytest-dev__pytest-5262", "pytest-dev__pytest-7521"})  # 已批准公开自测的两份固定任务。
PYTEST_SELF_TEST_NODE = "testing/test_capture.py::test_capture_binary_output"  # 两题原始 commit 都跟踪的公开用例。
PYTEST_SELF_TEST_ARGV = ("python", "-m", "pytest", "-q", PYTEST_SELF_TEST_NODE)  # 模型调用固定公开检查的完整参数。
MAX_PUBLIC_SELF_TEST_SECONDS = 30.0  # 单次公开自测执行限时，含准备候选和等待进程，单位秒。
_VERSION_FILE = "src/_pytest/_version.py"  # 镜像已安装的源码所使用的生成文件。
_REPORT_PREFIX = "HARNESS_PUBLIC_PYTEST "  # 可信驱动的结构化回报，不使用 pytest 文本冒充执行计数。
_CANDIDATE_PHASE = "HARNESS_PUBLIC_PYTEST_CANDIDATE"  # 冻结环境已验证，随后的初始化异常属于候选运行阶段。
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024  # 当前两题的候选快照上限；超出时不能验证该候选。
_MAX_VERSION_BYTES = 16 * 1024  # 生成文件体积上限，防止意外读入其他安装产物。

VERSION_PROBE = """set -e
source /opt/miniconda3/bin/activate testbed
cd /testbed
PYTHONDONTWRITEBYTECODE=1 python - "$1" <<'PY'
import base64
import json
from pathlib import Path
import re
import sys

import pytest
import _pytest._version as installed

p = Path('/testbed/src/_pytest/_version.py')
version = pytest.__version__
revision = re.search(r'\\+g([a-f0-9]{7,40})(?:\\.|$)', version)
if (
    p.is_symlink() or Path(installed.__file__).resolve() != p
    or Path(pytest.__file__).resolve().parent not in {Path('/testbed/src'), Path('/testbed/src/pytest')}
    or version != installed.version or revision is None
    or not sys.argv[1].startswith(revision.group(1))
):
    raise RuntimeError('官方镜像的 pytest 版本与固定基线不匹配')
with p.open('rb') as stream:
    raw = stream.read(16385)
if not raw or len(raw) > 16384:
    raise RuntimeError('镜像生成的 pytest 版本文件超限')
print(json.dumps({'version': version, 'content': base64.b64encode(raw).decode('ascii')}))
PY
"""  # 只读取同一镜像已安装的版本模块，用版本携带的 Git 前缀核验固定提交。


PUBLIC_SELF_TEST_DRIVER = """set -e
source /opt/miniconda3/bin/activate testbed
work=$(mktemp -d)
mkdir "$work/candidate" "$work/base"
tar -xf /candidate.tar -C "$work/candidate" --no-same-owner
tar -xf /baseline-source.tar -C "$work/base" --no-same-owner
export PYTHONPATH="$work/candidate/src"
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
cd "$work/base"
python - "$work/candidate" "$1" "$2" "$3" <<'PY'
import hashlib
import json
from pathlib import Path
import sys


def report(status, reason, code, collected=0, ran=0, skipped=0, **facts):
    '''写出 status/reason、实际 code 与 pytest 计数，附导入 facts 后以原始 code 结束。'''
    print('HARNESS_PUBLIC_PYTEST ' + json.dumps(dict(
        status=status, reason=reason, tests_collected=collected, tests_run=ran, tests_skipped=skipped,
        **facts
    )))
    raise SystemExit(code)


candidate, selector, expected_version, version_sha256 = Path(sys.argv[1]), *sys.argv[2:]
frozen_version = Path('/pytest-version.py').read_bytes()
test_path = Path(selector.split('::', 1)[0])
if not test_path.is_file() or hashlib.sha256(frozen_version).hexdigest() != version_sha256:
    report('error', 'self_test_environment', 2)

print('HARNESS_PUBLIC_PYTEST_CANDIDATE', flush=True)
candidate_src = candidate / 'src'
version_path = candidate_src / '_pytest' / '_version.py'
capture_path = candidate_src / '_pytest' / 'capture.py'
expected_pytest_paths = [candidate_src / 'pytest.py', candidate_src / 'pytest/__init__.py']
if not capture_path.is_file() or not any(p.is_file() for p in expected_pytest_paths):
    report('error', 'self_test_candidate_import', 2)
if version_path.exists() and version_path.read_bytes() != frozen_version:
    report('error', 'self_test_candidate_version', 2)
version_path.write_bytes(frozen_version)
capture_digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()

try:
    import pytest
    import _pytest.capture as capture
    import _pytest._version as version
except Exception as error:
    print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
    report('error', 'self_test_candidate_import', 2)
loaded_pytest = Path(pytest.__file__).resolve()
loaded_capture = Path(capture.__file__).resolve()
if (
    loaded_pytest not in expected_pytest_paths or loaded_capture != capture_path
    or Path(version.__file__).resolve() != version_path or pytest.__version__ != expected_version
    or hashlib.sha256(loaded_capture.read_bytes()).hexdigest() != capture_digest
):
    report('error', 'self_test_candidate_import', 2)

class Outcomes:
    '''通过 pytest 公开 hook 的实际回执统计被选中、运行和跳过的测试。'''

    def __init__(self):
        '''初始化本次 pytest 会话的计数和独立节点集合。'''
        self.collected = 0  # 已选中的总项数。
        self.ran = set()  # 实际到达 call 的未跳过节点。
        self.skipped = set()  # 任何阶段报告跳过的节点。

    def pytest_collection_finish(self, session):
        '''从最终选中列表 session.items 固定收集数量；返回 None。'''
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report):
        '''从真实测试 report 累计执行与跳过的节点；返回 None。'''
        if report.skipped:
            self.skipped.add(report.nodeid)
        elif report.when == 'call':
            self.ran.add(report.nodeid)


outcomes = Outcomes()
code = int(pytest.main(['-q', '-p', 'no:cacheprovider', selector], plugins=[outcomes]))
ran, skipped = len(outcomes.ran), len(outcomes.skipped)
if outcomes.collected == 0 and code == 5:
    status, reason = 'no_tests', 'self_test_no_tests'
elif code == 0 and ran == 0 and skipped:
    status, reason = 'all_skipped', 'self_test_all_skipped'
elif code == 0 and outcomes.collected > 0 and ran == outcomes.collected and not skipped:
    status, reason = 'passed', None
elif code == 1 and ran:
    status, reason = 'failed', 'self_test_assertion_failed'
else:
    status, reason = 'error', 'self_test_candidate_test_error'
report(
    status, reason, code, outcomes.collected, ran, skipped,
    pytest_path=loaded_pytest.relative_to(candidate).as_posix(),
    capture_path=loaded_capture.relative_to(candidate).as_posix(),
    capture_sha256=capture_digest,
    pytest_version=pytest.__version__,
)
PY
"""  # 将公开基线测试和候选源码分别放入容器，核对实际导入并记录 pytest 回执。



def freeze_pytest_self_test_metadata(probe: str, source: Path, version_file: Path) -> dict:
    """校验 probe 的镜像版本报告及 source 的公开用例，保存 version_file 并返回可冻结指纹。

    probe 为已确认退出的镜像探测完整标准输出；source 仅含 Git 基线。无法解析、
    版本来源或公开测试不成立时抛 ValueError，阻止进入付费阶段。
    """
    try:
        metadata = json.loads(probe)
        version = metadata["version"]
        data = base64.b64decode(metadata["content"], validate=True)
        test_file = PYTEST_SELF_TEST_NODE.split("::", 1)[0]
        with tarfile.open(source) as archive:
            if _VERSION_FILE in archive.getnames():
                raise ValueError("公开自测预期使用由镜像生成的版本文件")
            member = archive.getmember(test_file)
            stream = archive.extractfile(member) if member.isfile() else None
            if stream is None:
                raise ValueError("固定公开测试不是可读的普通文件")
            with stream:
                test_source = stream.read()
        if not isinstance(version, str) or not version or not 0 < len(data) <= _MAX_VERSION_BYTES:
            raise ValueError("无可信的 pytest 版本数据")
    except (KeyError, TypeError, ValueError, binascii.Error, tarfile.TarError) as error:
        raise ValueError("无法冻结 pytest 自测环境；详见原始离线预检日志") from error
    version_file.write_bytes(data)
    return {
        "selector": PYTEST_SELF_TEST_NODE,
        "version": version,
        "version_sha256": hashlib.sha256(data).hexdigest(),
        "source_file": test_file,
        "source_sha256": hashlib.sha256(test_source).hexdigest(),
        "runner_sha256": hashlib.sha256(PUBLIC_SELF_TEST_DRIVER.encode("utf-8")).hexdigest(),
    }


def archive_pytest_candidate(workspace: Path, destination: Path) -> str:
    """把 workspace 的候选字节写到 destination，返回有界归档的 SHA-256。

    排除基线 Git 元数据，拒绝链接和特殊文件。不同的候选文件、路径或内容对应不同的
    摘要；固定归档成员的元数据，避免同一候选因归档时刻或宿主文件属性变化而丢失回执。
    """
    size = 0  # 已接纳的源文件字节数。

    def include(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """审阅归档条目 member；Git 元数据整树跳过，其他条目经检查后规范化。"""
        parts = PurePosixPath(member.name).parts
        if parts and parts[-1].casefold() == ".git":
            return None
        if not (member.isfile() or member.isdir()) or (member.isdir() and workspace.joinpath(*parts).is_junction()):
            raise ValueError("候选自测归档含链接或特殊路径")
        nonlocal size
        size += member.size
        if size > _MAX_SNAPSHOT_BYTES:
            raise ValueError("候选自测归档超出上限")
        member.mtime, member.uid, member.gid = 0, 0, 0
        member.uname, member.gname = "", ""
        member.pax_headers.clear()
        return member

    with tarfile.open(destination, "w", dereference=False) as archive:
        archive.add(workspace, arcname=".", filter=include)
    with destination.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pytest_self_test_result(process: dict, stdout: str) -> dict:
    """由已监督的 process 和完整 stdout 组装公开自测事实，返回状态、原因与实际测试数。

    timeout 和输出截断优先处理；只有唯一结构化结果和真实退出码相符才接纳通过。
    """
    defaults = {"tests_collected": 0, "tests_run": 0, "tests_skipped": 0, "exit_code": process.get("exit_code")}
    if process.get("timed_out"):
        return {**defaults, "status": "timed_out", "reason": "self_test_timeout"}
    if process.get("output_limit_exceeded"):
        return {**defaults, "status": "error", "reason": "self_test_output_limit"}
    lines = [line.removeprefix(_REPORT_PREFIX) for line in stdout.splitlines() if line.startswith(_REPORT_PREFIX)]
    if len(lines) == 1:
        try:
            report = json.loads(lines[0])
            if not isinstance(report, dict):
                raise ValueError("自测回执不是对象")
            status = report.get("status")
            counts = tuple(report.get(key) for key in ("tests_collected", "tests_run", "tests_skipped"))
            valid_counts = all(type(number) is int and number >= 0 for number in counts)
            if valid_counts and status in ("passed", "failed", "error", "no_tests", "all_skipped"):
                collected, ran, skipped = counts
                exit_code = defaults["exit_code"]
                if (status == "passed" and exit_code == 0 and ran > 0 and ran == collected and skipped == 0
                        or status == "failed" and exit_code == 1 and ran > 0
                        or status == "error" and exit_code in {0, 1, 2, 3, 4, 5}
                        or status == "no_tests" and exit_code == 5 and collected == 0
                        or status == "all_skipped" and exit_code == 0 and ran == 0 and skipped > 0):
                    keys = ("status", "reason", "tests_collected", "tests_run", "tests_skipped",
                            "pytest_path", "capture_path", "capture_sha256", "pytest_version")
                    return {**{key: report[key] for key in keys if key in report}, "exit_code": exit_code}
        except ValueError:
            pass
    reason = "self_test_candidate_test_error" if _CANDIDATE_PHASE in stdout.splitlines() else "self_test_environment"
    return {**defaults, "status": "error", "reason": reason}
