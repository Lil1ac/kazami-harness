"""仓库评测的门禁和真实Docker链路；Docker测试显式启用，不把脚本模型当质量成绩。"""

import asyncio
import json
import os
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_harness.config import load_config
from agent_harness.evaluation.engine import evaluate
from agent_harness.evaluation.models import Experiment
from agent_harness.evaluation.repository import RepositoryEnvironment, RepositoryExecutor
from agent_harness.providers.base import ModelResponse, ToolCall
from tests.helpers.models import configured_run


class AllowCommands:
    """离线门禁替身，仅返回当前测试已授权的工具判定。"""

    def decision(self, name):
        """接收工具name并返回allow；测试中的越界仍必须被工作区校验阻止。"""
        return "allow"


def test_archive_links_are_bounded_and_materialized(tmp_path, monkeypatch):
    """内部普通文件别名保持字节，越界链接必须拒绝，避免Windows链接权限成为运行前隐患。"""
    environment = RepositoryEnvironment({}, tmp_path / "evidence", configured_run(), Path(__file__))
    environment.directory.mkdir()

    environment.baseline = "a" * 40
    with tarfile.open(environment.directory / "baseline.git.tar", "w") as archive:
        content = b"ref: refs/heads/master\n"
        entry = tarfile.TarInfo("HEAD")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    for target, valid in (("image.png", True), ("../../outside", False)):
        with tarfile.open(environment.archive, "w") as archive:
            original = tarfile.TarInfo("image.png")
            original.size = 4
            archive.addfile(original, io.BytesIO(b"data"))
            alias = tarfile.TarInfo("alias.png")
            alias.type, alias.linkname = tarfile.SYMTYPE, target
            archive.addfile(alias)
        work = tmp_path / str(valid)
        if valid:
            asyncio.run(environment.workspace(work))
            assert (work / "alias.png").read_bytes() == b"data"
            assert not (work / "alias.png").is_symlink()
            assert (work / ".git/HEAD").read_bytes() == content
        else:
            with pytest.raises(ValueError):
                asyncio.run(environment.workspace(work))


def test_patch_stages_worktree_and_fixed_git_metadata(tmp_path, monkeypatch):
    """在工作区 Git 元数据已变更时，补丁仍从文件和固定基线归档生成；tmp_path 隔离试次，monkeypatch 替换 Docker，无返回值。"""
    environment = RepositoryEnvironment({}, tmp_path / "evidence", configured_run(), Path(__file__))
    environment.directory.mkdir()
    git_archive = environment.directory / "baseline.git.tar"
    git_archive.write_bytes(b"trusted baseline")
    workspace = tmp_path / "trial"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git/config").write_text("candidate git metadata", encoding="utf-8")
    (workspace / "subdir").mkdir()
    (workspace / "subdir/.git").write_text("candidate git reference", encoding="utf-8")
    (workspace / "subdir/added.py").write_bytes(b"answer = 42\n")
    baseline = "a" * 40
    environment.baselines[str(workspace.resolve())] = baseline
    expected_patch = "diff --git a/subdir/added.py b/subdir/added.py\n"
    staged = []

    async def container(command, mounts, seconds=600):
        """检查 command 的提交及 mounts 中只读归档；seconds 是超时秒数，返回假 Docker 成功回执。"""
        assert command[-1] == baseline
        sources = [Path(mount.split(",")[1].removeprefix("source=")) for mount in mounts[1::2]]
        assert workspace not in sources and sources.count(git_archive) == 1
        assert all(mount.endswith(",readonly") for mount in mounts[1::2])
        candidate = next(source for source in sources if source != git_archive)
        with tarfile.open(candidate) as archive:
            members = {Path(member.name).as_posix(): member for member in archive.getmembers()}
            assert ".git" not in members
            assert "subdir/.git" not in members
            assert archive.extractfile(members["subdir/added.py"]).read() == b"answer = 42\n"
        staged.append(candidate)
        return {"exit_code": 0, "stdout": expected_patch}

    def read_stdout(result):
        """从模拟回执 result 返回完整 stdout 正文。"""
        return result["stdout"]

    monkeypatch.setattr(environment, "container", container)
    monkeypatch.setattr(environment, "full_stdout", read_stdout)
    patch_path = environment.directory / "candidate.patch"
    assert asyncio.run(environment.patch(workspace, patch_path)) == expected_patch
    assert patch_path.read_bytes() == expected_patch.encode()
    assert len(staged) == 1 and not staged[0].exists()
    assert (workspace / ".git/config").read_text(encoding="utf-8") == "candidate git metadata"


def test_commands_never_fallback_to_host(tmp_path):
    """越界、后台及未启用命令均在容器调用前拒绝，不能回落为宿主进程。"""
    executor = RepositoryExecutor(SimpleNamespace(), tmp_path, tmp_path / "runs", configured_run(tools=["run_command"]))
    executor.permissions = AllowCommands()
    for arguments, code in (({"argv": ["python"], "cwd": "../"}, "path_outside_workspace"),
                            ({"argv": ["python"], "background": True}, "permission_denied")):
        result = asyncio.run(executor.execute("run_command", json.dumps(arguments)))
        assert result["error"]["code"] == code
    executor.inner.config.tools = []
    assert asyncio.run(executor.execute("run_command", '{"argv":["python"]}'))["error"]["code"] == "permission_denied"


def test_command_receipt_only_carries_its_own_output_references(tmp_path):
    """在 tmp_path 隔离的 17 次容器命令中核对逐条回执和完整的 Run 产物，无返回值。"""
    work = tmp_path / "work"
    work.mkdir()
    environment = SimpleNamespace(directory=tmp_path / "env")
    issued = []

    async def command(path, argv, seconds, *, deadline=None):
        """模拟 path 独占容器在 seconds 限额内执行 argv，返回可供适配器复制的双流回执。"""
        name = f"command-{len(issued) + 1}"
        artifact = Path("commands") / name
        directory = environment.directory / "processes" / artifact
        directory.mkdir(parents=True)
        for stream in ("stdout", "stderr"):
            (directory / f"{stream}.bin").write_bytes(f"{name}:{stream}".encode())
        issued.append(artifact)
        return {"exit_code": 0, **{f"{stream}_ref": f"artifacts/{artifact.as_posix()}/{stream}.bin"
                                  for stream in ("stdout", "stderr")}}

    environment.command = command
    runs = tmp_path / "runs"
    run = runs / "current"
    run.mkdir(parents=True)  # 完整 Runner 会在绑定产物前创建并登记运行目录。
    executor = RepositoryExecutor(environment, work, runs, configured_run(tools=["run_command"]))
    executor.permissions = AllowCommands()
    executor.bind_run(run)
    for index in range(1, 18):
        receipt = asyncio.run(executor.execute("run_command", '{"argv":["python","-V"]}'))
        assert receipt["ok"]
        expected = [f"artifacts/commands/command-{index}/{stream}.bin" for stream in ("stdout", "stderr")]
        assert [reference["path"] for reference in receipt["artifact_refs"]] == expected
        for stream, relative in zip(("stdout", "stderr"), expected):
            assert (run / relative).read_bytes() == f"command-{index}:{stream}".encode()
    assert len(executor.artifact_refs) == 34


def test_repository_preflight_errors_never_call_model(tmp_path, monkeypatch):
    """参考修复通过但基线回归失败时拒绝付费，完整保留环境错误证据。"""
    record = {"instance_id": "fixture-1", "problem_statement": "fixed issue", "patch": "gold", "repo": "org/repo",
              "FAIL_TO_PASS": '["bug"]', "PASS_TO_PASS": '["regression"]'}
    (tmp_path / "record.json").write_text(json.dumps(record), encoding="utf-8")
    suite = {"name": "fixture", "version": "1", "provenance": "scripted", "cases": [
        {"id": "fixture-1", "project": "org/repo", "category": "fix", "split": "dev", "prompt": "fixed issue", "backend": "swebench", "source": "record.json"}]}
    (tmp_path / "suite.json").write_text(json.dumps(suite), encoding="utf-8")

    async def prepare(self, expected=None):
        """为self返回固定测试环境，忽略expected，不运行Docker。"""
        return {"image_id": "sha256:fixture"}

    async def workspace(self, path):
        """用路径 path 模拟已核对的解包，不运行Docker。"""
        path.mkdir(parents=True)

    async def verify_workspace(self, path):
        """核对被模拟的 path 已解包，避免在该离线用例调用 Docker。"""
        assert path.is_dir()

    async def grade(self, patch, evidence):
        """按patch是否为参考解生成证据；evidence只定位输出，返回有回归失败的基线。"""
        return {"status": "passed" if patch else "failed", "official": {"tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": ["bug"]}, "PASS_TO_PASS": {"success": [], "failure": ["regression"]}}}}

    monkeypatch.setattr("agent_harness.evaluation.repository.RepositoryEnvironment.prepare", prepare)
    monkeypatch.setattr("agent_harness.evaluation.repository.RepositoryEnvironment.workspace", workspace)
    monkeypatch.setattr("agent_harness.evaluation.repository.RepositoryEnvironment.verify_workspace", verify_workspace)
    monkeypatch.setattr("agent_harness.evaluation.repository.RepositoryEnvironment.start_session", verify_workspace)
    monkeypatch.setattr("agent_harness.evaluation.repository.RepositoryEnvironment.grade", grade)
    config = configured_run(tools=["read_file"], memory={"use": False, "capture": False})
    report = asyncio.run(evaluate(tmp_path / "suite.json", config, Experiment(), tmp_path / "runs", tmp_path / "work",
                                  "", swe_python=Path(__file__), preflight_only=True))
    assert report["stop_reason"] == "invalid_case" and report["trials"] == []
    assert report["preflight"][0]["baseline_valid"] is False


class ContainerProbe:
    """通过真实Runner验证容器命令与提交后补丁；输出不修复题目，因此独立评分必须失败。"""

    async def complete(self, messages, tools):
        """根据messages工具回执返回探测调用或结束；tools为真实声明，返回有完整用量的脚本响应。"""
        responses = [item for item in messages if item.get("role") == "tool"]
        if responses:
            assert json.loads(responses[-1]["content"])["ok"]
            return ModelResponse(message={"role": "assistant", "content": "环境探测完成，未修复issue。"}, tool_calls=[],
                                 response_model="offline-probe", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, finish_reason="stop")
        code = ("import os,pathlib; p=pathlib.Path; "
                "assert not os.getenv('DEEPSEEK_API_KEY'); assert not p('/var/run/docker.sock').exists(); "
                "assert not p('/evidence').exists(); assert sorted(os.listdir('/sys/class/net'))==['lo']; "
                "assert not p('record.json').exists(); p('isolation_probe.txt').write_text('isolation-ok')")
        # 在容器内提交，用于证明宿主差异基准不随Agent的HEAD变动。
        command = "python -c \"" + code + "\" && git add isolation_probe.txt && git -c user.name=probe -c user.email=probe@localhost commit -qm probe"
        call = ToolCall(call_id="probe", name="run_command", arguments_json=json.dumps({"argv": ["bash", "-c", command]}))
        return ModelResponse(message={"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id, "type": "function",
                             "function": {"name": call.name, "arguments": call.arguments_json}}]}, tool_calls=[call],
                             response_model="offline-probe", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, finish_reason="tool_calls")


@pytest.mark.skipif(os.getenv("HARNESS_TEST_DOCKER") != "1", reason="显式启用真实官方镜像集成检查")
def test_real_container_runner_and_clean_grader(tmp_path, monkeypatch):
    """两种策略均通过真实Runner调用无网络容器；提交后的补丁可导出，评分不误认探测为修复。"""
    root = Path(__file__).resolve().parents[2]
    record = json.loads((root / "evals/swe-pilot/pytest-dev__pytest-5262.json").read_text(encoding="utf-8"))
    (tmp_path / "record.json").write_text(json.dumps(record), encoding="utf-8")
    suite = {"name": "容器工程验收", "version": "1", "provenance": "脚本模型，仅验证工程", "cases": [
        {"id": record["instance_id"], "project": record["repo"], "category": "probe", "split": "dev", "prompt": record["problem_statement"],
         "backend": "swebench", "source": "record.json"}]}
    (tmp_path / "suite.json").write_text(json.dumps(suite), encoding="utf-8")
    config = load_config(root / "evals/swe-pilot/deepseek.toml")
    config.limits.max_model_turns = 4
    monkeypatch.setenv("DEEPSEEK_API_KEY", "probe-secret-must-not-enter-container")
    work = root / ".runtime/docker-work" / uuid4().hex[:8]  # 短路径避免Windows旧式260字符限制，试次仍用完整UUID。
    report = asyncio.run(evaluate(tmp_path / "suite.json", config, Experiment(), tmp_path / "runs", work,
                                  "fixture", client_factory=ContainerProbe, swe_python=root / ".runtime/swe-env/Scripts/python.exe"))
    assert report["state"] == "completed" and len(report["trials"]) == 2
    for row in report["trials"]:
        assert row["execution_status"] == "completed" and row["grade"]["status"] == "failed" and not row["success"]
        patch = tmp_path / "runs/.evaluations" / report["id"] / (row["run_id"] + ".patch")
        assert "isolation-ok" in patch.read_text(encoding="utf-8")
    environment = RepositoryEnvironment(record, tmp_path / "cleanup", config, root / ".runtime/swe-env/Scripts/python.exe")
    environment.image = report["environments"][record["instance_id"]]["image_id"]
    result = asyncio.run(environment.container(["-c", "sleep 30"], [], 1))
    assert result["timed_out"]
    remaining = asyncio.run(environment.checked(["docker", "ps", "-aq", "--filter", "name=^/" + result["container_name"] + "$"]))
    assert not remaining.strip()
